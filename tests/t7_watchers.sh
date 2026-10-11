#!/usr/bin/env bash
# TEST 7 — watchers, offline.  A fake `ssh` on PATH runs the "remote" script
# locally, so a temp dir stands in for the box.  Checks:
#   gpu_box_watch     flags a non-zero _rc.txt
#   deadline_worker   SIGTERMs the payload, verifies the pull, drops the marker
#   destroy_gate      HOLDs while a remote file is unpulled; once all gates pass
#                     it calls `fleet destroy`, and a refusal there leaves the
#                     box alive; with --force-unsafe it answers both typed
#                     confirmations and the (fake) vastai destroy runs
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
T=$HERE/tests/run_watchers
rm -rf "$T"; mkdir -p "$T/bin" "$T/root/fleet_state" "$T/root/spool/pending" "$T/root/state" "$T/remote/jobA/out/task_0" \
                      "$T/remote/jobA/out/task_1" "$T/root/cache/jobA/task_0" "$T/archive"
# fake ssh: run the remote command locally, with the box env file redirected
# to a test copy so box-side helpers (lanes.sh) load against the temp tree
cat > "$T/bin/ssh" <<EOF
#!/usr/bin/env bash
for a; do last=\$a; done
script=\$(cat)
if [ "\${FLEET_TEST_BAD_BUSY:-0}" = 1 ] && [[ "\$script" == *'queues='* ]]; then
  echo 'bad busy inventory'; exit 0
fi
if [ "\${FLEET_TEST_EMPTY_COUNT:-0}" = 1 ] && [[ "\$script" == *'find "\$j/out"'* ]]; then
  exit 0
fi
printf '%s\n' "\$script" | sed "s|/root/.fleet_env|$T/fleet_env|g" | bash -c "\$last"
EOF
printf 'export REPO=%s RUNROOT=%s FLEET_BOX_HOME=%s\n' "$T" "$T/runroot" "$HERE/box" > "$T/fleet_env"
mkdir -p "$T/runroot/run_a"; echo x > "$T/runroot/run_a/f.txt"   # a run dir NOT in the archive
printf '#!/bin/sh\necho "FAKE VASTAI CALLED $*" >> "%s/vastai_calls"\n' "$T" > "$T/bin/vastai"
# The fake box has no GPU work. Do not read the controller's nvidia-smi output.
printf '#!/bin/sh\ncase "$*" in *compute-apps*) ;; *) echo 0 ;; esac\n' > "$T/bin/nvidia-smi"
chmod +x "$T/bin/ssh" "$T/bin/vastai" "$T/bin/nvidia-smi"
echo 'export IP9=127.0.0.1; export PORT9=22' > "$T/root/fleet_state/instances.env"
echo '{"instances":[{"index":9,"vast_id":"12345"}]}' > "$T/root/fleet_state/instances.json"
printf 'Q_VAST_REMOTE_ROOT=%s\nFLEET_ARCHIVE_ROOT=%s\n' "$T/remote" "$T/archive" > "$T/root/config.env"
echo 0   > "$T/remote/jobA/out/task_0/_rc.txt"
echo 127 > "$T/remote/jobA/out/task_1/_rc.txt"
cp "$T/remote/jobA/out/task_0/_rc.txt" "$T/root/cache/jobA/task_0/"
export PATH="$T/bin:$PATH" Q_ROOT="$T/root" Q_CONFIG="$T/root/config.env" \
       FLEET_WATCH_POLL_SEC=1 FLEET_CANCEL_SETTLE_SEC=1
fails=0
check () { if eval "$2"; then echo "ok    $1"; else echo "FAIL  $1"; fails=$((fails+1)); fi; }

# --- gpu_box_watch -----------------------------------------------------------
bash "$HERE/watchers/gpu_box_watch.sh" 9 --until "$(date -d '+2 sec' '+%F %T')"
check "gpu_box_watch flags rc=127" "grep -q 'NON-ZERO rc' '$T/root/logs/gpu_box_watch.log'"

