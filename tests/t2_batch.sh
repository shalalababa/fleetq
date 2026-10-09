#!/usr/bin/env bash
# TEST 2 — Google Batch end to end: submit -> track -> pull -> `q pull`,
# and ONE `gcloud batch jobs list` call per cycle.
# Deliberate test-only overrides, both recorded in TESTS.md:
#   * Q_BATCH_CPU_CAP is raised, because the REAL 32-CPU cap is 100% consumed by
#     the other tracks' legacy gbatch waves; without the override the daemon
#     correctly refuses to submit anything (which is itself test 3's subject).
#   * machine e2-standard-2 + --cpus 1, so the wave costs ONE 2-vCPU spot VM.
set -uo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
Q=$HERE/bin
if [ "${FLEETQ_CLOUD_TESTS:-0}" != 1 ]; then
  echo "SKIP: $(basename "$0") needs real cloud credentials and a filled-in config.env"
  echo "      (set FLEETQ_CLOUD_TESTS=1 to run it; it may create billable resources)"
  exit 77
fi
export Q_LOCAL_RESERVE_PCT=0 Q_ROOT=$HERE/tests/run_batch
rm -rf "$Q_ROOT"; mkdir -p "$Q_ROOT"
export Q_BATCH_CPU_CAP=4096 Q_CYCLE_SEC=60 Q_PULL_INTERVAL_SEC=300
cat > "$Q_ROOT/lanes.cmds" <<'EOF'
printf 'task ok\n' > "$OUT/result.txt"; date -u >> "$OUT/result.txt"; python3 -c "print('py',2+2)" >> "$OUT/result.txt"; hostname >> "$OUT/result.txt"
printf 'task ok\n' > "$OUT/result.txt"; date -u >> "$OUT/result.txt"; python3 -c "print('py',3+3)" >> "$OUT/result.txt"; hostname >> "$OUT/result.txt"
EOF
$Q/q submit --name qtest-batch1 --cmds "$Q_ROOT/lanes.cmds" --site batch \
    --cpus 1 --mem 1G --machine e2-standard-2 --spot 1 --resumable 1 --timeout 20m
nohup "$Q/qd" --cycles 60 >> "$Q_ROOT/qd.stdout" 2>&1 &
echo "qd started pid=$! under $Q_ROOT (60 cycles x 60 s max)"
