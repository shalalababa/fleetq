# fleetq

A small job queue and fleet toolkit for running many independent experiment
tasks across whatever compute is at hand: the local machine, Google Cloud
Batch, rented Vast.ai GPU boxes, Vultr CPU VMs, and a Slurm cluster. It is
Python standard library plus bash, and it keeps all state in plain files
under one directory.

There are two halves:

- **`bin/q` + `bin/qd`** -- a spool-based queue. `q submit` drops a job (a
  `.cmds` file, one shell command per task) into a spool directory; the `qd`
  daemon schedules tasks onto sites, watches them, pulls results back, and
  keeps a ledger.
- **`bin/fleet`** -- controller-side tooling for GPU boxes running many
  long runs: per-GPU lanes on each box (`box/lanes.sh`), a box watchdog,
  wave specs that generate every submit/check/pull script from one file,
  duration-based lane packing, failure-signature triage, and a destroy command
  that refuses to delete a box until its results are verifiably archived.

## Architecture

```
   you                                     controller (laptop / workstation)
   ---                                     ---------------------------------
   q submit --name J --cmds J.cmds  --->   spool/pending/J.json
                                                 |
                                                 v
                                   qd  (one process, one cycle every Q_CYCLE_SEC)
                                    |   state/<job>.json  BOARD.txt
          +-------------+-----------+-------------+------------------+
          |             |                         |                  |
      site_local    site_batch                site_vast          site_vultr
      fork+taskset  gcloud batch jobs         ssh + nohup        create VM, run,
      PSS watchdog  one `list` per cycle      lib/vast_runner    pull, delete VM
          |             |                         |
          |         GCS bucket  <-- pull_worker (detached, verified)
          |             |                         |
          v             v                         v
      local_out/    cache/<job>/            box -> GCS relay -> cache/<job>/
                                            (1 h token, sha256 manifest)

   fleet gen --wave W   ->  waves/W/generated/{RUN_IDS.txt, runs.json,
                             lane_*.cmds, submit.sh, preflight_remote.sh,
                             pull_results.sh, submit_slurm.sh, bundle.sh}
   fleet submit/check/pull/destroy  ->  boxes running box/lanes.sh (one lane per GPU)
```

All queue state is files: `spool/` for submissions, `state/<job>.json` per job
(written atomically), `logs/`, `cache/` for pulled results. Batch submissions
also append intent and submit outcomes to `ledger.tsv`; local jobs do not
create that file. The daemon can be killed at any point and restarted; it
re-adopts what is running.

## The invariants

These are the rules the code is organised around. Each one exists because its
absence cost real work; `docs/LESSONS.md` has the incidents.

1. **A task whose `$OUT/_rc.txt` is `0` is never run again.** Resubmitting a
   job under the same name after it reached a terminal state archives the old
   state and resumes: completed tasks are skipped, failed and missing ones
   run. Job names are claimed atomically (`os.link`), so two submits cannot
   both win.

2. **Memory is enforced on the process group's PSS, not on RSS.** A task's
   `--mem` is a reservation for scheduling and a kill threshold. The watchdog
   polls RSS of the whole process group (cheap) and, only when that is over
   the cap, confirms with PSS from `/proc/<pid>/smaps_rollup` (accurate for
   shared pages, e.g. forked data-loader workers). It kills only after
   `Q_MEM_GRACE_POLLS` consecutive confirmed polls, then requeues the task up
   to `Q_MAX_MEM_KILLS` times before failing it. Free memory for scheduling is
   `MemAvailable` minus a reserve of `max(Q_LOCAL_RESERVE_MB,
   Q_LOCAL_RESERVE_PCT% of MemTotal)`.

3. **Completion is read from pulled outputs, never from a remote exit code.**
   Vast tasks are launched detached (`nohup`), so the ssh exit status means
   nothing. A remote task is done when its `_rc.txt` has been pulled home and
   verified.

4. **A pull is verified before it counts.** The Batch pull worker checks file
   count, total bytes, the job manifest's sha256s, and a witness file (the
   largest one). The GCS relay ships a per-file sha256 manifest and the box's
   relay marker only advances after the local side verifies it.

