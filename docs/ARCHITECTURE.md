# Architecture

## Processes

- **`bin/q`** (bash): the user CLI. `submit` validates arguments, writes a
  job spec to `spool/pending/`, and claims the job name atomically. `status`,
  `board`, `log`, `cancel` and `pull` read or annotate state. `q` never
  schedules anything itself.
- **`bin/qd`** (Python): the daemon. One process per `Q_ROOT`, guarded by a
  lock file. Each cycle (`Q_CYCLE_SEC`) it ingests the spool, refreshes every
  site once, schedules runnable tasks by priority then age, and polls running
  local tasks every `Q_RSS_POLL_SEC` for memory and timeouts. `qd --cycles N`
  runs N cycles in the foreground (used by the quickstart and tests).
- **`bin/qd_supervise.sh`**: restarts `qd` if it exits.
- **`lib/pull_worker.py`**: a detached worker per Batch result pull, so a slow
  transfer never blocks the scheduling cycle. It is killed as a process group
  on timeout.
- **`bin/fleet`** (bash) and `fleet/*.py`: controller tooling for GPU boxes and
  waves; separate from the queue but sharing `config.env` and `Q_ROOT`.
- **`box/lanes.sh`, `box/watchdog.sh`**: run on each GPU box; installed there
  by `fleet push-repo`.

## State on disk (`Q_ROOT`)

| path | written by | contents |
|------|-----------|----------|
| `spool/pending/` | `q submit` | job specs waiting to be ingested |
| `state/<job>.json` | `qd` (atomic rename) | job and per-task state, attempts, site bookkeeping |
| `state/archive/` | `q submit` on resume | previous epochs of a resubmitted job |
| `ledger.tsv` | Batch adapter (append-only) | Batch submit intent, failure, and acceptance; absent for local-only jobs |
| `BOARD.txt` | `qd` | the latest board, readable without the daemon |
| `local_out/<job>/task_<i>/` | local tasks | `$OUT` for local tasks: `_rc.txt`, `_pid.txt`, logs |
| `cache/<job>/` | pull worker / relays | pulled results from remote sites |
| `inputs/` or `inputs/<subpath>/` | you | local job input root, optionally selected by `--inputs` |
| `logs/` | `qd`, pull workers | per-job and per-pull logs |
| `fleet_state/` | `fleet refresh`, `fleet destroy` | `instances.json/.env`, `destroyed.jsonl`, `durations.json` |
| `waves/<id>/` | you, `fleet gen` | `spec.json` and `generated/` |

Nothing is kept only in memory. Killing `qd` at any instant and restarting it
is a supported operation.

## Task lifecycle

```
pending -> LAUNCHING -> RUNNING -> DONE            (rc 0, never re-run)
                                 -> FAILED          (rc != 0)
                                 -> requeued        (memory kill, under Q_MAX_MEM_KILLS)
```

A job is DONE when every task is DONE, FAILED when it is terminal with any
failed task. `q submit` with the same name after a terminal state archives the
state and starts a new epoch; tasks whose `_rc.txt` is 0 are skipped.

## Site adapters

Each site module (`lib/site_<name>.py`) exposes the same small surface used by
the scheduler: refresh its view once per cycle, report free capacity, launch a
set of tasks, and collect completions. Capacity is site-specific:

- **local**: threads, `MemAvailable` minus the reserve, and GPU slots. Tasks
  are pinned to CPUs with `taskset` and placed in their own process group so
  the memory watchdog and kills act on the whole tree.
- **batch**: a vCPU budget. A chunk's cost is computed from how many tasks fit
  on one VM by both CPU and memory, so memory-heavy tasks are charged for the
  VMs they actually occupy. Submits are booked against the budget immediately,
  before the next listing shows them.
- **vast**: measured load average against a configured core cap, per box.
- **vultr**: VM count, vCPU cap, a spend budget, and an expiry; the adapter
  creates and deletes VMs and journals ownership.

## Pull and verification

- **Batch**: tasks write to GCS; `pull_worker.py` copies with `gcloud storage
  rsync` (optional directory excludes), then verifies file count, bytes,
  manifest sha256s and a witness file before the pull is recorded.
- **Vast, direct**: rsync from the box.
- **Vast, `gcs` relay** (`lib/vast_relay_pull.sh`): box packs files newer than
  its relay marker (tar + zstd) with a sha256 manifest, uploads with a 1-hour
  impersonated token received over ssh stdin, controller downloads and
  verifies (exit 70 on mismatch), then the marker advances. Per-task variant
  for large jobs: `lib/vast_task_pull.sh`.
- **Vast, `cluster`/`r2` relay**: two hops via a cluster login node (shared
  ControlMaster from `fleet cluster-connect`), optionally via R2 with rclone.
- **Inputs to Vast** (`lib/vast_inputs_push.sh`): optional GCS path for
  staged control files, rsync fallback.

## Box lanes

`box/lanes.sh` is a bash library sourced on the box. A lane is a directory of
files: the snapshotted command list, a cursor, a lock, and per-task logs. The
supervisor loop claims the next task under `flock`, runs it, records the exit
code, and advances; on failure it aborts the lane unless
`FL_ABORT_ON_FAIL=0`. `fl_queue_or_add` appends to an existing lane or starts
one. Events go to `$RUNROOT/_cloud_logs/events.jsonl` and optionally to ntfy.
`box/watchdog.sh` observes (dead supervisor, idle billing, disk, heartbeat)
and holds no queue state, so it is safe to restart on every push.

## Waves

`fleet/wavespec.py` parses and validates a spec; `fleet/wavegen.py` expands it
and writes every per-wave artifact; `fleet/wavecheck.py` evaluates the same
spec's predicates against a runroot. Targets: `lanes` (box lanes), `slurm`
(sbatch with export lists), `manual` (checked and pulled, never submitted).
`fleet/packing.py` and `fleet/durations.py` turn measured run durations into
lane assignments; `fleet/triage.py` matches logs against
`fleet/failure_signatures.tsv`; `fleet/digest.py` summarises the events of a
night.

## Destroy gate

`fleet/destroy_box.sh` uses `fleet/runroot_inventory.py` on both the box and
the archive (`FLEET_ARCHIVE_HOST`/`FLEET_ARCHIVE_ROOT`, or local). Every run
dir on the box must be present in the archive with counters at least as large
and matching identity hashes. A valid empty box inventory has no run dirs to
compare. `--yes` prompts for the Vast id. `--force-unsafe` permits unarchived
dirs and additionally prompts for `DESTROY UNARCHIVED` when any are found.

## Watchers

`watchers/*.sh` are detached controller-side loops, one `flock` each, that
read box state over the same ssh helpers as `bin/fleet` and write only logs
and marker files. They split one decision into stages so the irreversible
step has a single owner: `deadline_worker.sh` stops work and verifies the
pull, then drops `$FLEET_STATE/no_more_work_box<N>`; `destroy_gate_worker.sh`
re-checks everything itself (jobs terminal, no science processes, local >=
remote file counts under `out/`) and only then calls `fleet destroy`, whose
archive gate still applies unless the worker was started with
`--force-unsafe` (then the worker's own gates are the verification and it
answers `fleet destroy --force-unsafe`'s two typed confirmations). Markers under `$Q_ROOT/spool/tmp/`
(`destroyed_box<N>`, `destroy_refused_box<N>`, `deadline_cancel_box<N>`)
record outcomes and allow cancellation.
