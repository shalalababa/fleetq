# Lessons: incidents and the design decisions they produced

Each entry is a real failure from running this tool, what it cost, and the
mechanism that now prevents it. Research details are omitted; the numbers are
the measured ones.

## Data safety

**Destroying a box is a deletion.** A cleanup tier once deleted trained models
that later turned out to be needed, and GPU nondeterminism meant they could not
be re-created bit-for-bit. Destroying a rented box deletes every run dir on it
just as surely, with no `rm` typed.
*Decision:* `fleet destroy` reports by default and, with `--yes`, destroys after an
inventory proves every run dir on the box exists in the archive with counters
at least as large (checkpoint step, score lines, file count, bytes) and
matching identity-file hashes. Greater-or-equal, not equal: an archive
accumulates runs from several boxes. An unreadable inventory fails the gate;
a valid inventory with zero run dirs has no archive comparison to make.
`--force-unsafe` permits unarchived dirs after an additional typed confirmation.

**A symlink is not an archive.** A trainer wrote checkpoints under a cache
directory and symlinked them into the run dir. Pulls re-planted dangling links
on the archive host, and a routine re-pull on top of an already dereferenced
3.46 GB archive replaced real files with dangling links: 16 of 16 runs became
0 of 16.
*Decision:* payload rsyncs use `--copy-unsafe-links`.

**Hard-linked bundles are views.** A bundle built with `cp -al` shares inodes
with the live runroot. An in-place resume appended to a bundle that was
supposed to be frozen and its manifest then failed on 1282 files.
*Decision:* `bundle.sh` records how many sampled files are hard links in
`_BUNDLE_MODE.txt` and says plainly when the bundle is a view.

**Verify on bytes and hashes, not on `du` or on the transfer's own filters.**
Disk-usage totals differ across filesystems, and checking a pull with the same
exclude list the pull used can only ever confirm the pull.
*Decision:* the Batch pull verifier compares file count, total bytes, manifest
sha256s and a witness file (the largest); the destroy gate enumerates the box's
tree independently of any pull filter.

## Transfers

**A home link that is token-bucket shaped.** Short transfers arrived at
~10 MB/s; sustained ones collapsed to ~70 kB/s (4.19 MB in 60 s of a 393 MB
file). GCS to the same machine measured 14.3 MB/s, unshaped. Pulling multi-GB
results directly from boxes took most of a day.
*Decision:* the `gcs` relay. The box uploads to GCS with a 1-hour token
minted by impersonating a bucket-scoped service account; the token travels
over ssh stdin and is read by curl from a pipe descriptor. The controller
downloads over the fast path, verifies a per-file sha256 manifest, and only
then advances the box's relay marker.

**`--append-verify` skips same-size files.** That is right for immutable
payload and wrong for two things. A checkpoint pointer file (`latest`) is
rewritten on every save but always the same length, so ten pulled runs kept
pointing at the checkpoint from the first pull (~105k steps, while ~495k-step
checkpoints sat beside them) and a downstream evaluation reported OK on the
wrong model. And a VERSION stamp of constant length never updated on a box
after a code push, so the box looked current while running old code.
*Decision:* two rsync option sets. Code uses `--checksum`. Result pulls run a
second, checksum-forced pass over small mutable witness files.

**rsync exit 24 is routine on a live box.** "Files vanished" happens whenever
checkpoints rotate mid-transfer. Under `set -e` it aborted a multi-box pull
after the second box, and the run looked finished.
*Decision:* the generated pull script tolerates 24, not 23, and reports the
boxes that failed by name.

**rsync include lists need every parent.** rsync descends only into included
directories, so a wanted path without its parents transfers nothing, which
looks like a slow network.
*Decision:* the include chain is generated from the spec's declared outputs,
with both the bare and `/***` form for each leaf.

## Remote execution

**ssh re-parses its arguments.** ssh joins arguments into one string for a
remote login shell. An argument with a space split and shifted every later
positional, and a submit queued 2 commands instead of 8 with no error.
*Decision:* `fl_ssh_script` passes scripts on stdin and arguments pre-quoted
with `printf %q`.

**ssh eats stdin.** An ssh inside a script that is itself fed on stdin
consumed the rest of the script; every later command silently never ran.
*Decision:* every non-script ssh uses `-n`.

**A detached launch's exit status means nothing.** Vast tasks start with
`nohup`; the ssh that started them returns 0 whether or not the task will
succeed.
*Decision:* completion is read only from a pulled `_rc.txt`.

