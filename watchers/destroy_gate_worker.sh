#!/usr/bin/env bash
# Verified auto-destroy for rented boxes running queue work.
#
#   watchers/destroy_gate_worker.sh N [N...] [--procs REGEX] [--force-unsafe]
#
# Destroy is irreversible and deletes every run dir on the box, so the gate is
# the whole point.  Per box, independently, ALL of these must pass:
#
#   0. $FLEET_STATE/no_more_work_box<N> exists -- someone (a person, or
#      deadline_worker.sh after its final verified pull) declared the work done;
#   1. every queue job placed on box N is terminal (DONE/FAILED/CANCELLED);
#   2. no science process is still running on the box;
#   3. every job tree on the box is fully pulled: local file count >= remote
#      file count under out/, compared as a SUPERSET.
#
# Gate 3 once counted `_rc.txt` files instead, which was a data-loss path:
# _rc.txt is written only when a cell COMPLETES, so a cell cut mid-run has none
# but does have its resumable checkpoint, and a job whose cells were all cut
# was skipped with no check at all.  Counting every file under out/ closes it.
#
# Then it calls `fleet destroy N --yes`, which runs its own archive gate for
# fleet run dirs.  If that gate refuses, the refusal stands, the box stays
# alive and the log says why.
#
# --force-unsafe overrides that archive gate (and ONLY that gate; the four
# above still have to pass).  Use it when this worker's own checks are the
# right verification and `fleet destroy`'s archive comparison looks in the
# wrong place -- e.g. results came home through the queue's GCS relay into
# $Q_ROOT/cache, not into FLEET_ARCHIVE_ROOT, so the archive gate would report
# every run dir as unarchived.  The worker answers both typed confirmations
# (DESTROY UNARCHIVED, then the vast id) only after its own gates pass.
#
# Run detached so the release does not depend on a session staying alive (an
# idle paid box is the most expensive failure in the system):
#   setsid nohup watchers/destroy_gate_worker.sh 5 6 >/dev/null 2>&1 &
set -uo pipefail
FLEET_HOME="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
source "$FLEET_HOME/fleet/common.sh"

BOXES=(); FORCE=0
PROCS="${FLEET_SCIENCE_PROCS:-[t]rain\.py|[b]enchmark\.py|[r]un\.py}"
while [ $# -gt 0 ]; do
  case "$1" in
    --procs)        PROCS="${2:?}"; shift 2 ;;
    --force-unsafe) FORCE=1; shift ;;
    *)              BOXES+=("$1"); shift ;;
  esac
