#!/usr/bin/env bash
# Per-task DELTA pull for a vast job:  vast_task_pull.sh JOB BOX
#
# The stock gcs relay (vast_relay_pull.sh) packs everything new into ONE
# uploadType=media object.  Above a few GB that single upload fails (rc=84), the
# job's pull never completes, and the daemon keeps showing RUNNING 0/N while
# every cell already has rc=0 on the box (seen on a 5.9 GB, 22-cell job).  This
# script ships ONE GCS object per task_N instead, sha-verified per task.
#
# Box -> GCS -> local, 1-hour impersonated token fed over ssh stdin only; the
# box-side helper is STAGED TO A FILE first because stdin is one channel and the
# token owns it (`printf tok | ssh "bash -s" <<HEREDOC` silently gives the
# heredoc the stdin slot and the token is lost).
#
# Idempotent and safe to run mid-job: a task is skipped only when the LOCAL
# file count already >= the REMOTE file count (superset rule -- compare under
# out/ and never demand equality).  Run it by hand, or from a watcher, for jobs
# too large for the single-object relay.
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
QROOT="${Q_ROOT:-$HERE}"
CFG="${Q_CONFIG:-$QROOT/config.env}"; [ -f "$CFG" ] || CFG="$HERE/config.env"
# shellcheck disable=SC1090
[ -f "$CFG" ] && . "$CFG"
JOB=${1:?usage: vast_task_pull.sh JOB BOX}
BOX=${2:?usage: vast_task_pull.sh JOB BOX   (BOX = the N of IP<N>/PORT<N>)}
mkdir -p "$QROOT/spool/tmp"
exec 9>"$QROOT/spool/tmp/.taskpull.$(echo "$JOB" | tr -c "a-zA-Z0-9" _).lock"
flock -n 9 || { echo "another task pull for this job holds the lock"; exit 0; }
BUCKET=${Q_BUCKET:?set Q_BUCKET}
SA=${Q_VAST_GCS_SA:?set Q_VAST_GCS_SA}
GC=$(command -v "${Q_GCLOUD:-gcloud}") || { echo "no gcloud"; exit 2; }
RROOT=${Q_VAST_REMOTE_ROOT:-/workspace/qvast}
INST=${Q_VAST_INSTANCES:-fleet_state/instances.env}; case "$INST" in /*|"~"*) ;; *) INST="$QROOT/$INST";; esac
DEST=$QROOT/cache/$JOB
LOG=$QROOT/logs/$JOB.taskpull.log
mkdir -p "$QROOT/logs" "$DEST"
say(){ printf '%s %s\n' "$(date '+%F %T %z')" "$*" >> "$LOG"; }
eval "$(grep -E "^export (IP|PORT)${BOX}=" "${INST/#\~/$HOME}")"
IP=$(eval echo "\${IP$BOX:-}"); PORT=$(eval echo "\${PORT$BOX:-}")
[ -n "$IP" ] && [ -n "$PORT" ] || { echo "box $BOX not in $INST"; exit 2; }
SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=20 -o ServerAliveInterval=30 -p $PORT ${Q_VAST_USER:-root}@$IP"
say "TASK DELTA PULL START pid=$$ job=$JOB box=$BOX (per-task objects, helper staged as a file)"

# --- stage the box-side helper ONCE, as a file -------------------------------
$SSH "cat > /tmp/vtask_up.sh" <<'REMOTE'
#!/usr/bin/env bash
set -uo pipefail
read -r TOK
[ -n "$TOK" ] || { echo "NO_TOKEN"; exit 21; }
J=$1; T=$2; B=$3; O=$4
[ -n "$J" ] && [ -n "$T" ] || { echo "BAD_ARGS J=$J T=$T"; exit 20; }
ZS=$(command -v zstd 2>/dev/null); [ -x "/venv/main/bin/zstd" ] && ZS=/venv/main/bin/zstd
if [ -n "$ZS" ]; then COMP="$ZS -3 -T4"; else COMP="gzip -1"; fi
R=$5
cd "$R/$J/out" || { echo "NO_OUT_DIR $R/$J/out"; exit 22; }
[ -d "$T" ] || { echo "NO_TASK_DIR $T"; exit 23; }
tar -C . -I "$COMP" -cf "/tmp/vtask_$T.tar.z" "$T" 2>/tmp/vtask_$T.tarerr || { echo "TAR_FAIL $(tail -1 /tmp/vtask_$T.tarerr)"; exit 25; }
code=$(curl -sS --max-time 1800 -o /tmp/vtask_$T.resp -w '%{http_code}' -X POST --data-binary @"/tmp/vtask_$T.tar.z" \
  -K <(printf 'header = "Authorization: Bearer %s"\n' "$TOK") \
  -H 'Content-Type: application/octet-stream' \
  "https://storage.googleapis.com/upload/storage/v1/b/$B/o?uploadType=media&name=$O")
unset TOK
if [ "$code" != "200" ]; then
  echo "HTTP $code $(head -c 300 /tmp/vtask_$T.resp | tr '\n' ' ')"
  rm -f "/tmp/vtask_$T.tar.z" /tmp/vtask_$T.resp; exit 26
fi
echo "SHA $(sha256sum "/tmp/vtask_$T.tar.z" | awk '{print $1}')"
echo "SIZ $(stat -c%s "/tmp/vtask_$T.tar.z")"
rm -f "/tmp/vtask_$T.tar.z" /tmp/vtask_$T.resp /tmp/vtask_$T.tarerr
REMOTE
$SSH -n "test -s /tmp/vtask_up.sh && wc -l < /tmp/vtask_up.sh" >/dev/null 2>&1 \
  || { say "FATAL: could not stage box helper"; exit 3; }
say "box helper staged ok"

tasks=$($SSH -n "ls -1 $RROOT/$JOB/out" 2>/dev/null | grep '^task_' | sort -V)
say "tasks on box: $(echo $tasks | wc -w)"
ok=0; skip=0; fail=0
for t in $tasks; do
  rn=$($SSH -n "find $RROOT/$JOB/out/$t -type f 2>/dev/null | wc -l" 2>/dev/null | tr -d '[:space:]')
  ln=$(find "$DEST/$t" -type f 2>/dev/null | wc -l)
  if [ -n "$rn" ] && [ "$ln" -ge "$rn" ] && [ "$rn" -gt 0 ]; then skip=$((skip+1)); continue; fi
  obj="jobs/$JOB/relay_task/$t.tar.z"
  tok=$("$GC" auth print-access-token --impersonate-service-account="$SA" 2>/dev/null | tail -1)
  [ -n "$tok" ] || { say "$t: could not mint token"; fail=$((fail+1)); continue; }
  # stdin carries the token and NOTHING else; the helper is already on the box
  out=$(printf '%s\n' "$tok" | $SSH "bash /tmp/vtask_up.sh '$JOB' '$t' '$BUCKET' '$obj' '$RROOT'" 2>&1)
  tok=""; unset tok
  sha=$(printf '%s\n' "$out" | awk '/^SHA /{print $2}')
  siz=$(printf '%s\n' "$out" | awk '/^SIZ /{print $2}')
  if [ -z "$sha" ]; then
    say "$t: box-side FAILED :: $(printf '%s' "$out" | grep -vi 'have fun\|welcome to vast' | tail -2 | tr '\n' ' ')"
    fail=$((fail+1)); continue
  fi
  tmp=$(mktemp -d); mkdir -p "$DEST"
  if ! timeout 1800 "$GC" storage cp "gs://$BUCKET/$obj" "$tmp/$t.tar.z" >/dev/null 2>&1; then
    say "$t: download failed"; rm -rf "$tmp"; fail=$((fail+1)); continue; fi
  got=$(sha256sum "$tmp/$t.tar.z" | awk '{print $1}')
  if [ "$got" != "$sha" ]; then say "$t: SHA MISMATCH box=$sha local=$got"; rm -rf "$tmp"; fail=$((fail+1)); continue; fi
  if tar -C "$DEST" -xf "$tmp/$t.tar.z"; then
    ok=$((ok+1)); say "$t pulled ok ($(numfmt --to=iec "${siz:-0}") compressed, sha verified)"
  else
    fail=$((fail+1)); say "$t: untar failed"
  fi
  rm -rf "$tmp"
  timeout 120 "$GC" storage rm "gs://$BUCKET/$obj" >/dev/null 2>&1 || true
done
say "TASK DELTA PULL DONE ok=$ok skipped=$skip failed=$fail"
say "local rc files: $(find $DEST -name _rc.txt | wc -l)  local files: $(find $DEST -type f | wc -l)"
say "rc histogram: $(cat $DEST/task_*/_rc.txt 2>/dev/null | sort | uniq -c | tr '\n' ' ')"
[ "$fail" -eq 0 ]
