#!/usr/bin/env bash
# TEST 2b — the pull half of the Batch path, exercised against REAL GCS data
# without submitting anything: the outputs of an already-complete Batch job
# (T2B_JOB, default canary-full1; 4 tasks) are pulled by the daemon's own
# single-stream code, verified by file count + witness sha, and handed to an
# agent via `q pull`.
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
Q=$HERE/bin
if [ "${FLEETQ_CLOUD_TESTS:-0}" != 1 ]; then
  echo "SKIP: $(basename "$0") needs real cloud credentials and a filled-in config.env"
  echo "      (set FLEETQ_CLOUD_TESTS=1 to run it; it may create billable resources)"
  exit 77
fi
export Q_LOCAL_RESERVE_PCT=0 Q_ROOT=$HERE/tests/run_pull
rm -rf "$Q_ROOT"; mkdir -p "$Q_ROOT/state" "$Q_ROOT/logs"
T2B_JOB=${T2B_JOB:-canary-full1}
python3 - "$Q_ROOT" "$HERE" "$T2B_JOB" <<'PY'
import json, os, sys, time
root, code, job = sys.argv[1], sys.argv[2], sys.argv[3]
os.environ["Q_ROOT"] = root
sys.path.insert(0, os.path.join(code, "lib"))
import qcommon as Q
from site_batch import BatchSite
Q.ensure_tree()
st = {"name": job, "state": "RUNNING", "site": "batch", "ntasks": 4,
      "cmds_path": "-", "priority": 0, "created": "-", "tasks": {}, "chunks": [],
      "retries": 0, "events": [],
      "spec": {"name": job, "site": "batch", "cpus": 2, "mem_mib": 4096,
               "threads": 2, "spot": 1, "resumable": 1, "cmds_src": "(pull-only test)"}}
Q.save_state(st)
b = BatchSite(Q.load_config())
t0 = time.time()
# The pull is ASYNC since the detached-worker refactor: pull() hands back
# "started"/"active" and only reports "done" once the worker's marker lands.
# Polling here is what the daemon's own cycle does.
phase = b.pull(st, force=True)
while phase != "done" and time.time() - t0 < 300:
    time.sleep(2)
    phase = b.pull(st, force=True)
Q.save_state(st)
print("pull took %.1fs (phase=%s)" % (time.time() - t0, phase))
print(json.dumps(st["pull"], indent=1))
PY
echo "=== q pull ==="
$Q/q pull "$T2B_JOB" "$Q_ROOT/dest"
echo "=== ASSERTIONS ==="
python3 - "$Q_ROOT" "$T2B_JOB" <<'PY'
import hashlib, json, os, sys
r, job = sys.argv[1], sys.argv[2]; ok = True
def chk(c, m):
    global ok; print(("PASS " if c else "FAIL ") + m); ok = ok and c
st = json.load(open(os.path.join(r, "state", job + ".json")))
p = st["pull"]
chk(p["files"] >= 16, "daemon pulled %d files from real GCS into cache/" % p["files"])
chk(p["bytes"] > 0, "%d bytes, counted from file stats (never du -s)" % p["bytes"])
w = p["witness"]
chk(bool(w and w["sha256"]), "witness %s = %s" % (w["path"], w["sha256"][:16]))
h = hashlib.sha256(open(os.path.join(r, "cache", job, w["path"]), "rb").read()).hexdigest()
chk(h == w["sha256"], "witness sha recomputes identically")
d = os.path.join(r, "dest")
n = sum(len(f) for _a, _b, f in os.walk(d))
chk(n == p["files"], "`q pull` delivered all %d files with no network call" % n)
rcs = [open(os.path.join(d, t, "_rc.txt")).read().strip()
       for t in sorted(os.listdir(d)) if os.path.isdir(os.path.join(d, t))]
chk(rcs and all(x == "0" for x in rcs), "every task_i/_rc.txt in the pull is 0: %s" % rcs)
log = open(os.path.join(r, "qd.log")).read()
chk("GCALL batch-pull" in log and " -m " not in log, "the pull was single-stream (no gsutil -m)")
print("RESULT:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
PY
