#!/usr/bin/env bash
# Run every test.  Offline tests use a throwaway Q_ROOT and Q_CONFIG=/dev/null;
# cloud tests exit 77 (SKIP) unless FLEETQ_CLOUD_TESTS=1.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pass=0; fail=0; skip=0; failed=()
run () {
  local name="$1"; shift
  local log; log="$(mktemp)"
  "$@" >"$log" 2>&1; local rc=$?
  case "$rc" in
    0)  echo "PASS  $name"; pass=$((pass+1)) ;;
    77) echo "SKIP  $name ($(tail -1 "$log"))"; skip=$((skip+1)) ;;
    *)  echo "FAIL  $name (rc=$rc)"; tail -20 "$log" | sed 's/^/      /'; fail=$((fail+1)); failed+=("$name") ;;
  esac
  rm -f "$log"
}
for t in t1_local t3_capacity t4_preempt t5_routing t7_watchers t8_lanes t2_batch t2_check t2b_pull t3b_batchcap t6_vast; do
  [ -f "$HERE/$t.sh" ] && run "$t" bash "$HERE/$t.sh"
done
for t in test_pullfix test_vultr test_vultr_cli t9_waves; do
  tmp_root="$(mktemp -d)"
  [ -f "$HERE/$t.py" ] && run "$t" env Q_ROOT="$tmp_root" Q_CONFIG=/dev/null python3 "$HERE/$t.py"
  rm -rf "$tmp_root"
done
echo "passed=$pass failed=$fail skipped=$skip"
[ "$fail" -eq 0 ] || { echo "failed: ${failed[*]}"; exit 1; }
