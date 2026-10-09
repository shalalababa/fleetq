#!/usr/bin/env bash
# TEST 4 — correlated-spot-preemption recovery, fully mocked (no VM is created).
# A spot chunk is marked FAILED in a fake `gcloud batch jobs list` payload with
# task 0 already rc=0 in the pulled cache; the daemon must resubmit ONLY task 1,
# ON DEMAND (provisioningModel STANDARD), seeded from the existing $OUT.
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
Q=$HERE/bin
export Q_CONFIG=/dev/null        # offline test: never read a real config.env
export Q_LOCAL_RESERVE_PCT=0 Q_ROOT=$HERE/tests/run_preempt
rm -rf "$Q_ROOT"; mkdir -p "$Q_ROOT"
export Q_DRY_RUN=1 Q_CYCLE_SEC=1
printf 'echo a > "$OUT/a.txt"\necho b > "$OUT/b.txt"\n' > "$Q_ROOT/lanes.cmds"
echo '[]' > "$Q_ROOT/fake_empty.json"
$Q/q submit --name qtest-preempt --cmds "$Q_ROOT/lanes.cmds" --site batch \
    --cpus 1 --mem 1G --spot 1 --resumable 1 > /dev/null
echo "--- cycle 1: admit + submit the SPOT chunk (dry) ---"
Q_FAKE_LIST="$Q_ROOT/fake_empty.json" $Q/qd --once > /dev/null
# ---- simulate: the spot chunk came back FAILED, 1 of 2 tasks survived ----
python3 - "$Q_ROOT" <<'PY'
import json,os,sys
r=sys.argv[1]
json.dump([{"name":"projects/p/locations/us-central1/jobs/qtest-preempt-c0",
            "status":{"state":"FAILED","taskGroups":{"group0":{"counts":
                      {"SUCCEEDED":"1","FAILED":"1"}}}},
            "taskGroups":[{"parallelism":"2","taskCount":"2",
                           "taskSpec":{"computeResource":{"cpuMilli":"1000","memoryMib":"1024"}}}],
            "allocationPolicy":{"instances":[{"policy":{"machineType":"e2-standard-8",
                                "provisioningModel":"SPOT"}}]},
            "createTime":"2026-09-03T00:00:00Z"}],
          open(os.path.join(r,"fake_failed.json"),"w"))
d=os.path.join(r,"cache","qtest-preempt","task_0"); os.makedirs(d,exist_ok=True)
open(os.path.join(d,"_rc.txt"),"w").write("0\n")          # task 0 really finished
open(os.path.join(d,"a.txt"),"w").write("a\n")
PY
echo "--- cycle 2: see FAILED, pull, resubmit the survivor set ON DEMAND ---"
Q_FAKE_LIST="$Q_ROOT/fake_failed.json" $Q/qd --once > /dev/null
$Q/q status qtest-preempt
echo "=== ASSERTIONS ==="
python3 - "$Q_ROOT" <<'PY'
import json,os,sys
r=sys.argv[1]; ok=True
def chk(c,m):
    global ok; print(("PASS " if c else "FAIL ")+m); ok = ok and c
st=json.load(open(os.path.join(r,"state","qtest-preempt.json")))
ch=st["chunks"]
chk(len(ch)==2, "a second chunk was submitted (%d chunks)"%len(ch))
if len(ch)>1:
    c=ch[1]
    chk(c["kind"]=="ondemand", "retry chunk kind=ondemand")
    chk(c["spot"] is False, "retry chunk is NOT spot")
    chk(c["indices"]==[1], "retry covers ONLY the missing task (%s), not task 0"%c["indices"])
    cfg=json.load(open(os.path.join(r,"stage","qtest-preempt","chunks",c["tag"]+".json")))
    pm=cfg["allocationPolicy"]["instances"][0]["policy"]["provisioningModel"]
    chk(pm=="STANDARD", "generated job JSON says provisioningModel=%s"%pm)
    run=cfg["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1]
    chk('_rc.txt' in run and 'SKIP task' in run,
        "the retry runner SKIPS any task whose $OUT/_rc.txt is already 0 (seeded resume)")
    chk(cfg["allocationPolicy"]["network"]["networkInterfaces"][0]["noExternalIpAddress"] is True,
        "no-external-IP contract preserved on the retry")
chk(st["retries"]==1, "retry counter incremented (%s)"%st["retries"])
chk(any("PREEMPTION" in e for e in st["events"]), "the recovery is logged explicitly")
log=open(os.path.join(r,"qd.log")).read().splitlines()
cyc=[i for i,l in enumerate(log) if "CYCLE" in l and "begin" in l]
for k,i in enumerate(cyc):
    end=cyc[k+1] if k+1<len(cyc) else len(log)
    n=sum(1 for l in log[i:end] if "GCALL list" in l)
    chk(n<=1, "cycle %d made %d list call(s)"%(k+1,n))
sub=[l for l in log if "GCALL submit" in l]
chk(len(sub)==2 and "STANDARD" in sub[1], "2 submits logged, the second STANDARD")
print("RESULT:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
PY
