#!/usr/bin/env bash
# The per-task pull must report failed or empty box inventories as failures.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/root/fleet_state"
printf 'export IP9=127.0.0.1\nexport PORT9=22\n' > "$T/root/fleet_state/instances.env"
cat > "$T/bin/ssh" <<'SH'
#!/usr/bin/env bash
for arg; do last=$arg; done
case "$last" in
  'cat > /tmp/vtask_up.sh') cat >/dev/null ;;
  'test -s /tmp/vtask_up.sh'*) exit 0 ;;
  'ls -1 '*)
    case "$TASK_PULL_LIST" in
      failed) exit 1 ;;
      empty) exit 0 ;;
      found) echo task_0 ;;
    esac ;;
  'find '*) exit 1 ;;
  *) exit 1 ;;
esac
SH
printf '#!/bin/sh\nexit 0\n' > "$T/bin/gcloud"
chmod +x "$T/bin/ssh" "$T/bin/gcloud"
export PATH="$T/bin:$PATH" Q_ROOT="$T/root" Q_CONFIG=/dev/null Q_BUCKET=test Q_VAST_GCS_SA=test Q_GCLOUD=gcloud
fails=0
for mode in failed empty found; do
  export TASK_PULL_LIST="$mode"
  bash "$HERE/lib/vast_task_pull.sh" job 9 >/dev/null 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then echo "FAIL  $mode inventory returned success"; fails=$((fails+1)); fi
done
echo "task pull inventory: $fails failure(s)"
test "$fails" -eq 0
