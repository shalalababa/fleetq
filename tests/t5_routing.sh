#!/usr/bin/env bash
# TEST 5 — `--site either` routing.  Small job (< 4 tasks) -> local.  Big job ->
# the first available site in Q_EITHER_ORDER.  Batch and vast are both mocked
# (Q_DRY_RUN) as having room; local tasks really run.
#
# Adding the vast site CHANGED this contract.  The default order is now
# local,vast,batch — rented boxes are paid for whether or not we use them,
# while Batch costs per use.  The ORIGINAL contract (big -> batch) is still
# asserted below, under an explicit Q_EITHER_ORDER=local,batch,vast, so the old
# behaviour stays tested rather than being deleted to make the suite green.
# One FAKE box is registered (TEST-NET address); Q_DRY_RUN means no ssh is made.
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
Q=$HERE/bin
export Q_CONFIG=/dev/null        # offline test: never read a real config.env
export Q_ROOT=$HERE/tests/run_route
rm -rf "$Q_ROOT"; mkdir -p "$Q_ROOT"
export Q_DRY_RUN=1 Q_CYCLE_SEC=3 Q_RSS_POLL_SEC=1 Q_LOCAL_RESERVE_PCT=0 Q_LOCAL_RESERVE_MB=256
export Q_VAST_INSTANCES="$Q_ROOT/boxes.env" Q_EITHER_ORDER=local,vast,batch
printf 'export IP1=192.0.2.10\nexport PORT1=22\n' > "$Q_VAST_INSTANCES"
echo '[]' > "$Q_ROOT/empty.json"
python3 -c "print('echo hi > \"\$OUT/x.txt\"\n'*2, end='')" > "$Q_ROOT/small.cmds"
python3 -c "print('echo hi > \"\$OUT/x.txt\"\n'*6, end='')" > "$Q_ROOT/big.cmds"
$Q/q submit --name qroute-small --cmds "$Q_ROOT/small.cmds" --site either --cpus 1 --threads 1 --mem 64M >/dev/null
$Q/q submit --name qroute-big   --cmds "$Q_ROOT/big.cmds"   --site either --cpus 2 --threads 1 --mem 64M >/dev/null
Q_FAKE_LIST="$Q_ROOT/empty.json" $Q/qd --cycles 2 >/dev/null
echo "=== ASSERTIONS ==="
python3 - "$Q_ROOT" <<'PY'
import json,os,sys
r=sys.argv[1]; ok=True
def chk(c,m):
    global ok; print(("PASS " if c else "FAIL ")+m); ok=ok and c
a=json.load(open(os.path.join(r,"state","qroute-small.json")))
b=json.load(open(os.path.join(r,"state","qroute-big.json")))
chk(a["site"]=="local","2-task either job went LOCAL (site=%s)"%a["site"])
chk(a["state"]=="DONE","and finished DONE")
chk(b["site"]=="vast","6-task either job went VAST under the default order (site=%s)"%b["site"])
# Q_DRY_RUN cannot produce real $OUT/_rc.txt files, so a vast job can never be
# seen to finish here and MAY re-plan the remainder into a further chunk. That is
# the dry-run harness, not a routing defect: what this test owns is WHERE the job
# went and that the FIRST chunk covered the whole job. t6_vast.sh is the one that
# proves completion, against real outputs on a real box.
chk(bool(b["chunks"]) and b["chunks"][0]["indices"]==list(range(6)),
    "first chunk covers all 6 tasks (%s)"%(b["chunks"][0]["indices"] if b["chunks"] else None))
print("RESULT:","ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
PY

# ---- phase 2: the ORIGINAL contract still holds when asked for explicitly ----
export Q_ROOT=$HERE/tests/run_route2
rm -rf "$Q_ROOT"; mkdir -p "$Q_ROOT"
echo '[]' > "$Q_ROOT/empty.json"
python3 -c "print('echo hi > \"\$OUT/x.txt\"\n'*6, end='')" > "$Q_ROOT/big.cmds"
$Q/q submit --name qroute-big2 --cmds "$Q_ROOT/big.cmds" --site either --cpus 2 --threads 1 --mem 64M >/dev/null
Q_EITHER_ORDER=local,batch,vast Q_FAKE_LIST="$Q_ROOT/empty.json" $Q/qd --cycles 2 >/dev/null
python3 - "$Q_ROOT" <<'PY2'
import json,os,sys
r=sys.argv[1]
b=json.load(open(os.path.join(r,"state","qroute-big2.json")))
ok = b.get("site")=="batch"
print(("PASS " if ok else "FAIL ")+"6-task either job went BATCH under Q_EITHER_ORDER=local,batch,vast (site=%s)"%b.get("site"))
print("RESULT PHASE2:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
PY2
