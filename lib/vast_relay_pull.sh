#!/usr/bin/env bash
# vast_relay_pull.sh — result pull for the vast site that avoids the slow
# direct box -> local link.
#
# Why: the home link this was built on is token-bucket shaped.  A small file
# bursts at ~10 MB/s, but a sustained transfer from a far host collapses to
# 70-130 kB/s (box -> local measured 79 kB/s).  Cloud object stores are not
# shaped the same way (GCS -> local measured 14-28 MB/s).  So results take a
# detour through something fast.
#
#   gcs      (recommended) the box packs everything new since the last pull
#            (files newer than its per-job .relay_marker) into ONE tar.zst and
#            pushes it to gs://$Q_BUCKET/jobs/<job>/relay/; this machine pulls
#            it with `gcloud storage cp` and unpacks it into the cache in the
#            same task_i/ layout Batch produces.  The box is handed a 1-hour
#            IMPERSONATED access token over ssh stdin only.  Every shipped file
#            is verified by sha256 against the box's own manifest; any mismatch
#            is fatal (rc=70) and the marker is only promoted after a verified
#            unpack, so a failed pull re-ships instead of losing files.
#            Falls back to the cluster path on failure.
#
#   cluster  two hops.  Hop 1: a cluster login node you hold an
#            ssh ControlMaster socket to (fleet/cluster_connect.sh) INITIATES an
#            rsync from the box into its scratch (14.3 MB/s measured) -- so the
#            results are safe off the box first.  Hop 2: local rsyncs from the
#            cluster over the same socket (~100-130 kB/s, resumable).
#
#   r2       hop 1 as above, then the cluster tars+zstds the job into ONE
#            object, pushes it with rclone to a Cloudflare R2 remote that is
#            already authorized THERE, mints a short-lived presigned GET URL,
#            and local curls it over HTTPS (measured ~12 s for 333 MB end to
#            end).  Falls back to the cluster hop 2 on any failure.
#
# usage: vast_relay_pull.sh USER IP PORT REMOTE_ROOT JOB DEST RELAY_HOST RELAY_ROOT [MODE]
# rc: 0 only when the chosen route (or its fallback) fully succeeded.
#
# SECURITY: tokens and presigned URLs are bearer credentials.  They are never
# echoed, never written to disk and never placed in argv (curl reads them from
# a -K config on a pipe fd), so they cannot leak through the pull log or `ps`.
# Nothing long-lived ever reaches the rented box.
#
# Required env for gcs mode: Q_BUCKET, Q_VAST_GCS_SA (the service account to
# impersonate; it needs objectAdmin on the bucket only), Q_GCLOUD (gcloud path).
set -uo pipefail
U=$1; IP=$2; PORT=$3; ROOT=$4; JOB=$5; DEST=$6; RH=$7; RR=$8
MODE=${9:-${Q_VAST_RELAY:-cluster}}

CTL="$HOME/.ssh/cm/%r@%h:%p"
RSSH="ssh -o ControlPath=$CTL -o ControlMaster=no -o BatchMode=yes -o ConnectTimeout=25"
BOXSSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=20 -o ServerAliveInterval=30 -p $PORT"

mkdir -p "$DEST"

# ---- hop 1 (cluster-initiated; the cluster account holds the box's ssh key).
# -n: never let ssh eat stdin.  Used by the cluster and r2 modes.
hop1_box_to_cluster() {
  $RSSH -n "$RH" "mkdir -p '$RR/$JOB/out' && rsync -a --partial -e '$BOXSSH' '$U@$IP:$ROOT/$JOB/out/' '$RR/$JOB/out/'" 2>&1 | grep -v '^DEBUG:' >&2
  local rc=${PIPESTATUS[0]}; echo "relay hop1 box->cluster rc=$rc" >&2; return "$rc"
}

# ---- hop 2, cluster mode: local <- cluster, idempotent and resumable per file.
hop2_cluster() {
  rsync -a --checksum --partial -e "$RSSH" "$RH:$RR/$JOB/out/" "$DEST/" 2>&1 | grep -v '^DEBUG:' >&2
  local rc=${PIPESTATUS[0]}; echo "relay hop2 cluster->local rc=$rc" >&2; return "$rc"
}