done
[ ${#BOXES[@]} -gt 0 ] || die "usage: destroy_gate_worker.sh N [N...] [--procs REGEX] [--force-unsafe]"

Q="$FLEET_HOME/bin/q"
FLEET="$FLEET_HOME/bin/fleet"
RROOT="${Q_VAST_REMOTE_ROOT:-/workspace/qvast}"
POLL="${FLEET_WATCH_POLL_SEC:-120}"
mkdir -p "$Q_ROOT/logs" "$Q_ROOT/spool/tmp" "$FLEET_STATE"
exec 9>"$Q_ROOT/spool/tmp/.destroygate.lock"
flock -n 9 || { echo "another destroy gate holds the lock"; exit 0; }
LOG="$Q_ROOT/logs/destroy_gate.log"
say(){ printf '%s %s\n' "$(date '+%F %T %z')" "$*" >> "$LOG"; }
say "DESTROY GATE ARMED pid=$$ boxes=${BOXES[*]} force_unsafe=$FORCE (requires $FLEET_STATE/no_more_work_box<N>)"

# Child bash: timeout cannot run a shell function, and fl_require_instance
# calls die(), which must not end the worker.
on_box(){ timeout "${3:-120}" bash -c 'source "$1/fleet/common.sh"; fl_ssh_script "$2" "$3" "$4" "$5"' \
            _ "$FLEET_HOME" "$1" "$2" "$RROOT" "$PROCS" 2>/dev/null; }

vast_id(){
  python3 - "$FLEET_STATE/instances.json" "$1" <<'PY' 2>/dev/null
import json, sys
for i in json.load(open(sys.argv[1])).get("instances", []):
    if int(i.get("index", -1)) == int(sys.argv[2]):
        print(i.get("vast_id", "")); break
PY
}

COUNT_PROCS='ps -eo stat=,args= | RE="$2" awk '"'"'$1 !~ /^T/ && $2 != "flock" && $0 ~ ENVIRON["RE"] {k++} END{print k+0}'"'"
COUNT_ROOTS='found=0; for j in "$1"/*/; do [ -d "$j/out" ] || continue; found=1; files=$(find "$j/out" -type f -printf .) || exit 1; printf "%s %s\n" "$(basename "$j")" "${#files}"; done; [ "$found" -eq 1 ] || exit 1; echo END'

check_box(){   # echoes PASS or the first failing reason
  local n=$1 j st bx procs roots rn ln q_list
  [ -f "$FLEET_STATE/no_more_work_box$n" ] ||
    { echo "no no_more_work_box$n marker (work not declared finished)"; return; }
  # (1) every queue job placed on this box is terminal
  q_list=$(timeout 90 "$Q" list 2>/dev/null) ||
    { echo "queue listing failed -- refusing"; return; }
  if printf '%s\n' "$q_list" | awk '$2=="BROKEN"{bad=1} END{exit !bad}'; then
    echo "queue has unreadable state -- refusing"; return
  fi
  while read -r j st; do
    [ -n "$j" ] || continue
    case "$st" in DONE|FAILED|CANCELLED) continue ;; esac
    bx=$(python3 - "$Q_ROOT/state/$j.json" <<'PY' 2>/dev/null
import json, sys
d = json.load(open(sys.argv[1]))
print(d["spec"].get("vast_box") or d.get("pending_box") or "")
PY
) || { echo "cannot read job $j state -- refusing"; return; }
    [ "$bx" = "$n" ] && { echo "job $j still $st on box $n"; return; }
  done < <(printf '%s\n' "$q_list" | awk '$3=="vast"{print $1" "$2}')
  # (2) no science processes on the box
  procs=$(on_box "$n" "$COUNT_PROCS") ||
    { echo "box $n unreachable while counting processes -- refusing"; return; }
  procs=${procs//[[:space:]]/}
  [[ "$procs" =~ ^[0-9]+$ ]] || { echo "box $n returned invalid process count -- refusing"; return; }
  [ "$procs" -gt 0 ] && { echo "box $n still has $procs science process(es)"; return; }
  # (3) every job tree fully pulled, superset rule at out/ level
  roots=$(on_box "$n" "$COUNT_ROOTS" 300) ||
    { echo "box $n unreachable while counting files -- refusing"; return; }
  [ "${roots##*$'\n'}" = END ] ||
    { echo "box $n returned incomplete file inventory -- refusing"; return; }
  while read -r j rn; do
    [ -n "$j" ] && [ "$j" != END ] || continue
    [[ "$rn" =~ ^[0-9]+$ ]] || { echo "box $n returned invalid file count -- refusing"; return; }
    [ "$rn" -eq 0 ] && continue
    ln=$(find "$Q_ROOT/cache/$j" -type f 2>/dev/null | wc -l)
    [ "$ln" -lt "$rn" ] && { echo "job $j NOT fully pulled from box $n (local $ln < remote $rn files under out/)"; return; }
  done <<< "$roots"
  echo PASS
}

while :; do
  alldone=1
  for N in "${BOXES[@]}"; do
    [ -f "$Q_ROOT/spool/tmp/destroyed_box$N" ] && continue
    [ -f "$Q_ROOT/spool/tmp/destroy_refused_box$N" ] && continue
    alldone=0
    r=$(check_box "$N")
    if [ "$r" != "PASS" ]; then say "box $N HOLD: $r"; continue; fi
    vid=$(vast_id "$N")
    if [ -z "$vid" ]; then
      say "box $N: all gates PASS but no vast_id in $FLEET_STATE/instances.json -- run 'fleet refresh'"
      continue
    fi
    say "box $N: all four gates PASS; destroying vast id $vid"
    if [ "$FORCE" -eq 1 ]; then
      # Report first, to learn which prompts the destroy will ask.
      rep=$(timeout 900 "$FLEET" destroy "$N" 2>&1)
      printf '%s\n' "$rep" | tail -15 >> "$LOG"
      if printf '%s' "$rep" | grep -q 'exist ONLY on this box'; then
        say "box $N: archive gate reports unarchived dirs; overriding (--force-unsafe)"
        ack=$(printf 'DESTROY UNARCHIVED\n%s\n' "$vid")
      else
        ack=$(printf '%s\n' "$vid")
      fi
      out=$(printf '%s' "$ack" | timeout 900 "$FLEET" destroy "$N" --force-unsafe 2>&1)
    else
      out=$(printf '%s\n' "$vid" | timeout 900 "$FLEET" destroy "$N" --yes 2>&1)
    fi
    printf '%s\n' "$out" | tail -25 >> "$LOG"
    if printf '%s' "$out" | grep -q '^  destroyed\.'; then
      touch "$Q_ROOT/spool/tmp/destroyed_box$N"; say "box $N DESTROYED (vast id $vid)"
    else
      touch "$Q_ROOT/spool/tmp/destroy_refused_box$N"
      say "box $N destroy did NOT confirm -- box still alive, needs a look (output above)"
    fi
  done
  [ "$alldone" = 1 ] && { say "every box handled; gate exiting"; exit 0; }
  sleep "$POLL"
done
