#!/usr/bin/env bash
# TEST 1 — local smoke: 3 tasks; task 1 blows its --mem cap (RSS kill + requeue),
# task 2 already has $OUT/_rc.txt == 0 (skip-resume), task 0 runs normally.
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
Q=$HERE/bin
export Q_CONFIG=/dev/null        # offline test: never read a real config.env
export Q_ROOT=$HERE/tests/run_local
rm -rf "$Q_ROOT"; mkdir -p "$Q_ROOT"
# test-only capacity overrides (the box is at ~1.5 GB MemAvailable right now)
export Q_CYCLE_SEC=6 Q_RSS_POLL_SEC=2 Q_LOCAL_RESERVE_PCT=0 Q_LOCAL_RESERVE_MB=256 \
       Q_LOCAL_RESERVE_THREADS=2 Q_LOCAL_MAX_TASKS=3
cat > "$Q_ROOT/lanes.cmds" <<'EOF'
echo hello-0 > "$OUT/data.txt"; sleep 3

printf 'x' > "$OUT/started.txt"; python3 -c "import time; b=bytearray(160*1024*1024); time.sleep(300)"
echo hello-2 > "$OUT/data.txt"
EOF
# pre-complete task 2 so the resume rule has something to skip
mkdir -p "$Q_ROOT/local_out/t1local/task_2"
echo PRESEEDED > "$Q_ROOT/local_out/t1local/task_2/data.txt"
echo 0 > "$Q_ROOT/local_out/t1local/task_2/_rc.txt"
$Q/q submit --name t1local --cmds "$Q_ROOT/lanes.cmds" --site local \
    --cpus 1 --threads 1 --mem 64M --priority 5
echo "--- running qd for 6 short cycles ---"
$Q/qd --cycles 6
echo "=== q status ==="
$Q/q status t1local
echo "=== ASSERTIONS ==="
python3 - "$Q_ROOT" <<'PY'
import json,os,sys
r=sys.argv[1]; st=json.load(open(os.path.join(r,"state","t1local.json")))
t=st["tasks"]; ok=True
def chk(c,msg):
    global ok
    print(("PASS " if c else "FAIL ")+msg); ok = ok and c
chk(st["ntasks"]==3, "3 tasks (blank line in lanes.cmds dropped at submit)")
chk(t["0"]["state"]=="DONE" and t["0"]["rc"]==0, "task 0 DONE rc=0")
chk(open(os.path.join(r,"local_out","t1local","task_0","data.txt")).read().strip()=="hello-0",
    "task 0 wrote $OUT/data.txt")
chk(t["1"]["mem_kills"]>=1, "task 1 RSS-killed at least once (mem_kills=%s)"%t["1"]["mem_kills"])
chk(t["1"]["attempts"]>=2, "task 1 REQUEUED and re-run (attempts=%s)"%t["1"]["attempts"])
chk(t["1"]["state"]=="FAILED", "task 1 FAILED after Q_MAX_MEM_KILLS")
chk(t["1"]["rss_peak_mib"]>64, "watchdog saw peak RSS %sMiB over the 64MiB cap"%t["1"]["rss_peak_mib"])
chk(t["2"]["state"]=="DONE" and "resume" in t["2"].get("reason",""), "task 2 skip-resumed")
chk(open(os.path.join(r,"local_out","t1local","task_2","data.txt")).read().strip()=="PRESEEDED",
    "task 2 output untouched (it was NOT re-run)")
chk(not os.path.exists(os.path.join(r,"local_out","t1local","task_2","_cmd.txt")),
    "task 2 never launched (_cmd.txt absent)")
chk(st["state"]=="FAILED", "job FAILED overall (1 of 3 tasks failed) — reported, not hidden")
# NB: never `pgrep -f` here — it matches the invoking shell's own cmdline.
# Check the recorded process GROUP instead: exact, and immune to name matching.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(r))), "lib"))
import qcommon as QQ
pg=t["1"]["pgid"]; tab=QQ.proc_table()
chk(not any(p==pg for (p,_b) in tab.values()),
    "killed process GROUP %d is gone (no orphan memory hog)"%pg)
print("RESULT:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
PY