# ---- hop 2, r2 mode: cluster -> R2 object -> local over HTTPS.
hop2_r2() {
  local remote=${Q_VAST_R2_REMOTE:?set Q_VAST_R2_REMOTE (rclone remote:bucket on the cluster)}
  local prefix=${Q_VAST_R2_PREFIX:-qvast_relay}
  local expire=${Q_VAST_R2_EXPIRE:-2h}
  local zl=${Q_VAST_R2_ZSTD:-3}
  local zt=${Q_VAST_R2_ZSTD_THREADS:-4}
  local cleanup=${Q_VAST_R2_CLEANUP:-1}
  local obj="$prefix/$JOB.tar.zst"

  # local zstd is needed to unpack; bail to the rsync path if absent.
  local ZS; ZS=$(command -v zstd || echo "")
  [ -n "$ZS" ] || ZS=$(ls -1 "$HOME"/miniforge3/envs/*/bin/zstd 2>/dev/null | head -1)
  if [ -z "$ZS" ] || [ ! -x "$ZS" ]; then
    echo "relay hop2 r2: no local zstd, falling back to cluster mode" >&2; return 90
  fi

  # cluster: pack -> push -> print "SHA <sha256>" then "URL <presigned>" on stdout.
  # Everything else on that side goes to stderr so the URL parse cannot be fooled.
  local out
  out=$($RSSH -n "$RH" "set -uo pipefail
    RC=\$HOME/bin/rclone
    W='$RR/$JOB'; SRC=\"\$W/out\"; TAR=\"\$W/.relay.tar.zst\"
    [ -d \"\$SRC\" ] || { echo 'no src' >&2; exit 11; }
    rm -f \"\$TAR\"
    tar -C \"\$SRC\" -I \"zstd -$zl -T$zt\" -cf \"\$TAR\" . >&2 || exit 12
    \$RC copyto \"\$TAR\" '$remote/$obj' --s3-no-check-bucket >&2 || exit 13
    echo \"SHA \$(sha256sum \"\$TAR\" | awk '{print \$1}')\"
    echo \"SIZ \$(stat -c%s \"\$TAR\")\"
    \$RC link --expire '$expire' '$remote/$obj' 2>/dev/null | sed 's/^/URL /'
    rm -f \"\$TAR\"" 2>/dev/null)
  local rcr=$?
  if [ "$rcr" -ne 0 ]; then echo "relay hop2 r2: cluster-side stage rc=$rcr, falling back" >&2; return 91; fi

  local sha siz url
  sha=$(printf '%s\n' "$out" | awk '$1=="SHA"{print $2}' | head -1)
  siz=$(printf '%s\n' "$out" | awk '$1=="SIZ"{print $2}' | head -1)
  url=$(printf '%s\n' "$out" | awk '$1=="URL"{print $2}' | head -1)
  if [ -z "$sha" ] || [ -z "$url" ]; then echo "relay hop2 r2: no sha/url, falling back" >&2; return 92; fi
  echo "relay hop2 r2: staged $siz B object, presigned (url redacted)" >&2

  local tmp; tmp=$(mktemp "${TMPDIR:-/tmp}/qvast_r2.XXXXXX") || return 93
  # -K on a pipe fd: the URL never appears in argv and never touches disk.
  curl -sS --fail --retry 3 --retry-delay 2 -o "$tmp" \
       -K <(printf 'url = "%s"\n' "$url") >&2
  local crc=$?
  unset url; out=""
  if [ "$crc" -ne 0 ]; then rm -f "$tmp"; echo "relay hop2 r2: curl rc=$crc, falling back" >&2; return 94; fi

  local got; got=$(sha256sum "$tmp" | awk '{print $1}')
  if [ "$got" != "$sha" ]; then rm -f "$tmp"; echo "relay hop2 r2: sha mismatch, falling back" >&2; return 95; fi

  "$ZS" -d -c "$tmp" | tar -x -C "$DEST" 2>&1 | grep -v '^DEBUG:' >&2
  local trc=${PIPESTATUS[1]}
  rm -f "$tmp"
  if [ "$trc" -ne 0 ]; then echo "relay hop2 r2: untar rc=$trc, falling back" >&2; return 96; fi

  # the staging object is transient; the payload now exists on the cluster and locally.
  if [ "$cleanup" = "1" ]; then
    $RSSH -n "$RH" "\$HOME/bin/rclone deletefile '$remote/$obj' >/dev/null 2>&1; true" >/dev/null 2>&1
  fi
  echo "relay hop2 r2 cluster->r2->local rc=0 bytes=$siz" >&2
  return 0
}


# ---- hop, gcs mode: box -> GCS -> local, no cluster hop at all.
# The box gets a 1-hour impersonated token over ssh stdin ONLY: it lands in one
# shell variable, is handed to curl through a -K config on a pipe fd, and is
# never in argv, never on the box's disk and never echoed.  Nothing long-lived
# ever reaches the box.
hop_gcs() {
  local bucket=${Q_BUCKET:-}
  local gcloud_bin; gcloud_bin=$(command -v "${Q_GCLOUD:-gcloud}" || true)
  local sa=${Q_VAST_GCS_SA:-}
  [ -n "$bucket" ] && [ -n "$sa" ] || { echo "relay gcs: Q_BUCKET and Q_VAST_GCS_SA must be set" >&2; return 79; }
  local zl=${Q_VAST_GCS_ZSTD:-3}
  local zt=${Q_VAST_GCS_ZSTD_THREADS:-8}
  local cleanup=${Q_VAST_GCS_CLEANUP:-1}
  local jobdir="$ROOT/$JOB"
  local stamp; stamp=$(date +%s)
  local obj="jobs/$JOB/relay/$stamp.tar.zst"

  local ZS; ZS=$(command -v zstd || echo "")
  [ -n "$ZS" ] || ZS=$(ls -1 "$HOME"/miniforge3/envs/*/bin/zstd 2>/dev/null | head -1)
  if [ -z "$ZS" ] || [ ! -x "$ZS" ]; then echo "relay gcs: no local zstd" >&2; return 80; fi
  [ -n "$gcloud_bin" ] && [ -x "$gcloud_bin" ] || { echo "relay gcs: no gcloud (Q_GCLOUD=${Q_GCLOUD:-gcloud})" >&2; return 81; }

  # stage the box-side helper.  NO -n here: -n points stdin at /dev/null and
  # would silently feed `cat` nothing, leaving a 0-byte helper on the box
  # (exactly what happened on the first run of this mode).
  $BOXSSH "$U@$IP" "cat > /tmp/qvast_gcs_up.sh" <<'REMOTE'
#!/usr/bin/env bash
# box side: pack the files newer than the marker, push ONE object to GCS.
# stdout carries only the protocol lines; everything else goes to stderr.
set -uo pipefail
read -r TOK
[ -n "$TOK" ] || { echo "NO_TOKEN" >&2; exit 21; }
JOBDIR=$1; BUCKET=$2; OBJ=$3; ZL=$4; ZT=$5; STAMPF=$6
cd "$JOBDIR" 2>/dev/null || { echo "no jobdir" >&2; exit 22; }
[ -d out ] || { echo "no out dir" >&2; exit 23; }
ZS=$(command -v zstd || echo /venv/main/bin/zstd)
[ -x "$ZS" ] || { echo "no zstd on box" >&2; exit 24; }
# snapshot boundary FIRST, so anything written during the transfer is simply
# picked up by the next pull instead of being silently skipped.
touch "$STAMPF.new"
if [ -f "$STAMPF" ]; then
  find out -type f -newer "$STAMPF" -printf '%P\n' > /tmp/qvast.list
else
  find out -type f -printf '%P\n' > /tmp/qvast.list
fi
N=$(wc -l < /tmp/qvast.list)
if [ "$N" -eq 0 ]; then echo "EMPTY"; rm -f "$STAMPF.new" /tmp/qvast.list; exit 0; fi
TAR=/tmp/qvast_relay.tar.zst
tar -C out -I "$ZS -$ZL -T$ZT" -cf "$TAR" -T /tmp/qvast.list >&2 || exit 25
code=$(curl -sS -o /tmp/qvast_up.out -w '%{http_code}' -X POST --data-binary @"$TAR" \
  -K <(printf 'header = "Authorization: Bearer %s"\n' "$TOK") \
  -H 'Content-Type: application/octet-stream' \
  "https://storage.googleapis.com/upload/storage/v1/b/$BUCKET/o?uploadType=media&name=$OBJ")
unset TOK
if [ "$code" != "200" ]; then echo "upload http=$code" >&2; rm -f /tmp/qvast_up.out; exit 26; fi
rm -f /tmp/qvast_up.out
echo "SHA $(sha256sum "$TAR" | awk '{print $1}')"
echo "SIZ $(stat -c%s "$TAR")"
echo "CNT $N"
# per-file manifest of exactly what went into the tar (paths relative to out/)
(cd out && xargs -a /tmp/qvast.list -d '\n' sha256sum) | sed 's/^/MAN /'
rm -f "$TAR" /tmp/qvast.list
exit 0
REMOTE
  [ $? -eq 0 ] || { echo "relay gcs: could not stage box helper" >&2; return 82; }

  local tok out rcb
  tok=$("$gcloud_bin" auth print-access-token --impersonate-service-account="$sa" 2>/dev/null)
  [ -n "$tok" ] || { echo "relay gcs: could not mint impersonated token" >&2; return 83; }
  # token goes over ssh stdin and nowhere else
  out=$(printf '%s\n' "$tok" | $BOXSSH "$U@$IP" \
        "bash /tmp/qvast_gcs_up.sh '$jobdir' '$bucket' '$obj' '$zl' '$zt' '$jobdir/.relay_marker'" 2>/dev/null)
  rcb=$?
  tok=""; unset tok
  [ "$rcb" -eq 0 ] || { echo "relay gcs: box-side rc=$rcb" >&2; return 84; }

  if printf '%s\n' "$out" | grep -q '^EMPTY$'; then
    echo "relay gcs: nothing new since last pull rc=0" >&2; return 0
  fi
  local sha siz cnt
  sha=$(printf '%s\n' "$out" | awk '$1=="SHA"{print $2}' | head -1)
  siz=$(printf '%s\n' "$out" | awk '$1=="SIZ"{print $2}' | head -1)
  cnt=$(printf '%s\n' "$out" | awk '$1=="CNT"{print $2}' | head -1)
  [ -n "$sha" ] && [ -n "$siz" ] || { echo "relay gcs: no sha/siz from box" >&2; return 85; }

  local tmpd; tmpd=$(mktemp -d "${TMPDIR:-/tmp}/qvast_gcs.XXXXXX") || return 86
  printf '%s\n' "$out" | sed -n 's/^MAN //p' > "$tmpd/manifest"
  "$gcloud_bin" storage cp "gs://$bucket/$obj" "$tmpd/blob" >&2
  [ $? -eq 0 ] || { rm -rf "$tmpd"; echo "relay gcs: download failed" >&2; return 87; }
  if [ "$(sha256sum "$tmpd/blob" | awk '{print $1}')" != "$sha" ]; then
    rm -rf "$tmpd"; echo "relay gcs: tar sha mismatch" >&2; return 88; fi

  "$ZS" -d -c "$tmpd/blob" | tar -x -C "$DEST" 2>&1 | grep -v '^DEBUG:' >&2
  local trc=${PIPESTATUS[1]}
  [ "$trc" -eq 0 ] || { rm -rf "$tmpd"; echo "relay gcs: untar rc=$trc" >&2; return 89; }

  # per-file verification against the box's own manifest; ANY mismatch is fatal.
  local bad
  bad=$( (cd "$DEST" && sha256sum -c --quiet "$tmpd/manifest") 2>&1 | head -5 )
  if [ -n "$bad" ]; then
    rm -rf "$tmpd"
    echo "relay gcs: PER-FILE VERIFY FAILED: $bad" >&2; return 70; fi
  rm -rf "$tmpd"

  # only now promote the marker, so a failed pull re-ships instead of losing files
  $BOXSSH -n "$U@$IP" "mv -f '$jobdir/.relay_marker.new' '$jobdir/.relay_marker'" >/dev/null 2>&1
  [ "$cleanup" = "1" ] && "$gcloud_bin" storage rm "gs://$bucket/$obj" >/dev/null 2>&1
  echo "relay gcs box->gcs->local rc=0 files=$cnt packed=$siz verified=per-file-sha256" >&2
  return 0
}

# ---- is the cluster ControlMaster socket actually alive?  Fallback needs it.
cluster_socket_live() { [ -n "$RH" ] && $RSSH -n -O check "$RH" >/dev/null 2>&1; }

case "$MODE" in
  gcs)
      hop_gcs; grc=$?
      [ "$grc" -eq 0 ] && exit 0
      echo "relay gcs rc=$grc -> attempting cluster fallback" >&2
      if ! cluster_socket_live; then
        echo "relay FATAL: gcs path failed (rc=$grc) and no cluster ControlMaster socket is live; no route left" >&2
        exit "$grc"
      fi
      hop1_box_to_cluster || exit $?
      hop2_cluster; exit $? ;;
  r2) hop1_box_to_cluster || exit $?
      hop2_r2; r2rc=$?
      [ "$r2rc" -eq 0 ] && exit 0
      echo "relay hop2 r2 rc=$r2rc -> cluster fallback" >&2
      hop2_cluster; exit $? ;;
  *)  hop1_box_to_cluster || exit $?
      hop2_cluster; exit $? ;;
esac
