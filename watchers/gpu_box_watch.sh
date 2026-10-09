#!/usr/bin/env bash
# Box-side truth for queue jobs running on rented boxes.
#
#   watchers/gpu_box_watch.sh N [N...] [--until 'YYYY-MM-DD HH:MM']
#
# The queue's view of a vast job only changes after a chunk is PULLED, so a wave
# whose every cell fails in seconds still reads RUNNING 0/9 until the next pull.
# That once left a GPU box idle for minutes with nine rc=127 cells nobody had
# seen.  This watcher reads $OUT/_rc.txt straight off each box and logs ANY
# non-zero rc the moment it appears, so a fast-failing wave is caught in one
# poll instead of at pull time.
#
# Detached + flock, so it survives the agent session that started it:
#   setsid nohup watchers/gpu_box_watch.sh 5 6 >/dev/null 2>&1 &
# Point whatever wakes you (a log monitor, ntfy) at $Q_ROOT/logs/gpu_box_watch.log.
set -uo pipefail
FLEET_HOME="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
source "$FLEET_HOME/fleet/common.sh"

BOXES=(); UNTIL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --until) UNTIL="${2:?--until needs a time}"; shift 2 ;;
    *)       BOXES+=("$1"); shift ;;
  esac
done
[ ${#BOXES[@]} -gt 0 ] || die "usage: gpu_box_watch.sh N [N...] [--until TIME]"
END=0; [ -n "$UNTIL" ] && END=$(date -d "$UNTIL" +%s)
RROOT="${Q_VAST_REMOTE_ROOT:-/workspace/qvast}"
POLL="${FLEET_WATCH_POLL_SEC:-120}"

mkdir -p "$Q_ROOT/logs" "$Q_ROOT/spool/tmp"
exec 9>"$Q_ROOT/spool/tmp/.gpubox.lock"
flock -n 9 || { echo "another gpu-box watcher holds the lock"; exit 0; }
LOG="$Q_ROOT/logs/gpu_box_watch.log"
say(){ printf '%s %s\n' "$(date '+%F %T %z')" "$*" >> "$LOG"; }
say "GPU BOX WATCH ARMED pid=$$ boxes=${BOXES[*]}${UNTIL:+ until $UNTIL}"

# Runs on the box; $1 = the queue's remote root.  Quoted heredoc: nothing here
# is expanded locally, and fl_ssh_script passes $1 pre-quoted.
RC_SCAN=$(cat <<'EOS'
for j in "$1"/*/; do
  n=$(find "$j" -name _rc.txt 2>/dev/null | wc -l)
  [ "$n" -gt 0 ] || continue
  bad=$(find "$j" -name _rc.txt -exec sh -c 'v=$(tr -d "[:space:]" < "$1"); [ "$v" != 0 ] && echo "$v"' _ {} \; 2>/dev/null | sort | uniq -c | tr '\n' ' ')
  echo "$(basename "$j") rc_files=$n nonzero=[${bad:-none}]"
done
EOS
)

declare -A SEEN
while :; do
  for N in "${BOXES[@]}"; do
    # Child bash: timeout cannot run a shell function, and fl_require_instance
    # calls die(), which must not end the watcher.
    out=$(timeout 150 bash -c 'source "$1/fleet/common.sh"; fl_ssh_script "$2" "$3" "$4"' \
            _ "$FLEET_HOME" "$N" "$RC_SCAN" "$RROOT" 2>/dev/null | grep -E 'rc_files=')
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      key="b$N:${line%% *}"
      if [ "${SEEN[$key]:-}" != "$line" ]; then
        SEEN[$key]="$line"
        case "$line" in
          *"nonzero=[none]"*) say "box$N $line" ;;
          *)                  say "box$N $line  <-- NON-ZERO rc, investigate" ;;
        esac
      fi
    done <<< "$out"
  done
  [ "$END" -gt 0 ] && [ "$(date +%s)" -ge "$END" ] && { say "end time reached; standing down"; break; }
  sleep "$POLL"
done
say "GPU BOX WATCH COMPLETE"
