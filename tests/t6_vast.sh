#!/usr/bin/env bash
# TEST 6 — vast site: 3 tasks dispatched to a rented box's CPU lane.
# Covers: admission by measured load, staging, detached launch (whose ssh rc is
# meaningless), chunk liveness, single-stream pull-back, the rc==0 resume rule,
# and the job reaching DONE from the PULLED _rc.txt files rather than from the
# runner's own log.
#
# TEST-ONLY OVERRIDES, and why:
#  * Q_VAST_CORE_CAP=999 — the real cap is 61.4 and the boxes are deliberately
#    held at ~58-60 by the GPU pilots, so a truthful cap would leave this test
#    WAITING forever on live science load. The three tasks are `echo`s costing
#    milliseconds, so this does not meaningfully perturb the boxes.
#  * Q_VAST_REMOTE_ROOT=/workspace/qvast_t6 — never the production /workspace/qvast.
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
Q=$HERE/bin
if [ "${FLEETQ_CLOUD_TESTS:-0}" != 1 ]; then
  echo "SKIP: $(basename "$0") needs real cloud credentials and a filled-in config.env"
  echo "      (set FLEETQ_CLOUD_TESTS=1 to run it; it may create billable resources)"
  exit 77
fi
export Q_ROOT=$HERE/tests/run_vast
rm -rf "$Q_ROOT"; mkdir -p "$Q_ROOT"
export Q_CYCLE_SEC=6 Q_VAST_CORE_CAP=999 Q_VAST_REMOTE_ROOT=/workspace/qvast_t6 \
       Q_PULL_INTERVAL_SEC=1 Q_LOCAL_RESERVE_PCT=0

# a box must be reachable or there is nothing to test
BOX=${T6_BOX:-1}; export T6_BOX=$BOX
INST=${Q_VAST_INSTANCES:-$HERE/fleet_state/instances.env}
eval "$(grep -E "^export (IP|PORT)${BOX}=" "$INST")"
IP=$(eval echo "\${IP$BOX:-}"); PORT=$(eval echo "\${PORT$BOX:-}")
SSHO="-n -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=20"
ssh $SSHO -p "$PORT" root@"$IP" "rm -rf /workspace/qvast_t6; echo box-reachable" || {
  echo "SKIP: box $BOX unreachable"; exit 77; }

cat > "$Q_ROOT/lanes.cmds" <<'EOF'
echo hello-0 > "$OUT/data.txt"
echo hello-1 > "$OUT/data.txt"
echo hello-2 > "$OUT/data.txt"
EOF
$Q/q submit --name t6vast --cmds "$Q_ROOT/lanes.cmds" --site vast \
    --cpus 1 --threads 1 --mem 64M --priority 5 --vast-box "$BOX"
echo "--- running qd for 8 short cycles ---"
$Q/qd --cycles 8
echo "=== q status ==="
$Q/q status t6vast
echo "=== ASSERTIONS ==="
python3 - "$Q_ROOT" <<'PY'
import json, os, sys
r = sys.argv[1]
st = json.load(open(os.path.join(r, "state", "t6vast.json")))
ok = True
def chk(c, msg):
    global ok
    print(("PASS " if c else "FAIL ") + msg); ok = ok and c
chk(st["ntasks"] == 3, "3 tasks parsed from lanes.cmds")
chk(st.get("site") == "vast", "admitted to the vast site (site=%s)" % st.get("site"))
chk(bool(st.get("chunks")), "at least one chunk recorded")
if st.get("chunks"):
    c = st["chunks"][0]
    chk(c["state"] in ("SUCCEEDED", "RUNNING"), "chunk state %s" % c["state"])
    chk(c["box"] == os.environ.get("T6_BOX", "1"), "chunk landed on the pinned box (box=%s)" % c.get("box"))
cache = os.path.join(r, "cache", "t6vast")
for i in range(3):
    d = os.path.join(cache, "task_%d" % i)
    rc = os.path.join(d, "_rc.txt")
    chk(os.path.exists(rc), "task %d _rc.txt pulled back" % i)
    if os.path.exists(rc):
        chk(open(rc).read().strip() == "0", "task %d rc == 0" % i)
    dat = os.path.join(d, "data.txt")
    chk(os.path.exists(dat) and open(dat).read().strip() == "hello-%d" % i,
        "task %d payload pulled back ($OUT/data.txt)" % i)
    chk(os.path.exists(os.path.join(d, "_cmd.txt")), "task %d _cmd.txt written" % i)
chk(st["state"] == "DONE", "job DONE (state=%s)" % st["state"])
chk(st.get("counts", {}).get("done") == 3, "counts.done == 3 (%s)" % st.get("counts"))
print("RESULT:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
PY
