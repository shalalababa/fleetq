#!/usr/bin/env bash
# Offline GCS relay: a failed quiet sha256 check must not promote the marker.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/remote/job/out/task_0" "$T/dest"
printf '0\n' > "$T/remote/job/out/task_0/_rc.txt"
export RELAY_TEST_ROOT="$T"
cat > "$T/bin/ssh" <<'SH'
#!/usr/bin/env bash
for arg; do last=$arg; done
case "$last" in
  'cat > /tmp/qvast_gcs_up.sh') cat > "$RELAY_TEST_ROOT/helper" ;;
  *'bash /tmp/qvast_gcs_up.sh '*) bash -c "${last/\/tmp\/qvast_gcs_up.sh/$RELAY_TEST_ROOT\/helper}" 2>> "$RELAY_TEST_ROOT/remote.log" ;;
  *'mv -f '* )
    [ "${RELAY_TEST_MARKER_FAIL:-0}" = 1 ] && exit 1
    bash -c "$last" ;;
  *) exit 1 ;;
esac
SH
cat > "$T/bin/gcloud" <<'SH'
#!/usr/bin/env bash
case "$1 $2" in
  'auth print-access-token') echo test-token ;;
  'storage cp') cp "$RELAY_TEST_ROOT/object" "$4" ;;
  'storage rm') exit 0 ;;
  *) exit 1 ;;
esac
SH
cat > "$T/bin/curl" <<'SH'
#!/usr/bin/env bash
for arg; do
  case "$arg" in @*) cp "${arg#@}" "$RELAY_TEST_ROOT/object" ;; esac
done
echo 200
SH
cat > "$T/bin/sha256sum" <<'SH'
#!/usr/bin/env bash
[ "${RELAY_TEST_VERIFY_FAIL:-0}" = 1 ] && [ "$1" = -c ] && exit 1
[ "${RELAY_TEST_MANIFEST_FAIL:-0}" = 1 ] && [ "$1" = task_0/_rc.txt ] && exit 1
exec /usr/bin/sha256sum "$@"
SH
cat > "$T/bin/find" <<'SH'
#!/usr/bin/env bash
[ "${RELAY_TEST_FIND_FAIL:-0}" = 1 ] && exit 1
exec /usr/bin/find "$@"
SH
chmod +x "$T/bin/"*
export PATH="$T/bin:$PATH" Q_BUCKET=test-bucket Q_VAST_GCS_SA=test-sa Q_GCLOUD=gcloud Q_VAST_GCS_CLEANUP=0
run_pull(){ bash "$HERE/lib/vast_relay_pull.sh" user 127.0.0.1 22 "$T/remote" job "$T/dest" '' '' gcs >/dev/null 2>"$T/log"; result=$?; if [ "$result" -ne 0 ]; then cat "$T/log" >&2; cat "$T/remote.log" >&2 2>/dev/null; fi; return "$result"; }
fails=0
check(){ if "$@"; then :; else echo "FAIL  $*"; fails=$((fails+1)); fi; }
export RELAY_TEST_VERIFY_FAIL=1
run_pull; rc=$?
check test "$rc" -eq 70
check test ! -e "$T/remote/job/.relay_marker"
unset RELAY_TEST_VERIFY_FAIL
export RELAY_TEST_MANIFEST_FAIL=1
run_pull; rc=$?
check test "$rc" -ne 0
check test ! -e "$T/remote/job/.relay_marker"
unset RELAY_TEST_MANIFEST_FAIL
export RELAY_TEST_FIND_FAIL=1
run_pull; rc=$?
check test "$rc" -ne 0
check test ! -e "$T/remote/job/.relay_marker"
unset RELAY_TEST_FIND_FAIL
export RELAY_TEST_MARKER_FAIL=1
run_pull; rc=$?
check test "$rc" -ne 0
check test ! -e "$T/remote/job/.relay_marker"
unset RELAY_TEST_MARKER_FAIL
run_pull; rc=$?
check test "$rc" -eq 0
check test -f "$T/remote/job/.relay_marker"
echo "relay: $fails failure(s)"
test "$fails" -eq 0
