#!/usr/bin/env bash
# TEST 2 assertions — run after tests/t2_batch.sh has had time to land.
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
Q=$HERE/bin
if [ "${FLEETQ_CLOUD_TESTS:-0}" != 1 ]; then
  echo "SKIP: $(basename "$0") needs real cloud credentials and a filled-in config.env"
  echo "      (set FLEETQ_CLOUD_TESTS=1 to run it; it may create billable resources)"
  exit 77
fi
export Q_ROOT=$HERE/tests/run_batch
DEST=$Q_ROOT/pulled
rm -rf "$DEST"
$Q/q status qtest-batch1
echo "=== q pull ==="
$Q/q pull qtest-batch1 "$DEST"
echo "=== ASSERTIONS ==="
python3 - "$Q_ROOT" "$DEST" "$HERE" <<'PY'
import json,os,re,subprocess,sys
r,dest,code=sys.argv[1],sys.argv[2],sys.argv[3]; ok=True
def chk(c,m):
    global ok; print(("PASS " if c else "FAIL ")+m); ok = ok and c
st=json.load(open(os.path.join(r,"state","qtest-batch1.json")))
chk(st["state"]=="DONE","job DONE (state=%s)"%st["state"])
c=st["chunks"][0]
chk(c["state"]=="SUCCEEDED","chunk %s SUCCEEDED %s"%(c["job"],c["counts"]))
chk(c["counts"].get("SUCCEEDED")==2,"both tasks SUCCEEDED per the ONE list call")
p=st.get("pull",{})
chk(p.get("files",0)>=8,"daemon pulled %s files into cache/"%p.get("files"))
chk(bool((p.get("witness") or {}).get("sha256")),"witness sha256 recorded: %s"%(p.get("witness") or {}).get("sha256","-")[:16])
chk(p.get("bytes",0)>0,"byte total from file stats (not du -s): %s"%p.get("bytes"))
for i,exp in ((0,"py 4"),(1,"py 6")):
    f=os.path.join(dest,"task_%d"%i,"result.txt")
    txt=open(f).read() if os.path.exists(f) else ""
    chk("task ok" in txt and exp in txt,"q pull delivered task_%d/result.txt (%r)"%(i,exp))
    chk(open(os.path.join(dest,"task_%d"%i,"_rc.txt")).read().strip()=="0",
        "task_%d/_rc.txt == 0 (the resume/completion truth)"%i)
log=open(os.path.join(r,"qd.log")).read().splitlines()
cyc=[i for i,l in enumerate(log) if "CYCLE" in l and "begin" in l]
per=[sum(1 for l in log[i:(cyc[k+1] if k+1<len(cyc) else len(log))] if "GCALL list" in l)
     for k,i in enumerate(cyc)]
chk(max(per)<=1,"at most ONE gcloud list call per cycle over %d cycles (max %d, total %d)"
    %(len(per),max(per),sum(per)))
pulls=[l for l in log if "GCALL pull" in l]
chk(all("-m" not in l.split("argv=")[1].split() for l in pulls),
    "%d pull(s), none used `gsutil -m` (single stream)"%len(pulls))
src=open(os.path.join(code,"lib","site_batch.py")).read()
chk('"-m"' not in src,"no `-m` anywhere in the Batch adapter source")
print("RESULT:","ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
PY
