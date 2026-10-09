#!/usr/bin/env bash
# Hard deadline for the queue work on one rented box.
#
#   watchers/deadline_worker.sh N --soft 'YYYY-MM-DD HH:MM' --hard 'YYYY-MM-DD HH:MM'
#                               [--procs REGEX]
#
#   soft  -- pull everything already finished (per-task, sha-verified).
#   hard  -- cancel the queue jobs pinned to box N, SIGTERM the science
#            processes and wait for them to exit, pull again, then compare the
#            box's tree with the local cache at out/ level.
#
# Written for the night a payment problem meant every box had to stop by a
# fixed time.  It does NOT destroy anything.  When the final comparison
# passes it drops $FLEET_STATE/no_more_work_box<N>, which is the first gate of
# watchers/destroy_gate_worker.sh; keeping the destroy gates in one place
# means they are never duplicated (and never drift) here.
#
# Arm it at decision time as a detached process, so the deadline does not
# depend on the session that set it still being alive:
#   setsid nohup watchers/deadline_worker.sh 5 --soft '...' --hard '...' >/dev/null 2>&1 &
# Cancel before it fires:  touch $Q_ROOT/spool/tmp/deadline_cancel_box<N>
set -uo pipefail
FLEET_HOME="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
source "$FLEET_HOME/fleet/common.sh"

N=""; SOFT_AT=""; HARD_AT=""
# Bracket the first letter so the pattern never matches the shell that carries it.
PROCS="${FLEET_SCIENCE_PROCS:-[t]rain\.py|[b]enchmark\.py|[r]un\.py}"
while [ $# -gt 0 ]; do
  case "$1" in
    --soft)  SOFT_AT="${2:?}"; shift 2 ;;
    --hard)  HARD_AT="${2:?}"; shift 2 ;;
    --procs) PROCS="${2:?}"; shift 2 ;;
    *)       N="$1"; shift ;;
  esac
done
[ -n "$N" ] && [ -n "$SOFT_AT" ] && [ -n "$HARD_AT" ] ||
  die "usage: deadline_worker.sh N --soft TIME --hard TIME [--procs REGEX]"
SOFT=$(date -d "$SOFT_AT" +%s) || die "bad --soft time"
HARD=$(date -d "$HARD_AT" +%s) || die "bad --hard time"

Q="$FLEET_HOME/bin/q"
RROOT="${Q_VAST_REMOTE_ROOT:-/workspace/qvast}"
mkdir -p "$Q_ROOT/logs" "$Q_ROOT/spool/tmp" "$FLEET_STATE"
exec 9>"$Q_ROOT/spool/tmp/.deadline_box$N.lock"
flock -n 9 || { echo "another deadline worker for box $N holds the lock"; exit 0; }
LOG="$Q_ROOT/logs/deadline_box$N.log"
CANCEL_FLAG="$Q_ROOT/spool/tmp/deadline_cancel_box$N"
say(){ printf '%s %s\n' "$(date '+%F %T %z')" "$*" >> "$LOG"; }
say "DEADLINE WORKER ARMED pid=$$ box=$N soft=$SOFT_AT hard=$HARD_AT (no destroy)"

# Child bash: timeout cannot run a shell function, and fl_require_instance
# calls die(), which must not end the worker.
on_box(){ timeout "${2:-120}" bash -c 'source "$1/fleet/common.sh"; fl_ssh_script "$2" "$3" "$4" "$5"' \
            _ "$FLEET_HOME" "$N" "$1" "$RROOT" "$PROCS"; }

# Queue jobs placed on this box: `q status` columns are name, state, site; the
# box comes from the job's state file (pinned --vast-box, else where qd put it).
jobs_on_box(){
  timeout 90 "$Q" status 2>/dev/null | awk '$3=="vast"{print $1" "$2}' |
  while read -r j st; do
    bx=$(python3 - "$Q_ROOT/state/$j.json" <<'PY' 2>/dev/null
import json, sys
d = json.load(open(sys.argv[1]))
print(d["spec"].get("vast_box") or d.get("pending_box") or "")
PY
)
    [ "$bx" = "$N" ] && echo "$j $st"
  done
}

wait_until(){  # $1 epoch, $2 label
  local target=$1 label=$2 now left
  while :; do
    if [ -f "$CANCEL_FLAG" ]; then
      say "CANCELLED: $CANCEL_FLAG present -- $label not fired, worker exiting"
      exit 0
    fi
    now=$(date +%s)
    [ "$now" -ge "$target" ] && return 0
    left=$(( target - now ))
    if [ "$left" -gt 900 ]; then sleep 600; else sleep 30; fi
  done
}

