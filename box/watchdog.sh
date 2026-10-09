#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# fleet box watchdog — runs ON a rented box next to box/lanes.sh
#
# Tier-1 detection: the dumb daemon that makes silence mean something. Runs ON
# the instance, so the machine that fails is the machine that reports; total
# instance death is covered from outside by the missed-heartbeat alert.
#
# It polls STATE conditions only. TRANSITIONS (task failed, queue aborted,
# queue drained) are emitted at their source by lanes.sh, which is
# more reliable than log scraping and needs no polling latency budget.
#
# Watched here:
#   - supervisor DEAD while its lane still claims to be active  (silent stall)
#   - instance fully idle while still billing                   (money)
#   - disk near full                                            (silent loss)
#   - heartbeat to the dead-man's switch
#
# AUTO-MITIGATION is OFF by default and, when on, is limited to actions
# that are identical in intent to what already ran. See wd_auto_restart below.
#
# Usage:  watchdog.sh start | stop | status | once
# ---------------------------------------------------------------------------
set -uo pipefail

source /root/.fleet_env 2>/dev/null || true
: "${RUNROOT:=/workspace/runs}"
: "${FLEET_BOX_HOME:=/root/fleet}"
source "$FLEET_BOX_HOME/lanes.sh" 2>/dev/null || {
  echo "FATAL: cannot source lanes.sh from $FLEET_BOX_HOME" >&2; exit 1; }

WD_DIR="$RUNROOT/_queue_control/_watchdog"
WD_PID="$WD_DIR/watchdog.pid"
WD_LOG="$RUNROOT/_cloud_logs/watchdog.out"
WD_INTERVAL="${FL_WATCHDOG_INTERVAL_SECONDS:-60}"
WD_HEARTBEAT_EVERY="${FL_HEARTBEAT_SECONDS:-300}"
# 10 min, tuned 2026-08-14: instances are kept queued as a matter of habit, so
# an idle one is an anomaly rather than a pause between waves. Effective lag is
# ~15 min once the queue's own 300 s IDLE_GRACE is counted.
WD_IDLE_ALERT_AFTER="${FL_IDLE_ALERT_SECONDS:-600}"
WD_IDLE_RENOTIFY="${FL_IDLE_RENOTIFY_SECONDS:-10800}"   # then every 3 h
# A flat percentage is the wrong unit in general (what matters is time-to-full,
# which depends on the wave's write rate), but it is the right one HERE: 50 GB
# per instance and no wave writes fast enough for 10% headroom to be minutes.
# Do not "improve" this into a projection without re-checking that.
WD_DISK_PCT="${FL_DISK_ALERT_PCT:-90}"
# Night auto-mitigation. OFF unless explicitly enabled per instance.
# The ONLY permitted action is restarting a supervisor that died while its
# queue still has work -- same queue, same tasks, same order, nothing
# re-derived. It never changes a config, never rents, never touches science.
WD_AUTO="${FL_NIGHT_AUTO:-0}"

mkdir -p "$WD_DIR" "$RUNROOT/_cloud_logs"

# --- dedupe -----------------------------------------------------------------
# A watchdog that cries wolf gets ignored, and an ignored watchdog is worse
# than none. Every condition reports once per episode; re-notification is on a
# long timer and always carries the elapsed time so it reads as an escalation
# rather than a repeat.

wd_should_report () {           # key  renotify_seconds
  local key="$1" every="${2:-0}" f="$WD_DIR/reported_$1" last now
  now="$(date +%s)"
  if [ ! -f "$f" ]; then printf '%s\n' "$now" > "$f"; return 0; fi
  [ "$every" -gt 0 ] || return 1
  last="$(cat "$f" 2>/dev/null || echo 0)"
  if [ $(( now - last )) -ge "$every" ]; then printf '%s\n' "$now" > "$f"; return 0; fi
  return 1
}

wd_clear_report () { rm -f "$WD_DIR/reported_$1"; }

wd_episode_start () {           # key -> prints epoch when this episode began
  local f="$WD_DIR/since_$1" now; now="$(date +%s)"
  [ -f "$f" ] || printf '%s\n' "$now" > "$f"
  cat "$f"
}
wd_episode_end () { rm -f "$WD_DIR/since_$1"; }

# --- checks -----------------------------------------------------------------