# --- deadline_worker -----------------------------------------------------------
cp "$T/remote/jobA/out/task_1/_rc.txt" "$T/root/cache/jobA/task_0/rc1"   # local now >= remote
(exec -a fleetq_t7_payload sleep 120) & PAYLOAD=$!
now=$(date '+%F %T')
bash "$HERE/watchers/deadline_worker.sh" 9 --soft "$now" --hard "$now" --procs '[f]leetq_t7_payload'
sleep 1
check "deadline_worker stopped the payload" "! kill -0 $PAYLOAD 2>/dev/null"
check "deadline_worker dropped the marker" "[ -f '$T/root/fleet_state/no_more_work_box9' ]"
kill "$PAYLOAD" 2>/dev/null
rm -f "$T/root/fleet_state/no_more_work_box9"
FLEET_TEST_EMPTY_COUNT=1 bash "$HERE/watchers/deadline_worker.sh" 9 --soft "$now" --hard "$now" --procs '[n]o_such_proc_t7'
check "deadline_worker rejects empty final inventory" "[ ! -f '$T/root/fleet_state/no_more_work_box9' ] && grep -q 'final count incomplete' '$T/root/logs/deadline_box9.log'"
touch "$T/root/fleet_state/no_more_work_box9"

# --- destroy_gate_worker -------------------------------------------------------
echo extra > "$T/remote/jobA/out/task_0/extra.bin"
timeout 4 bash "$HERE/watchers/destroy_gate_worker.sh" 9 --procs '[n]o_such_proc_t7'
check "destroy_gate HOLDs on an unpulled file" "grep -q 'NOT fully pulled' '$T/root/logs/destroy_gate.log'"
cp "$T/remote/jobA/out/task_0/extra.bin" "$T/root/cache/jobA/task_0/"
mkdir -p "$T/root/state"
echo '{broken' > "$T/root/state/unreadable.json"
timeout 4 bash "$HERE/watchers/destroy_gate_worker.sh" 9 --procs '[n]o_such_proc_t7'
check "destroy_gate HOLDs on unreadable queue state" "grep -q 'queue has unreadable state' '$T/root/logs/destroy_gate.log' && [ ! -f '$T/vastai_calls' ]"
rm -f "$T/root/state/unreadable.json"
mv "$T/remote/jobA/out" "$T/remote/jobA/saved_out"
timeout 4 bash "$HERE/watchers/destroy_gate_worker.sh" 9 --procs '[n]o_such_proc_t7'
check "destroy_gate HOLDs on empty remote inventory" "grep -q 'unreachable while counting files' '$T/root/logs/destroy_gate.log' && [ ! -f '$T/vastai_calls' ]"
mv "$T/remote/jobA/saved_out" "$T/remote/jobA/out"
timeout 60 bash "$HERE/watchers/destroy_gate_worker.sh" 9 --procs '[n]o_such_proc_t7'
check "destroy_gate passes its own gates" "grep -q 'all four gates PASS' '$T/root/logs/destroy_gate.log'"
check "fleet destroy refusal leaves box alive" "[ -f '$T/root/spool/tmp/destroy_refused_box9' ]"
check "vastai was never called" "[ ! -f '$T/vastai_calls' ]"
printf 'DESTROY UNARCHIVED\n12345\n' | FLEET_TEST_BAD_BUSY=1 timeout 60 "$HERE/bin/fleet" destroy 9 --force-unsafe > "$T/bad_busy.log" 2>&1
check "fleet destroy rejects malformed work inventory" "grep -q 'invalid work-in-flight inventory' '$T/bad_busy.log' && [ ! -f '$T/vastai_calls' ]"

rm -f "$T/root/spool/tmp/destroy_refused_box9"
timeout 60 bash "$HERE/watchers/destroy_gate_worker.sh" 9 --procs '[n]o_such_proc_t7' --force-unsafe
check "--force-unsafe overrides the archive gate" "grep -q 'overriding (--force-unsafe)' '$T/root/logs/destroy_gate.log'"
check "--force-unsafe destroy ran" "grep -q 'destroy instance 12345' '$T/vastai_calls' && [ -f '$T/root/spool/tmp/destroyed_box9' ]"

echo "watchers: $fails failure(s)"
[ "$fails" -eq 0 ]
