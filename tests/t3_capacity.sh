#!/usr/bin/env bash
# TEST 3 — admission control: submit more local jobs than fit; they must WAIT and
# be admitted in priority order (never oversubscribe the box).
# Only 2 threads are made available (reserve nproc-2), so 2 of the 4 jobs run.
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
Q=$HERE/bin
export Q_CONFIG=/dev/null        # offline test: never read a real config.env
export Q_ROOT=$HERE/tests/run_cap
rm -rf "$Q_ROOT"; mkdir -p "$Q_ROOT"
export Q_CYCLE_SEC=3 Q_RSS_POLL_SEC=1 Q_LOCAL_RESERVE_PCT=0 Q_LOCAL_RESERVE_MB=256 \
       Q_LOCAL_RESERVE_THREADS=$(( $(nproc) - 2 )) Q_LOCAL_MAX_TASKS=8
[ "$(nproc)" -ge 3 ] || { echo "SKIP: needs >= 3 CPUs"; exit 77; }
echo 'sleep 6; echo done > "$OUT/d.txt"' > "$Q_ROOT/one.cmds"
for p in 9 7 5 1; do
  $Q/q submit --name "qcap-p$p" --cmds "$Q_ROOT/one.cmds" --site local \
      --cpus 1 --threads 1 --mem 64M --priority "$p" > /dev/null
done
echo "submitted 4 single-task local jobs (priorities 9 7 5 1) for 2 free threads"
$Q/qd --cycles 12
echo "=== board ==="; cat "$Q_ROOT/BOARD.txt"
echo "=== ASSERTIONS ==="
python3 - "$Q_ROOT" <<'PY'
import json,os,sys
r=sys.argv[1]; ok=True
def chk(c,m):
    global ok; print(("PASS " if c else "FAIL ")+m); ok = ok and c
S={p:json.load(open(os.path.join(r,"state","qcap-p%d.json"%p))) for p in (9,7,5,1)}
start={p:S[p]["tasks"]["0"].get("started") for p in S}
end={p:S[p]["tasks"]["0"].get("ended") for p in S}
for p in S: chk(S[p]["state"]=="DONE", "qcap-p%d finished DONE"%p)
chk(all(start[p] for p in S), "every job eventually ran (none starved)")
order=sorted(S, key=lambda p: start[p])
chk(order[:2]==[9,7] or order[:2]==[7,9], "the two HIGHEST priorities started first (order %s)"%order)
chk(order[-1]==1, "the LOWEST priority started last")
chk(start[5]>=min(end[9],end[7])-0.5 and start[1]>=min(end[9],end[7])-0.5,
    "the waiting jobs started only after a slot freed (no oversubscription)")
# never more than 2 concurrent: intervals overlapping at any start point
iv=[(start[p],end[p]) for p in S]
mx=max(sum(1 for (s,e) in iv if s<=t<e) for (t,_e) in iv)
chk(mx<=2, "at most %d task(s) ran concurrently on 2 free threads"%mx)
low=[e for e in S[1]["events"] if "WAITING" in e]
chk(bool(low), "the low-priority job logged an explicit WAITING reason")
if low: print("     e.g. "+low[0])
print("RESULT:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
PY
