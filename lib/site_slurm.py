"""site_slurm — STUB queue adapter for a Slurm cluster.

Nothing here runs.  (Slurm work in this project went through the wave tooling
instead: `fleet gen` emits a cap-aware, re-runnable sbatch submit script; see
fleet/wavegen.py.  This stub records how the queue itself would drive Slurm.)  Every entry point raises NotImplementedError; the sbatch form
the implementation must generate is written out below so the port is mechanical.

The contract the daemon needs from any site adapter:
    refresh()                -> one cheap poll per cycle, cached
    free_cpus()              -> capacity left under our own cap
    plan_chunk(spec, remaining) -> {"indices", "parallelism", ...} or None
    submit_chunk(st, indices, spot, kind, first) -> chunk record
    update_chunks(st)        -> realized per-task state from the cached poll
    pull(st, force)          -> outputs into cache/<job>/ + count/witness verify
    done_indices(st)         -> {i : $OUT/task_i/_rc.txt == 0}
    cancel(st)

SLURM MAPPING (partition $Q_SLURM_PARTITION, account $Q_SLURM_ACCOUNT):

  one `q` job  ->  one sbatch ARRAY per chunk, array indices = the chunk's task
  indices, so the $OUT layout and the resume rule are byte-identical to Batch.

    #!/bin/bash
    #SBATCH --job-name=<job>-c<k>
    #SBATCH --account=$Q_SLURM_ACCOUNT
    #SBATCH --partition=$Q_SLURM_PARTITION
    #SBATCH --array=<comma-separated chunk indices>%<parallelism>
    #SBATCH --cpus-per-task=<spec.cpus>
    #SBATCH --mem=<spec.mem_mib>M
    #SBATCH --time=<spec.timeout_sec as HH:MM:SS>
    #SBATCH --output=/dev/null
    #SBATCH --error=/dev/null
    IDX=$SLURM_ARRAY_TASK_ID
    export IN=$Q_SLURM_SCRATCH/inputs[/<spec.inputs>]
    export OUT=$Q_SLURM_SCRATCH/jobs/<job>/out/task_$IDX
    mkdir -p "$OUT"
    [ -f "$OUT/_rc.txt" ] && [ "$(cat "$OUT/_rc.txt")" = 0 ] && exit 0   # resume
    line=$(sed -n "$((IDX+1))p" $Q_SLURM_SCRATCH/jobs/<job>/lanes.cmds)
    ( cd $Q_SLURM_SCRATCH && bash -c "$line" ) > "$OUT/_stdout.txt" 2> "$OUT/_stderr.txt"
    rc=$?; printf '%s\n' "$rc" > "$OUT/_rc.txt.tmp" && mv "$OUT/_rc.txt.tmp" "$OUT/_rc.txt"
    exit $rc

  ONE poll per cycle, like the Batch list call:
      sacct -n -P -X -o JobID,JobName,State -j <comma-joined chunk job ids>
  never `squeue` in a loop, and never one call per job.  A submit ledger proves
  acceptance, never execution: `sacct` realized state is the only truth.

  Capacity: Q_SLURM_CPU_CAP (our self-imposed cap) minus the cpus of our own
  RUNNING/PENDING array elements from that same sacct call.  Preemption does not
  exist here, so `resumable` degrades to plain requeue: --spot is ignored and
  submit_chunk(spot=False) is the only mode.

  Transfers: rsync over the cluster's ssh ControlMaster socket, single stream,
  >= 15 min apart, exactly as the Batch adapter pulls.
"""


class SlurmSite(object):
    NOTE = "Slurm queue adapter not implemented — see module docstring for the sbatch form."

    def __init__(self, cfg):
        self.cfg = cfg

    def _no(self, what):
        raise NotImplementedError("site_slurm.%s: %s" % (what, self.NOTE))

    def refresh(self):                                   self._no("refresh")
    def free_cpus(self):                                 self._no("free_cpus")
    def plan_chunk(self, spec, remaining):               self._no("plan_chunk")
    def submit_chunk(self, st, indices, spot, kind, first): self._no("submit_chunk")
    def update_chunks(self, st):                         self._no("update_chunks")
    def pull(self, st, force=False):                     self._no("pull")
    def done_indices(self, st):                          self._no("done_indices")
    def cancel(self, st):                                self._no("cancel")
