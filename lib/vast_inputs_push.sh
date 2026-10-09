#!/usr/bin/env bash
# vast_inputs_push.sh — ship job inputs to a vast box THROUGH GCS.
#
# Why: on a bad day local->box rsync measured 0.8 MB/s while local->GCS was
# ~15 MB/s and box<-GCS 24.4 MB/s.  On a better day rsync did 3.2 MB/s and the
# GCS route lost (local UPLOAD to GCS 4.3 MB/s plus ~20 s fixed overhead: 50 MB
# took 36.6 s vs rsync's 16.3 s; break-even ~600 MB).  So it is opt-in
# (Q_VAST_INPUTS_VIA_GCS=1) and site_vast.py falls back to rsync whenever this
# returns non-zero.  It can also be called directly to stage a large input tree.
#
# The box is handed a 1-hour IMPERSONATED token over ssh stdin, never in argv
# and never echoed.  Unlike the result relay, the box-side helper here writes
# the token into a 0600 curl config under mktemp for the duration of the
# download loop and shreds it afterwards; the token expires within the hour.
#
# usage: vast_inputs_push.sh USER IP PORT TAG SRC:REMOTE_DEST [SRC:REMOTE_DEST ...]
# rc: 0 only when every file arrived with a matching sha256.
set -uo pipefail
U=$1; IP=$2; PORT=$3; TAG=$4; shift 4
BUCKET=${Q_BUCKET:?set Q_BUCKET}
GCLOUD=$(command -v "${Q_GCLOUD:-gcloud}" || true)
SA=${Q_VAST_GCS_SA:?set Q_VAST_GCS_SA}
CLEAN=${Q_VAST_GCS_CLEANUP:-1}
PREFIX="inputs/vast/$TAG"
BOXSSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=20 -o ServerAliveInterval=30 -p $PORT"
[ -n "$GCLOUD" ] && [ -x "$GCLOUD" ] || { echo "inputs-gcs: no gcloud (Q_GCLOUD=${Q_GCLOUD:-gcloud})" >&2; exit 61; }
[ $# -gt 0 ] || { echo "inputs-gcs: nothing to ship" >&2; exit 62; }

staging=$(mktemp -d "${TMPDIR:-/tmp}/qvast_in.XXXXXX") || exit 63
plan="$staging/plan"; : > "$plan"
i=0
for pair in "$@"; do
  src=${pair%%:*}; dst=${pair#*:}
  [ -f "$src" ] || { echo "inputs-gcs: missing $src" >&2; rm -rf "$staging"; exit 64; }
  i=$((i+1)); key="f$i"
  cp -- "$src" "$staging/$key" || { rm -rf "$staging"; exit 65; }
  printf '%s %s %s\n' "$key" "$(sha256sum "$src" | awk '{print $1}')" "$dst" >> "$plan"
done

# one rsync uploads the whole staging dir in parallel
"$GCLOUD" storage rsync "$staging" "gs://$BUCKET/$PREFIX" >&2 || { rm -rf "$staging"; exit 66; }

$BOXSSH "$U@$IP" "cat > /tmp/qvast_in_$TAG.sh" <<'REMOTE'
#!/usr/bin/env bash
set -uo pipefail
read -r TOK
[ -n "$TOK" ] || { echo "NO_TOKEN" >&2; exit 71; }
BUCKET=$1; PREFIX=$2; PLAN=$3
CFG=$(mktemp); printf 'header = "Authorization: Bearer %s"\n' "$TOK" > "$CFG"
unset TOK
chmod 600 "$CFG"
rc=0
while read -r key sha dst; do
  [ -n "$key" ] || continue
  mkdir -p "$(dirname "$dst")"
  enc=$(printf '%s' "$PREFIX/$key" | sed 's|/|%2F|g')
  code=$(curl -sS -o "$dst" -w '%{http_code}' -K "$CFG" \
    "https://storage.googleapis.com/storage/v1/b/$BUCKET/o/$enc?alt=media")
  if [ "$code" != "200" ]; then echo "inputs-gcs: $dst http=$code" >&2; rc=72; break; fi
  got=$(sha256sum "$dst" | awk '{print $1}')
  if [ "$got" != "$sha" ]; then echo "inputs-gcs: sha mismatch for $dst" >&2; rc=73; break; fi
done < "$PLAN"
shred -u "$CFG" 2>/dev/null || rm -f "$CFG"
exit $rc
REMOTE
rcs=$?
if [ "$rcs" -ne 0 ]; then rm -rf "$staging"; echo "inputs-gcs: could not stage helper" >&2; exit 67; fi

# the plan is not secret; ship it with the helper
$BOXSSH "$U@$IP" "cat > /tmp/qvast_in_$TAG.plan" < "$plan"
[ $? -eq 0 ] || { rm -rf "$staging"; echo "inputs-gcs: could not stage plan" >&2; exit 68; }

tok=$("$GCLOUD" auth print-access-token --impersonate-service-account="$SA" 2>/dev/null)
[ -n "$tok" ] || { rm -rf "$staging"; echo "inputs-gcs: could not mint token" >&2; exit 69; }
printf '%s\n' "$tok" | $BOXSSH "$U@$IP" \
  "bash /tmp/qvast_in_$TAG.sh '$BUCKET' '$PREFIX' '/tmp/qvast_in_$TAG.plan'; r=\$?; rm -f /tmp/qvast_in_$TAG.sh /tmp/qvast_in_$TAG.plan; exit \$r" >&2
rcb=$?
tok=""; unset tok
rm -rf "$staging"
[ "$CLEAN" = "1" ] && "$GCLOUD" storage rm -r "gs://$BUCKET/$PREFIX" >/dev/null 2>&1
if [ "$rcb" -ne 0 ]; then echo "inputs-gcs rc=$rcb (caller should fall back to rsync)" >&2; exit "$rcb"; fi
echo "inputs-gcs: $i file(s) delivered and sha256-verified" >&2
exit 0
