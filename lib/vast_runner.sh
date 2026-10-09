#!/bin/bash
# ---------------------------------------------------------------------------
# qvast runner — executes a SUBSET of one job's lanes.cmds on a rented box.
#
#   runner.sh <jobdir> <indices-file> <nworkers>
#
# Staged per job as <jobdir>/runner.sh.  Never overwrite a runner.sh that is
# executing: bash reads a script incrementally, so rewriting the file under a
# live wave can corrupt it mid-run.
#
# CONVENTIONS — identical to site_batch/site_local so a job is portable:
#   * task index = PHYSICAL LINE of lanes.cmds (i -> line i+1); q strips blank
#     lines at submit time, so physical == non-blank
#   * $OUT = <jobdir>/out/task_<i>, created before the task runs
#   * $OUT/_cmd.txt, _stdout.txt, _stderr.txt, _rc.txt (atomic rename)
#   * a task whose $OUT/_rc.txt is 0 is NEVER re-run — resume is free
#   * $IN exported for parity with lines that reference it
# Workers pull indices from a shared flock'd cursor, so one slow task cannot
# idle the rest of the chunk.
# ---------------------------------------------------------------------------
set -uo pipefail
JOB="${1:?usage: runner.sh <jobdir> <indices-file> [nworkers]}"
IDXF="${2:?indices file}"
NW="${3:-4}"
CMDS="$JOB/lanes.cmds"
[ -r "$CMDS" ] || { echo "runner: no lanes.cmds in $JOB" >&2; exit 2; }
[ -r "$IDXF" ] || { echo "runner: no indices file $IDXF" >&2; exit 2; }
mapfile -t IDX < "$IDXF"
NI=${#IDX[@]}; [ "$NI" -gt 0 ] || { echo "runner: empty index list" >&2; exit 2; }
export IN="${QVAST_IN:-/workspace/inputs}"
mkdir -p "$JOB/out"
LOG="$JOB/_runner.log"
CUR="$JOB/_cursor"; echo 0 > "$CUR"
: "${QVAST_THREADS:=1}"
export OMP_NUM_THREADS=$QVAST_THREADS MKL_NUM_THREADS=$QVAST_THREADS \
       OPENBLAS_NUM_THREADS=$QVAST_THREADS NUMEXPR_NUM_THREADS=$QVAST_THREADS
echo "$$" > "$JOB/_runner.pid"
echo "[qvast] start job=$JOB n=$NI workers=$NW $(date -Is)" >> "$LOG"

claim() { local k; exec 9>"$CUR.lock"; flock 9; k=$(cat "$CUR"); echo $((k+1)) > "$CUR"; flock -u 9; exec 9>&-; echo "$k"; }

worker() {
  local w=$1 k i out rc line t0
  while :; do
    k=$(claim); [ "$k" -ge "$NI" ] && break
    i="${IDX[$k]}"
    out="$JOB/out/task_$i"
    if [ -f "$out/_rc.txt" ] && [ "$(cat "$out/_rc.txt" 2>/dev/null)" = "0" ]; then
      echo "[qvast w$w] SKIP task_$i (rc=0)" >> "$LOG"; continue; fi
    mkdir -p "$out" || continue
    line=$(sed -n "$((i+1))p" "$CMDS")
    printf '%s\n' "$line" > "$out/_cmd.txt"
    rm -f "$out/_rc.txt"
    export OUT="$out"
    t0=$(date +%s)
    echo "[qvast w$w] START task_$i $(date -Is)" >> "$LOG"
    ( cd "$JOB" && bash -c "$line" ) > "$out/_stdout.txt" 2> "$out/_stderr.txt"
    rc=$?
    printf '%s\n' "$rc" > "$out/_rc.txt.tmp" && mv "$out/_rc.txt.tmp" "$out/_rc.txt"
    echo "[qvast w$w] END task_$i rc=$rc secs=$(( $(date +%s) - t0 ))" >> "$LOG"
  done
}
for w in $(seq 1 "$NW"); do worker "$w" & done
wait
ok=0; bad=0
for i in "${IDX[@]}"; do
  r=$(cat "$JOB/out/task_$i/_rc.txt" 2>/dev/null || echo missing)
  if [ "$r" = "0" ]; then ok=$((ok+1)); else bad=$((bad+1)); fi
done
echo "[qvast] DONE job=$JOB ok=$ok fail=$bad of $NI $(date -Is)" >> "$LOG"
echo "QVAST_CHUNK_TERMINAL ok=$ok fail=$bad of $NI" >> "$LOG"
rm -f "$JOB/_runner.pid"
exit 0