wd_check_dead_supervisors () {
  local lane gpu active qdir pid nxt total pending
  for lane in "$RUNROOT/_queue_control"/lane_*; do
    [ -f "$lane/active_queue" ] || continue
    gpu="${lane##*/lane_}"
    active="$(cat "$lane/active_queue" 2>/dev/null || true)"
    qdir="$lane/$active"
    pid="$(cat "$qdir/supervisor.pid" 2>/dev/null || true)"
    if fl_alive "$pid"; then
      wd_clear_report "dead_lane_$gpu"; wd_episode_end "dead_lane_$gpu"
      continue
    fi
    # The lane still advertises an active queue but nobody is driving it. This
    # is the failure mode that LOOKS healthy in a casual `fl_status` glance.
    nxt="$(cat "$qdir/next_index" 2>/dev/null || echo 1)"
    total="$(wc -l < "$qdir/tasks.txt" 2>/dev/null || echo 0)"
    pending=$(( total - nxt + 1 )); [ "$pending" -lt 0 ] && pending=0
    # Auto-restart before alerting: if it works, this is a 60-second gap
    # instead of an idle lane until morning, and the digest still records it.
    if [ "$WD_AUTO" = "1" ] && wd_auto_restart "$gpu" "$active" "$qdir" "$pending"; then
      wd_clear_report "dead_lane_$gpu"
      continue
    fi
    if wd_should_report "dead_lane_$gpu" 10800; then
      fl_event supervisor_dead high "lane $gpu supervisor DEAD — $pending stranded" \
        "$(printf 'Lane %s still claims queue %s as active, but its supervisor (pid %s) is gone.\nNothing is running; %s of %s task(s) never started.\nThe lane will not self-recover in this build: requeue after checking what the last task did.' \
            "$gpu" "$active" "${pid:-none}" "$pending" "$total")" \
        "\"lane\":\"$gpu\",\"queue\":\"$(fl_jesc "$active")\",\"stranded\":$pending"
    fi
  done
}

# Relaunch the supervisor for an existing queue. Deliberately narrow:
#   * refuses if the child is still alive -- a supervisor can look dead while
#     its task runs on, and starting a second one would run that task twice
#   * refuses if the queue was aborted or cancelled -- those are decisions,
#     not accidents, and undoing them silently would be worse than the stall
#   * refuses after repeated attempts -- a lane that keeps losing its
#     supervisor has a real problem that restarting will not fix
wd_auto_restart () {
  local gpu="$1" qid="$2" qdir="$3" pending="$4" cpid tries f
  [ "$pending" -gt 0 ] || return 1
  [ -f "$qdir/aborted" ] && return 1
  [ -f "$qdir/cancelled" ] && return 1
  cpid="$(cat "$qdir/child.pid" 2>/dev/null || true)"
  if fl_alive "$cpid"; then
    return 1   # task still running; the supervisor is gone but the work is not
  fi
  f="$WD_DIR/restarts_${gpu}_${qid}"
  tries="$(cat "$f" 2>/dev/null || echo 0)"
  if [ "$tries" -ge 2 ]; then
    return 1   # twice is enough to know restarting is not the answer
  fi
  printf '%s\n' "$(( tries + 1 ))" > "$f"

  local shard; shard="$(cat "$qdir/shard" 2>/dev/null || echo "$gpu")"
  setsid bash -c "source '$FL_ENV_FILE' >/dev/null 2>&1 || true; \
                  source '$FL_HELPERS_FILE'; fl_queue_supervisor '$gpu' '$qid'" \
    >> "$RUNROOT/_cloud_logs/worker_shard${shard}.out" 2>&1 </dev/null &
  local pid="$!"
  sleep 3
  if fl_alive "$pid"; then
    printf '%s\n' "$pid" > "$qdir/supervisor.pid"
    fl_event supervisor_restarted default "lane $gpu supervisor restarted" \
      "$(printf 'Supervisor for queue %s on lane %s had died with %s task(s) pending; restarted it (attempt %s of 2).\nSame queue, same order, nothing re-derived. No task was running at the time.' \
          "$qid" "$gpu" "$pending" "$(( tries + 1 ))")" \
      "\"lane\":\"$gpu\",\"queue\":\"$(fl_jesc "$qid")\",\"attempt\":$(( tries + 1 ))"
    return 0
  fi
  return 1
}

wd_any_active_queue () {
  local lane
  for lane in "$RUNROOT/_queue_control"/lane_*; do
    [ -f "$lane/active_queue" ] && return 0
  done
  return 1
}

wd_gpu_busy () {
  local pids
  pids="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader,nounits 2>/dev/null | tr -d ' \t\r' | awk 'NF')"
  [ -n "$pids" ]
}

wd_check_idle_billing () {
  local since now elapsed ngpu
  if wd_any_active_queue || wd_gpu_busy; then
    wd_episode_end idle; wd_clear_report idle
    return 0
  fi
  since="$(wd_episode_start idle)"
  now="$(date +%s)"; elapsed=$(( now - since ))
  [ "$elapsed" -ge "$WD_IDLE_ALERT_AFTER" ] || return 0
  ngpu="$(nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null | wc -l)"
  if wd_should_report idle "$WD_IDLE_RENOTIFY"; then
    fl_event instance_idle default "instance idle $((elapsed/60))m — billing" \
      "$(printf 'No active queue and no GPU compute for %sm across %s GPU(s).\nThis instance is reserved, so nothing is lost — but it is burning money until it gets work or you destroy it.\nResults are still local to the instance: pull before destroying.' \
          "$((elapsed/60))" "$ngpu")" \
      "\"idle_s\":$elapsed,\"gpus\":$ngpu"
  fi
}