# Job roots on the box that have results.  A pull is driven over every job
# root that EXISTS on the box, not over the queue's list: a cancelled or
# archived job can still hold the only copy of finished cells.
LIST_ROOTS='for j in "$1"/*/; do [ -d "$j/out" ] && basename "$j"; done'
pull_all(){
  local jl j
  jl=$(on_box "$LIST_ROOTS" 120 2>/dev/null)
  for j in $jl; do
    say "  delta pull $j"
    timeout 1800 bash "$FLEET_HOME/lib/vast_task_pull.sh" "$j" "$N" >/dev/null 2>&1 ||
      say "  delta pull $j returned non-zero (see logs/$j.taskpull.log)"
  done
}

# ---------- soft pull ----------
wait_until "$SOFT" "soft pull"
say "=== SOFT PULL: pulling everything already finished ==="
jobs_on_box | while read -r j st; do say "  queue job $j state=$st"; done
pull_all
say "=== soft pull done ==="

# ---------- hard stop ----------
wait_until "$HARD" "hard stop"
say "=== HARD STOP: ending all queue work on box $N ==="
jobs_on_box | while read -r j st; do
  case "$st" in
    RUNNING|QUEUED|ADMITTED)
      say "  cancelling $j (was $st)"
      timeout 120 "$Q" cancel "$j" >> "$LOG" 2>&1 ;;
    *) say "  leaving $j ($st)" ;;
  esac
done
sleep "${FLEET_CANCEL_SETTLE_SEC:-60}"   # let cancelled runners exit before quiescing

say "--- box-side process quiesce ---"
# SIGTERM, then WAIT for the processes to actually go.  A payload with a
# SIGTERM handler writes its last checkpoint AFTER the signal (tmp file +
# os.replace), so pulling on a fixed sleep races that save and captures the
# previous checkpoint.  Poll until quiet (bounded), then pull.  Stopped (T)
# processes and flock wrappers are not counted.
QUIESCE=$(cat <<'EOS'
# The regex goes through the environment: awk -v would interpret its backslashes.
export RE="$1"
count(){ ps -eo stat=,args= | awk '$1 !~ /^T/ && $2 != "flock" && $0 ~ ENVIRON["RE"] {k++} END{print k+0}'; }
ps -eo pid=,args= | awk -v me=$$ '$1 != me && $0 ~ ENVIRON["RE"] {print $1}' | xargs -r kill -TERM 2>/dev/null
for i in $(seq 1 30); do
  [ "$(count)" -eq 0 ] && { echo "QUIET after $((i*3))s"; break; }
  sleep 3
done
echo "QUIESCE VERDICT: $(count) science process(es) still running"
EOS
)
# Called directly rather than through on_box: this script wants the regex as $1.
timeout 240 bash -c 'source "$1/fleet/common.sh"; fl_ssh_script "$2" "$3" "$4"' \
  _ "$FLEET_HOME" "$N" "$QUIESCE" "$PROCS" >> "$LOG" 2>&1 || say "  box $N unreachable at hard stop"

say "--- FINAL verified pull: pull, then enumerate the box tree and compare ---"
pull_all
COUNT_ROOTS='for j in "$1"/*/; do [ -d "$j/out" ] && printf "%s %s\n" "$(basename "$j")" "$(find "$j/out" -type f | wc -l)"; done'
jl=$(on_box "$COUNT_ROOTS" 300 2>/dev/null)
if [ -z "$jl" ] && ! on_box 'true' 60 >/dev/null 2>&1; then
  say "=== box $N unreachable for the final count -- marker NOT dropped ==="
  exit 1
fi
behind=0
while read -r j rn; do
  [ -n "$j" ] || continue
  # Superset rule: local >= remote, counted under out/.  Never demand equality.
  ln=$(find "$Q_ROOT/cache/$j" -type f 2>/dev/null | wc -l)
  if [ "$ln" -ge "$rn" ]; then say "  OK      $j local=$ln >= remote_out=$rn"
  else say "  BEHIND  $j local=$ln <  remote_out=$rn"; behind=$((behind+1)); fi
done <<< "$jl"

if [ "$behind" -eq 0 ]; then
  say "=== ALL JOB ROOTS VERIFIED local >= remote at out/ level ==="
  : > "$FLEET_STATE/no_more_work_box$N"
  say "dropped $FLEET_STATE/no_more_work_box$N -- destroy_gate_worker.sh owns the destroy"
else
  say "=== $behind JOB ROOT(S) STILL BEHIND -- marker NOT dropped, box $N stays alive ==="
  say "    Never delete without a verified copy elsewhere. Manual attention needed."
fi
say "=== HARD STOP COMPLETE ==="
