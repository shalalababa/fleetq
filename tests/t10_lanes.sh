#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
export REPO="$HERE" RUNROOT="$TMP/runs" FLEET_BOX_HOME="$HERE/box"
export FL_ENV_FILE="$TMP/fleet_env" FL_HELPERS_FILE="$HERE/box/lanes.sh"
export FL_QUEUE_IDLE_GRACE_SECONDS=0 FL_NTFY_TOPIC= FL_UTIL_SAMPLE=0
mkdir -p "$TMP/bin"
printf '#!/usr/bin/env bash\nexit 0\n' > "$TMP/bin/nvidia-smi"
chmod +x "$TMP/bin/nvidia-smi"
export PATH="$TMP/bin:$PATH"
mkdir -p "$RUNROOT"
printf 'export REPO=%q RUNROOT=%q FLEET_BOX_HOME=%q FL_ENV_FILE=%q FL_HELPERS_FILE=%q\n' \
  "$REPO" "$RUNROOT" "$FLEET_BOX_HOME" "$FL_ENV_FILE" "$FL_HELPERS_FILE" > "$FL_ENV_FILE"
source "$HERE/box/lanes.sh"
active_qdir () {
  local qc="$RUNROOT/_queue_control/lane_0" qid
  qid="$(cat "$qc/active_queue")"
  printf '%s/%s\n' "$qc" "$qid"
}
wait_for () {
  local f="$1" i
  for i in {1..100}; do [ -e "$f" ] && return 0; sleep .1; done
  echo "timeout waiting for $f" >&2
  return 1
}
cleanup () {
  if [ -f "$RUNROOT/_queue_control/lane_0/active_queue" ]; then
    fl_cancel_lane 0 >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

cat > "$TMP/order.cmds" <<EOF2
# ignored
printf 'one\\n' >> '$TMP/order'
printf 'two\\n' >> '$TMP/order'
EOF2
fl_queue_or_add "$TMP/order.cmds" 0 >/dev/null || exit 1
qdir="$(active_qdir)"
wait_for "$qdir/finished"
[ "$(cat "$TMP/order")" = $'one\ntwo' ]
[ "$(cat "$qdir/next_index")" = 3 ]
[ "$(wc -l < "$qdir/tasks.txt")" = 2 ]

cat > "$TMP/fail.cmds" <<EOF2
printf 'first\\n' >> '$TMP/fail_order'
false
printf 'third\\n' >> '$TMP/fail_order'
EOF2
fl_queue_or_add "$TMP/fail.cmds" 0 >/dev/null || exit 1
qdir="$(active_qdir)"
wait_for "$qdir/aborted"
[ "$(cat "$TMP/fail_order")" = first ]
[ "$(cat "$qdir/next_index")" = 2 ]
[ ! -f "$RUNROOT/_queue_control/lane_0/active_queue" ]

FL_ABORT_ON_FAIL=0 fl_queue_or_add "$TMP/fail.cmds" 0 >/dev/null || exit 1
qdir="$(active_qdir)"
wait_for "$qdir/finished"
[ "$(cat "$TMP/fail_order")" = $'first\nfirst\nthird' ]
[ "$(cat "$qdir/next_index")" = 4 ]

cat > "$TMP/held.cmds" <<EOF2
: > '$TMP/started'; while [ ! -f '$TMP/release' ]; do sleep .1; done; printf 'held\\n' >> '$TMP/append_order'
EOF2
cat > "$TMP/append.cmds" <<EOF2
printf 'appended\\n' >> '$TMP/append_order'
EOF2
fl_queue_or_add "$TMP/held.cmds" 0 >/dev/null || exit 1
qdir="$(active_qdir)"
wait_for "$TMP/started"
if fl_quiesce >"$TMP/quiesce.out" 2>&1; then echo 'quiesce accepted active lane' >&2; exit 1; fi
grep -Fq REFUSED "$TMP/quiesce.out"
fl_queue_or_add "$TMP/append.cmds" 0 >/dev/null || exit 1
: > "$TMP/release"
wait_for "$qdir/finished"
[ "$(cat "$TMP/append_order")" = $'held\nappended' ]
[ "$(cat "$qdir/next_index")" = 3 ]

rm -f "$TMP/started" "$TMP/release"
cat > "$TMP/cancel.cmds" <<EOF2
: > '$TMP/started'; while [ ! -f '$TMP/release' ]; do sleep .1; done; printf 'should-not-run\\n' >> '$TMP/cancel_order'
EOF2
fl_queue_or_add "$TMP/cancel.cmds" 0 >/dev/null || exit 1
qdir="$(active_qdir)"
wait_for "$TMP/started"
fl_cancel_lane 0 >/dev/null
[ -f "$qdir/cancelled" ]
[ ! -e "$TMP/cancel_order" ]
[ ! -f "$RUNROOT/_queue_control/lane_0/active_queue" ]
python3 - "$RUNROOT/_cloud_logs/events.jsonl" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    kinds = [json.loads(line)['kind'] for line in f]
assert 'queue_abort' in kinds, kinds
assert 'queue_done' in kinds, kinds
assert 'task_fail' in kinds, kinds
PY
echo 't10_lanes: order, cursor, abort, continue, append, cancel, quiesce, events OK'