**An assignment prefix cannot precede a subshell.** A lane line of the form
`NAME=x ( ... )` is a bash syntax error. All 16 tasks of one stage died with
rc=2 in 0 s. Paired stages used a different shape and never showed it.
*Decision:* single runs and pairs are emitted by one code path in the same
shape, `FL_TASK_NAME=x bash -lc '...'`.

**A stale script silently ignores new variables.** A lane does `cd $REPO` and
runs what is there. On boxes whose copy predated an override, one variable in
the command prefix bound nothing and four runs trained the wrong arm for
6.3 h each, while the other variables in the same prefix worked.
*Decision:* the generated preflight greps each producer script on the box for
every variable the wave sets, and compares the box's VERSION stamp with the
commit the wave was generated from.

**A broken binary presents as a network error.** On one box `/usr/bin/rsync`
was a 0-byte stub (the disk filled mid-write); the controller saw "connection
unexpectedly closed".
*Decision:* preflight checks that rsync and tar exist, are executable, and are
non-empty.

**A missing output folder fails at the very end.** A stage that writes
`<folder>/<run>.json` did its whole GPU pass and died on the final `open()`.
*Decision:* lane lines create the parents of declared file outputs, never the
run dir itself (an empty run dir can look like a resume).

## Scheduling and capacity

**Lanes that finish at different times idle GPUs.** Runs dealt round-robin
onto lanes left one lane running long after the others had drained; across
one campaign that was 33.4 idle GPU-hours on paid boxes.
*Decision:* `fleet pack` assigns runs longest-first over measured durations so
lanes drain together, and the board re-projects drain time per lane.

**`nproc` is not the box's CPU quota.** Container boxes report the host's core
count, while the cgroup quota is lower; load average also includes other
tenants on a shared host.
*Decision:* Vast capacity compares measured 1-minute load average with a
configured core cap (`Q_VAST_CORE_CAP`, per-box override).

**RSS double-counts shared pages.** Forked data-loader workers share most of
their memory; summing RSS over the process group overstated usage and killed
tasks that fit.
*Decision:* RSS is the cheap trigger; PSS from `smaps_rollup` confirms before
any kill, and a kill needs several consecutive confirmed polls.

**PIDs get reused.** After a daemon restart, a PID from `_pid.txt` can belong
to an unrelated process.
*Decision:* re-adoption matches PID plus process start time and excludes
zombies.

**Submit is not outcome.** A submission that the API accepted can still never
run (quota, stockout, a lost response).
*Decision:* submits are persisted before the call, `ALREADY_EXISTS` is
adopted, a chunk whose submit was interrupted and that is still missing from
the Batch listing after three cycles becomes `SUBMIT_LOST` (its tasks are
replanned under a new chunk name), and stalled chunks are warned on and then
resubmitted on demand.

**Per-user Slurm caps reject, they do not queue.** Submissions past the cap
were dropped.
*Decision:* the generated `submit_slurm.sh` submits up to the cap, skips runs
already queued or complete, and reports how many are left; rerun until zero.

**Environment leaks into Slurm jobs.** `--export=ALL` carried loaded modules
from the submitting shell into every job; a CUDA module shadowed the job's
pinned libraries and every GPU job segfaulted ~30 s in for a day.
*Decision:* the submit script refuses when modules outside a configurable
baseline are loaded.

**Heterogeneous GPU partitions split panels.** A bfloat16 evaluation was
byte-identical when rerun on the same GPU model but moved its metric by up to
0.11 across models, and one panel had been split across two models.
*Decision:* Slurm stages take a GPU constraint per stage (or a default), and
results from different GPU models are not pooled.

## Checking

**Markers are not evidence.** A run can exit 0 and write its DONE marker
having trained nothing (a resume-from-latest plus idempotent-skip hole did
exactly that across two waves).
*Decision:* `fleet check` evaluates the spec's predicates, prints every
observed counter whether or not it passed, and flags runs whose realized
training disagrees with the rest of their cell.

**One unreadable file must not hide a wave.** A mode-000 file raised
`PermissionError` and killed the whole check; 31 other runs went unreported.
*Decision:* an unreadable predicate is reported as not done for that run
only.

**Timestamp-only checkpoint names end in digits.** The trailing-number parser
read the microsecond field as a training step and the outlier scan flagged
noise it had manufactured.
*Decision:* bare timestamp names are recognised and carry no step.

**Comment lines become tasks.** The Batch runner indexes the command file by
line, so a comment line becomes a task.
*Decision:* templates say to strip comments before submitting.