5. **A rented box is checked against the archive before destruction.**
   `fleet destroy N` reports by default. With `--yes` it inventories every run dir on the
   box and requires each one to exist in the archive with counters at least as
   large (checkpoint step, score lines, file count, bytes) and matching
   identity-file hashes. An unreadable inventory fails the gate; a valid
   inventory with zero run dirs has nothing to compare and may pass.
   `--force-unsafe` permits unarchived dirs after an additional typed
   confirmation.

6. **Intent is persisted before side effects.** A local task is recorded as
   `LAUNCHING` before fork; a Batch submit is recorded before the API call, and
   an `ALREADY_EXISTS` response is adopted rather than retried. After a crash
   the daemon re-adopts local tasks by `$OUT/_pid.txt` plus the process start
   time (so a recycled PID is not mistaken for the task), excluding zombies.

## Sites

| site    | how tasks run | capacity | how results come back |
|---------|---------------|----------|-----------------------|
| `local` | forked, pinned with `taskset`, niced, in their own process group | threads (`nproc - Q_LOCAL_RESERVE_THREADS`), `MemAvailable - reserve`, `Q_LOCAL_GPU_SLOTS` | already local (`local_out/<job>/task_<i>`) |
| `batch` | Google Cloud Batch job per chunk, `Q_IMAGE` container, spot first | vCPU budget `Q_BATCH_CPU_CAP`, counted per VM by both cpu and memory packing | GCS -> `cache/<job>` by a detached, verified pull worker |
| `vast`  | `lib/vast_runner.sh` started with `nohup` on a registered box | measured 1-min load average vs `Q_VAST_CORE_CAP` (the box's cgroup quota, not `nproc`) | direct rsync, or a relay: `gcs`, `cluster`, `r2` |
| `vultr` | qd creates a dedicated CPU VM, runs, pulls, deletes it | `Q_VULTR_CPU_CAP`, `Q_VULTR_MAX_VMS`, a spend budget and an expiry time | rsync from the VM |
| `either`| tries sites in `Q_EITHER_ORDER`; jobs under 4 tasks prefer local | | |

Slurm is handled by the wave tooling (`fleet gen` emits a cap-aware
`submit_slurm.sh` for `target: slurm` stages); the queue's Slurm adapter is a
stub.

### Batch details

- One `gcloud batch jobs list` per cycle for all jobs, not one describe per
  job.
- Spot first. A chunk that is preempted or fails is resubmitted on demand up
  to `Q_MAX_CHUNK_RETRIES` times, seeded from what the pull already brought
  home, so finished tasks in a preempted chunk are not re-run.
- A chunk that gets no VM for `Q_STALL_WARN_SEC` is reported; after
  `Q_STALL_ABORT_SEC` it is deleted and resubmitted on demand.
- Pulls run in a detached worker with a process-group kill on timeout; a
  transfer that makes no progress for twice the timeout is treated as hung.

### Vast details

- Boxes are registered in `fleet_state/instances.env` (`IP<N>`, `PORT<N>`),
  written by `fleet refresh` from the `vastai` CLI with sticky box numbers.
  qd reads the list at start.
- A job must name its box (`--vast-box N`) or opt in to any box
  (`--vast-any`); `--vast-chunk` sets tasks per launch.
- One ssh per box per cycle collects load average and task states.

### Why the GCS relay exists

The controller this was built on sits behind a home link that is token-bucket
shaped: short transfers are fast, sustained ones collapse to roughly
70-80 kB/s. Pulling a multi-GB result directory straight from a box would take
most of a day. The same link pulls from GCS at 14-20 MB/s. So in `gcs` relay
mode:

1. The controller mints a **1-hour** access token by impersonating a dedicated
   service account (`Q_VAST_GCS_SA`) that can write only to the relay bucket,
   and passes it to the box over ssh **stdin** -- never on a command line or
   in a file on the controller.
2. On the box, `curl` reads the token from a pipe file descriptor (`curl -K`),
   packs every file newer than the box's `.relay_marker` with tar + zstd,
   writes a per-file sha256 manifest, and uploads to GCS.
3. The controller downloads, unpacks, and verifies every sha256 (exit 70 on
   mismatch). Only after verification does the box's relay marker advance, so
   a failed or partial relay is simply retried next cycle.
4. The relay object is deleted after a verified unpack
   (`Q_VAST_GCS_CLEANUP=1`).

Very large jobs (more than about 2 GB) use `lib/vast_task_pull.sh`, which
relays one task at a time.

The `cluster` mode does the same through two ssh hops via a university login
node (with `fleet cluster-connect` holding one authenticated ControlMaster so
2FA is answered once); `r2` adds a Cloudflare R2 hop via rclone.

## Box lanes (`box/lanes.sh`)

On a GPU box each GPU gets a lane: a FIFO of shell commands run one at a time
by an on-box supervisor. Lanes are file-backed and `flock`ed (claim, advance,
abort, finish), so the controller can append to a running lane safely. The
command file is snapshotted at submit time. A failed task aborts its lane by
default (`FL_ABORT_ON_FAIL=1`), because the usual cause is a broken config that
would fail every later task too. Paired tasks start together and the lane
waits for both. Every transition is appended to `events.jsonl` and optionally
pushed to an ntfy topic; failures are classified against
`failure_signatures.tsv`. Before a GPU task starts, the lane waits for the GPU
to be idle and escalates if it is not.

`box/watchdog.sh` runs beside the lanes and reports a dead supervisor, a box
that is billing while every lane is idle, a filling disk, and a stale
heartbeat; it can optionally restart a dead supervisor.

## Waves (`fleet gen`)

A wave is a set of runs declared once in `waves/<id>/spec.json` (or `.yaml`):
stages, the variables each stage expands over, the command template, declared
inputs and outputs, and `done_when` predicates (file exists, checkpoint step
at least N, JSONL has at least N lines, ...). `fleet gen --wave <id>` writes
`RUN_IDS.txt` -- the expanded run list, which is the human approval gate --
plus the lane command files, a submit script, a preflight script to run on the
box, a two-pass pull script and an optional bundle script. Because all of them
come from one spec, what is submitted, what counts as done, and what is pulled
cannot disagree.

`fleet check` evaluates the predicates against the pulled runroot and prints
every observed counter whether or not it passed, then flags runs whose
realized training disagrees with the rest of their own cell. `fleet requeue`
regenerates lanes for exactly the runs the checker reports as not done, with
byte-identical commands. `fleet pack` assigns runs to lanes by longest
processing time first over measured durations, so lanes on a box drain at the
same time instead of leaving GPUs idle while one lane finishes.

## Waves example

`examples/wave_hello/spec.json` is a small wave with three toy `train` runs
that write a step-named checkpoint and two score lines. Each dependent `eval`
run checks its training checkpoint and writes a result file. Generate and
inspect the artifacts locally, without submitting to a box:

```bash
python3 fleet/wavegen.py examples/wave_hello --lanes 0,1 --out /tmp/wave_hello_generated
cat /tmp/wave_hello_generated/RUN_IDS.txt
```

The generated lane files, submit and pull scripts, and bundle script share
the run list in `runs.json`. To check outputs after running the commands, use
`python3 fleet/wavecheck.py examples/wave_hello --runroot /path/to/runroot`.

## Watchers (`watchers/`)

Long-lived, detached controller-side processes for unattended nights. Each
takes a `flock`, logs to `$Q_ROOT/logs/`, and survives the session (or agent)
that started it; arm them at decision time with `setsid nohup ... &`.

- `gpu_box_watch.sh N... [--until TIME]` reads `$OUT/_rc.txt` straight off
  each box and logs any non-zero rc as soon as it appears. The queue only sees
  a vast job's results after a pull, so a wave that fails in seconds would
  otherwise read RUNNING until then.
- `deadline_worker.sh N --soft TIME --hard TIME [--procs REGEX]` enforces a
  hard stop on one box: at the soft time it pulls everything finished; at the
  hard time it cancels the queue jobs placed on the box, SIGTERMs the science
  processes and waits for them to exit (so a resumable cell writes its last
  checkpoint), pulls again, and compares the box tree with the local cache.
  On success it drops `$FLEET_STATE/no_more_work_box<N>`. It never destroys.
- `destroy_gate_worker.sh N... [--procs REGEX] [--force-unsafe]` releases
  boxes once four gates pass per box: the no-more-work marker exists, every
  queue job on the box is terminal, no science process is running, and every
  job tree is fully pulled (local file count >= remote under `out/`). It then
  calls `fleet destroy N --yes`; if that command's archive gate refuses, the
  box stays up and the log says why. With `--force-unsafe` the worker's own
  four gates are treated as the verification: it overrides `fleet destroy`'s
  archive gate and answers both typed confirmations. That is for queue-only
  boxes whose results came home through the relay into `$Q_ROOT/cache`, where
  the archive gate is looking in the wrong place.

`--procs` (or `FLEET_SCIENCE_PROCS`) is an awk regex for the payload
processes; bracket one letter (`[t]rain\.py`) so it cannot match the shell
that carries it.

## Quickstart (local only, no cloud account needed)

Requirements: Linux, bash, Python 3.9+, `taskset`, `flock`.

```bash
git clone <this repo> fleetq && cd fleetq
bash examples/quickstart.sh
```

`examples/quickstart.sh` creates a scratch install root at
`.scratch/quickstart` (pass a different directory as its first argument).
It writes a minimal `config.env` and submits `examples/hello.cmds` (four tasks,
one of which fails until an input file exists). It runs `qd` for a few cycles
in the foreground, shows the failure, creates the input, resubmits the same
job name, and shows that only the failed task runs again. Output from a run on
a 16-thread laptop:

The script removes its scratch root before each run. Both submits may print a
daemon warning because `qd` runs only for the bounded cycles shown here.
`q status` also prints command and output paths; timestamps, PIDs, memory and
file counts vary by machine.

```
### 3. status: task 3 fails because $IN/ready does not exist yet
hello                    FAILED    local    3/4   prio=0
  spec: site=local cpus=1.0 mem=256MiB threads=1 spot=1 resumable=1 gpu=0 timeout=120s
   task 0    DONE      rc=0     peak_rss=0MiB
   task 1    DONE      rc=0     peak_rss=7MiB
   task 2    DONE      rc=0     peak_rss=0MiB
   task 3    FAILED    rc=3     peak_rss=0MiB rc=3

### 4. fix the input and resubmit the SAME name: only task 3 runs again
q: hello was FAILED — archived to state/archive/ and resubmitted as epoch 1; tasks whose $OUT/_rc.txt is 0 will NOT be re-run
hello                    DONE      local    4/4   prio=0
   task 0    DONE      rc=0     peak_rss=0MiB resume: $OUT/_rc.txt already 0
   task 1    DONE      rc=0     peak_rss=0MiB resume: $OUT/_rc.txt already 0
   task 2    DONE      rc=0     peak_rss=0MiB resume: $OUT/_rc.txt already 0
   task 3    DONE      rc=0     peak_rss=0MiB
  . resume: 3 task(s) already complete, skipped
  . ADMITTED to local
  . local task 3 launched pid=372316 cpus=[1] mem_cap=256MiB
  . local task 3 DONE rc=0 peak_rss=0MiB
  . DONE: 4 done / 0 failed

### 5. pull the outputs
q pull hello -> .scratch/quickstart/pulled: rc=0, 25 files now in DEST

### 6. the board line
cycle 3 | local 0/15 thr used, 6984 MiB free (reserve 1 thr/1793 MiB), 0 task(s) | batch -/32 vCPU (not polled: no batch jobs) | vast not polled (no vast jobs) | hello=DONE[local]4/4
```

To run the same workflow manually in a separate local root:

```bash
export Q_ROOT="$PWD/.scratch/manual"
mkdir -p "$Q_ROOT"
cp config.env.example "$Q_ROOT/config.env"  # edit the local section if needed
printf 'echo hello > "$OUT/hello.txt"\n' > "$Q_ROOT/myjob.cmds"
nohup setsid bin/qd_supervise.sh start >/dev/null 2>&1 &
bin/q submit --name myjob --cmds "$Q_ROOT/myjob.cmds" --site local --cpus 2 --mem 4G
bin/q status
bin/q board
```

The supervisor restarts `qd` if it exits and writes `supervise.log` under
`Q_ROOT`. Status and board can show a spooled job until the next daemon cycle.
After `bin/q status myjob` shows `DONE`, stop this manual supervisor with
`bin/qd_supervise.sh stop`.

Inside a local task, `$OUT` is the task's output directory and `$IN` is
`$Q_ROOT/inputs` (or its `--inputs` subpath). The local runner exports `Q_JOB`,
`Q_TASK`, `Q_THREADS`, and thread-count variables from `--threads`
(`examples/hello.cmds` prints some of them). Batch and Vultr set `$IN` and
`$OUT` in their runners; Vast uses `QVAST_IN` for `$IN`. A local task that
exits 0 gets `_rc.txt` = 0 and is skipped on resubmit.

## Configuration

Everything is in one shell-sourceable file, `config.env` (start from
`config.env.example`). `Q_ROOT` is the install root -- spool, state, logs,
caches and fleet state live under it -- and defaults to the checkout. Every
queue config key can be overridden by an environment variable of the same
name; `Q_CONFIG=/path` selects a different file. `bin/fleet` sources the file
as shell code, so a value set in it takes precedence over an existing
environment variable with the same name.

Per-site setup:

- **local**: nothing. Adjust the reserve and `Q_LOCAL_MAX_TASKS`.
- **Google Batch**: `gcloud` authenticated, a project (`Q_PROJECT`), a bucket
  (`Q_BUCKET`), a container image (`Q_IMAGE`), and `Q_BATCH_CPU_CAP` set to
  your vCPU quota. Leave `Q_PROJECT` empty to keep Batch unused.
- **Vast.ai**: the `vastai` CLI with an API key, an ssh key registered with
  Vast, `fleet refresh` to write the box list, then restart `qd`. For the
  `gcs` relay, a service account that can write only to the bucket, and
  `roles/iam.serviceAccountTokenCreator` on it for your own account.
- **Vultr**: an API key file (mode 0600), an ssh key uploaded to Vultr and its
  id, `Q_VULTR_ENABLE=1`, and a budget and expiry. qd creates and deletes VMs
  itself and keeps an ownership journal so it only ever deletes VMs it made.
- **Slurm** (waves only): run `submit_slurm.sh` on the login node with
  `Q_SLURM_ACCOUNT`, `Q_SLURM_PARTITION`, `Q_SLURM_MAX_JOBS`.

## Tests

```bash
bash tests/run_all.sh
```

Offline tests run the real daemon against a temporary `Q_ROOT` with fake
cloud listings (`Q_FAKE_LIST`) and dry-run pulls. `t7_watchers` runs the
three watchers against a temp directory standing in for a box (a fake `ssh`
on `PATH` executes the remote script locally). `t8_relay` checks the relay,
`t9_task_pull` checks task pulls, `t10_lanes` checks lane queues, and
`t11_waves` checks wave generation and validation. Tests that need a real
Batch project or a real Vast box exit 77 (skipped) unless
`FLEETQ_CLOUD_TESTS=1` is set.

## Layout

```
bin/        q (CLI), qd (daemon), qd_supervise.sh, fleet (box/wave CLI)
lib/        qcommon.py, site_*.py, pull_worker.py, vultr_*.py, vast_*.sh
box/        lanes.sh, watchdog.sh  (run ON the GPU boxes)
watchers/   gpu_box_watch.sh, deadline_worker.sh, destroy_gate_worker.sh
fleet/      wave spec/gen/check, packing, durations, board, triage, digest,
            refresh, destroy_box.sh, cluster_connect.sh, common.sh
templates/  wheelhouse_batch.cmds
examples/   hello.cmds, quickstart.sh
tests/      shell + python tests
docs/       ARCHITECTURE.md, LESSONS.md
```

## Limitations

- Single controller. There is no HA daemon; one `qd` per `Q_ROOT`, guarded by
  a lock file.
- Linux only for the local site (`/proc`, `taskset`, `smaps_rollup`).
- `--mem` on the local site is a watchdog kill, not a cgroup or rlimit: a task
  can briefly exceed it between polls.
- The Vast site measures capacity from load average, which includes other
  tenants on a shared host; set a per-box cap (`Q_VAST_CORE_CAP_<N>`) when that
  matters.
- The queue's Slurm adapter is a stub; Slurm is driven through wave scripts.
- Box creation is deliberately manual (`vastai create`): choosing an offer is a
  price judgement.
- The scheduler is priority + FIFO with simple packing; there is no fair-share
  or preemption between jobs.

## License

MIT, see `LICENSE`.
