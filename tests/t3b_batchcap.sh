#!/usr/bin/env bash
# TEST 3b — the Batch half of admission control, against the REAL project
# listing.  Q_DRY_RUN=1 so that even a wrong decision could not submit
# anything; the point is that the daemon decides NOT to.  (When this was first
# run, other waves already held the whole 32-vCPU cap, so the real cap was
# full.  To make the test deterministic the cap is now forced to 0 here.)
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
Q=$HERE/bin
if [ "${FLEETQ_CLOUD_TESTS:-0}" != 1 ]; then
  echo "SKIP: $(basename "$0") needs real cloud credentials and a filled-in config.env"
  echo "      (set FLEETQ_CLOUD_TESTS=1 to run it; it may create billable resources)"
  exit 77
fi
export Q_LOCAL_RESERVE_PCT=0 Q_ROOT=$HERE/tests/run_cap_batch
rm -rf "$Q_ROOT"; mkdir -p "$Q_ROOT"
export Q_DRY_RUN=1 Q_BATCH_CPU_CAP=0     # real list call, mocked submit, no room
echo 'echo hi > "$OUT/x.txt"' > "$Q_ROOT/one.cmds"
$Q/q submit --name qcapb --cmds "$Q_ROOT/one.cmds" --site batch --cpus 4 --mem 8G >/dev/null
$Q/qd --once >/dev/null
$Q/q status qcapb
echo "=== ASSERTIONS ==="
python3 - "$Q_ROOT" <<'PY'
import json,os,re,sys
r=sys.argv[1]; ok=True
def chk(c,m):
    global ok; print(("PASS " if c else "FAIL ")+m); ok=ok and c
st=json.load(open(os.path.join(r,"state","qcapb.json")))
chk(st["state"]=="PENDING","job stayed PENDING (state=%s)"%st["state"])
chk(not st["chunks"],"NOTHING was submitted (%d chunks)"%len(st["chunks"]))
w=[e for e in st["events"] if "WAITING" in e]
chk(bool(w),"an explicit WAITING reason was recorded")
if w:
    print("     "+w[-1])
    m=re.search(r"batch free (-?\d+) vCPU of cap (\d+)", w[-1])
    chk(bool(m),"the reason names free vCPU against the cap")
    if m:
        chk(int(m.group(2))==0,"the forced cap (0) was in force")
        chk(int(m.group(1))<=0,"free vCPU is %s, so qd yields"%m.group(1))
log=open(os.path.join(r,"qd.log")).read()
chk(log.count("GCALL list")==1,"exactly 1 list call in the single cycle")
chk("GCALL submit" not in log,"no submit was even attempted")
print("RESULT:","ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
PY