wd_check_disk () {
  local pct
  pct="$(df -P "$RUNROOT" 2>/dev/null | awk 'NR==2{gsub(/%/,"",$5); print $5}')"
  [ -n "$pct" ] || return 0
  if [ "$pct" -ge "$WD_DISK_PCT" ]; then
    if wd_should_report disk 10800; then
      fl_event disk_full high "disk ${pct}% on $(hostname)" \
        "$(printf 'Filesystem holding %s is %s%% full.\nRuns write checkpoints without checking free space; a full disk corrupts them silently rather than failing loudly.\n\n%s' \
            "$RUNROOT" "$pct" "$(df -h "$RUNROOT" 2>/dev/null | tail -n +1)")" \
        "\"pct\":$pct"
    fi
  else
    wd_clear_report disk
  fi
}

wd_heartbeat () {
  [ -n "${FL_HC_URL:-}" ] || return 0
  command -v curl >/dev/null 2>&1 || return 0
  curl -fsS -m 10 --retry 2 "$FL_HC_URL" >/dev/null 2>&1 || true
}

wd_once () {
  wd_check_dead_supervisors
  wd_check_idle_billing
  wd_check_disk
}

# --- daemon -----------------------------------------------------------------

wd_loop () {
  local last_hb=0 now
  fl_event watchdog_up low "watchdog started" \
    "$(printf 'Instance watchdog is up.\nhelpers: %s\ninterval: %ss  heartbeat: %ss\nntfy: %s  healthchecks: %s' \
        "$(fl_version)" "$WD_INTERVAL" "$WD_HEARTBEAT_EVERY" \
        "${FL_NTFY_TOPIC:-DISABLED}" \
        "${FL_HC_URL:+configured}${FL_HC_URL:-not configured (optional)}")"
  while :; do
    wd_once
    now="$(date +%s)"
    if [ $(( now - last_hb )) -ge "$WD_HEARTBEAT_EVERY" ]; then
      wd_heartbeat
      last_hb="$now"
    fi
    sleep "$WD_INTERVAL"
  done
}

# The daemon re-enters this file with `source ... ; wd_loop`, so the dispatch
# below MUST NOT run on source -- otherwise `start` spawns a watchdog that
# sources the file, dispatches `start` again, and forks without bound.
if [ "${BASH_SOURCE[0]}" != "${0}" ]; then
  return 0 2>/dev/null || true
fi

case "${1:-start}" in
  start)
    if [ -f "$WD_PID" ] && fl_alive "$(cat "$WD_PID" 2>/dev/null)"; then
      echo "watchdog already running pid=$(cat "$WD_PID")"; exit 0
    fi
    setsid bash -c "source '$FLEET_BOX_HOME/watchdog.sh' >/dev/null 2>&1; wd_loop" \
      >> "$WD_LOG" 2>&1 </dev/null &
    printf '%s\n' "$!" > "$WD_PID"
    sleep 1
    if fl_alive "$(cat "$WD_PID")"; then
      echo "watchdog started pid=$(cat "$WD_PID") log=$WD_LOG"
    else
      echo "ERROR: watchdog failed to start; see $WD_LOG" >&2; exit 1
    fi
    ;;
  stop)
    if [ -f "$WD_PID" ]; then
      kill -TERM -- "-$(cat "$WD_PID")" 2>/dev/null || kill "$(cat "$WD_PID")" 2>/dev/null || true
      rm -f "$WD_PID"; echo "watchdog stopped"
    else
      echo "watchdog not running"
    fi
    ;;
  status)
    if [ -f "$WD_PID" ] && fl_alive "$(cat "$WD_PID" 2>/dev/null)"; then
      echo "watchdog RUNNING pid=$(cat "$WD_PID")"
    else
      echo "watchdog NOT running"
    fi
    echo "auto-restart: $([ "$WD_AUTO" = 1 ] && echo ENABLED || echo "off (set FL_NIGHT_AUTO=1)")"
    echo "ntfy topic:   ${FL_NTFY_TOPIC:-MISSING (alerts disabled)}"
    echo "healthchecks: ${FL_HC_URL:-not configured (optional)}"
    echo "helpers:      $(fl_version)"
    ;;
  once)  wd_once; echo "one pass complete" ;;
  *)     echo "usage: watchdog.sh start|stop|status|once" >&2; exit 2 ;;
esac
