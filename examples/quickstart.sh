#!/usr/bin/env bash
# Laptop quickstart: the local site end to end, no cloud account needed.
#
#   examples/quickstart.sh [SCRATCH_ROOT]
#
# Uses its own Q_ROOT (default .scratch/quickstart inside the checkout, which
# is gitignored), so it never touches another queue's spool, state or lock.
# The daemon is run in the foreground for a bounded number of short cycles
# instead of under the supervisor, so nothing is left running afterwards.
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
export Q_ROOT="${1:-$HERE/.scratch/quickstart}"
export Q_CONFIG="$Q_ROOT/config.env"
rm -rf "$Q_ROOT"; mkdir -p "$Q_ROOT"
# Local-only config: Batch, Vast and Vultr stay unconfigured.  Short cycles so
# the demo finishes in seconds (production default is 60 s).
grep -E '^Q_LOCAL_|^Q_MEM_|^Q_MAX_MEM' "$HERE/config.env.example" > "$Q_CONFIG"
printf 'Q_CYCLE_SEC=2\nQ_RSS_POLL_SEC=1\nQ_LOCAL_RESERVE_THREADS=1\n' >> "$Q_CONFIG"
q(){ "$HERE/bin/q" "$@"; }
qd_run(){ "$HERE/bin/qd" --cycles "$1"; }

echo "### 1. submit four tasks (one per line of examples/hello.cmds)"
q submit --name hello --cmds "$HERE/examples/hello.cmds" --site local \
  --cpus 1 --mem 256M --timeout 2m
echo
echo "### 2. run the daemon for a few cycles"
qd_run 4
echo
echo "### 3. status: task 3 fails because \$IN/ready does not exist yet"
q status hello
echo
echo "### 4. fix the input and resubmit the SAME name: only task 3 runs again"
touch "$Q_ROOT/inputs/ready"
q submit --name hello --cmds "$HERE/examples/hello.cmds" --site local \
  --cpus 1 --mem 256M --timeout 2m
qd_run 3
q status hello
echo
echo "### 5. pull the outputs (a local copy; the daemon owns every network transfer)"
q pull hello "$Q_ROOT/pulled"
( cd "$Q_ROOT/pulled" && for d in task_*; do printf '%s: rc=%s files=%s\n' "$d" "$(cat "$d/_rc.txt")" "$(ls "$d" | tr '\n' ' ')"; done )
echo
echo "### 6. the board line and the job's event log"
q board
q log hello | tail -n 6
