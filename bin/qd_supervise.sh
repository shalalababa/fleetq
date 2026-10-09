#!/usr/bin/env bash
# qd_supervise.sh — keep exactly one qd alive across a periodic memory reducer
# (the workstation this ran on killed large processes every 30 min) and any OOM
# kill.  Tiny (a bash sleep loop, ~4 MB RSS) so it is never itself the
# reducer's target; qd is restart-safe, so a restart costs nothing.
#
#   start:   nohup setsid bin/qd_supervise.sh start >/dev/null 2>&1 &
#   status:  bin/qd_supervise.sh status
#   stop:    bin/qd_supervise.sh stop
#
# Q_ROOT selects the data root (default: the checkout), so several independent
# queues (a sandbox next to a production one) can each have their own daemon.
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
QROOT="${Q_ROOT:-$(dirname "$HERE")}"
SLOG="$QROOT/supervise.log"
SLOCK="$QROOT/qd_supervise.lock"
PIDF="$QROOT/qd_supervise.pid"
say(){ echo "$(date -Is) supervise: $*" >> "$SLOG"; }
# supervise.log carries qd's stderr, so a crash loop can fill it fast: keep 2
# generations rather than letting it grow without bound during an incident.
rotate_slog(){
  local sz; sz=$(stat -c%s "$SLOG" 2>/dev/null || echo 0)
  if [ "$sz" -gt 5242880 ]; then mv -f "$SLOG.1" "$SLOG.2" 2>/dev/null; mv -f "$SLOG" "$SLOG.1"; fi
}
# a pid file can outlive its process and the pid can be REUSED by something
# unrelated: never signal a pid whose cmdline is not ours.
is_qd(){ [ -n "${1:-}" ] && tr -d "\0" < "/proc/$1/cmdline" 2>/dev/null | grep -q "qd"; }

case "${1:-start}" in
status)
  if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
    echo "supervisor RUNNING pid=$(cat "$PIDF")"
  else echo "supervisor NOT running"; fi
  qp=$(cat "$QROOT/qd.lock" 2>/dev/null || true)
  if is_qd "${qp:-}"; then echo "qd RUNNING pid=$qp"
  elif [ -n "${qp:-}" ] && kill -0 "$qp" 2>/dev/null; then
    echo "qd NOT running (qd.lock pid $qp belongs to something else — stale lock)"
  else echo "qd NOT running"; fi
  tail -n 3 "$QROOT/BOARD.txt" 2>/dev/null
  exit 0;;
stop)
  if [ -f "$PIDF" ]; then
    sp=$(cat "$PIDF")
    if tr -d "\0" < "/proc/$sp/cmdline" 2>/dev/null | grep -q "qd_supervise"; then
      kill "$sp" 2>/dev/null
    else say "PIDF pid $sp is not a supervisor — not signalling it"; fi
    rm -f "$PIDF"
  fi
  qp=$(cat "$QROOT/qd.lock" 2>/dev/null || true)
  if is_qd "${qp:-}"; then kill "$qp" 2>/dev/null
  else say "qd.lock pid ${qp:-none} is not a qd — not signalling it"; fi
  say "stopped by operator"
  echo "stop signalled (local tasks already running are left alone)"
  exit 0;;
start) ;;
*) echo "usage: $0 start|stop|status" >&2; exit 1;;
esac

exec 9>"$SLOCK"
if ! flock -n 9; then echo "another supervisor holds $SLOCK" >&2; exit 3; fi
echo $$ > "$PIDF"
trap 'say "exiting"; rm -f "$PIDF"' EXIT
say "start (Q_ROOT=$QROOT)"
fails=0
last_ok=$(date +%s)
while true; do
  rotate_slog
  start=$(date +%s)
  Q_ROOT="$QROOT" "$HERE/qd" >> "$SLOG" 2>&1
  rc=$?
  if [ "$rc" = 3 ]; then say "another qd holds the lock; idling"; sleep 60; continue; fi
  now=$(date +%s)
  # a qd that survived 5 minutes was not crash-looping: reset the backoff
  if [ $((now - start)) -ge 300 ]; then fails=0; last_ok=$now; fi
  fails=$((fails+1))
  back=$((10 * fails)); [ "$back" -gt 120 ] && back=120
  say "qd exited rc=$rc after $((now-start))s (restart #$fails in ${back}s)"
  sleep "$back"
done
