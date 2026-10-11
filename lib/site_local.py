"""site_local — run one lanes.cmds line per task on this box.

Contract (mirrors the Batch adapter exactly so a job is portable between sites):
  $IN  = <Q_ROOT>/inputs[/<--inputs subpath>]
  $OUT = <Q_ROOT>/local_out/<job>/task_<i>
  the task's own stdout/stderr/exit code land in $OUT/_stdout.txt,
  $OUT/_stderr.txt, $OUT/_rc.txt  (atomic rename; _rc.txt == "0" means DONE)

Admission control:
  * threads: nproc - Q_LOCAL_RESERVE_THREADS, handed out as an explicit taskset
    CPU set per task, so a task that ignores its own thread flag still cannot
    take more than it was granted.
  * memory : MemAvailable - reserve - (headroom still owed to already running
    tasks), where the reserve is max(Q_LOCAL_RESERVE_MB, Q_LOCAL_RESERVE_PCT% of
    MemTotal).  A task whose process-group memory exceeds its --mem is KILLED
    (SIGKILL on the process group) and requeued, up to Q_MAX_MEM_KILLS times.
  * gpu    : --gpu 1 claims one of Q_LOCAL_GPU_SLOTS; the queue does not
    partition the device, it only stops two GPU tasks running at once.
  * resume : a task whose $OUT/_rc.txt is 0 is never re-run.

Crash safety: the launch is persisted BEFORE the process exists (state
LAUNCHING) and the wrapper writes its own pgid to $OUT/_pid.txt as its first
act, so a daemon killed between fork and state-save re-adopts the task instead
of running it a second time.
"""
import os
import signal
import subprocess
import time

import qcommon as Q

WRAPPER = r'''
set +e
mkdir -p "$OUT" || exit 97
printf '%s\n' "$$" > "$OUT/_pid.txt"
rm -f "$OUT/_rc.txt"
printf '%s\n' "$Q_LINE" > "$OUT/_cmd.txt"
( cd "$Q_WORKDIR" && bash -c "$Q_LINE" ) > "$OUT/_stdout.txt" 2> "$OUT/_stderr.txt"
rc=$?
printf '%s\n' "$rc" > "$OUT/_rc.txt.tmp" && mv "$OUT/_rc.txt.tmp" "$OUT/_rc.txt"
exit $rc
'''

LIVE = ("RUNNING", "LAUNCHING")


class LocalSite(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.reserve_threads = Q.cint(cfg, "Q_LOCAL_RESERVE_THREADS")
        self.reserve_mb = Q.local_reserve_mb(cfg)
        self.max_tasks = Q.cint(cfg, "Q_LOCAL_MAX_TASKS")
        self.nice = Q.cint(cfg, "Q_LOCAL_NICE")
        self.max_mem_kills = Q.cint(cfg, "Q_MAX_MEM_KILLS")
        self.gpu_slots = Q.cint(cfg, "Q_LOCAL_GPU_SLOTS")
        self.grace_polls = max(1, Q.cint(cfg, "Q_MEM_GRACE_POLLS"))
        self.ncpu = Q.nproc()
        self.pool = list(range(self.reserve_threads, self.ncpu))
        # live Popen handles, so finished tasks are REAPED instead of piling up as
        # zombies (after a daemon restart the handle is gone, init reaps instead)
        self.procs = {}

    # ---------------------------------------------------------------- helpers
    def outdir(self, name, i):
        return os.path.join(Q.P["localout"], name, "task_%d" % i)

    def running(self, states):
        out = []
        for st in states:
            if st.get("site") != "local":
                continue
            for i, t in sorted(st.get("tasks", {}).items(), key=lambda kv: int(kv[0])):
                if t.get("state") in LIVE:
                    out.append((st, int(i), t))
        return out

    def used_cpus(self, states):
        used = set()
        for _st, _i, t in self.running(states):
            used.update(t.get("cpus", []))
        return used

    def used_gpus(self, states):
        return sum(int(t.get("gpu", 0) or 0) for _st, _i, t in self.running(states))

    def capacity(self, states):
        """(free_threads, free_mem_mib, n_running) — memory counts the headroom
        still owed to running tasks, so two 2 GB tasks cannot both be admitted
        into 2 GB."""
        run = self.running(states)
        free_threads = len(self.pool) - len(self.used_cpus(states))
        owed = 0
        for _st, _i, t in run:
            owed += max(0, int(t.get("mem_mib", 0)) - int(t.get("rss_mib", 0)))
        free_mem = Q.mem_available_mib() - self.reserve_mb - owed
        return max(0, free_threads), free_mem, len(run)

    def impossible(self, spec):
        """Why this spec can NEVER run on this box, or None.  A job that can only
        ever WAIT must be told so, not left in the queue forever."""
        if spec["threads"] > len(self.pool):
            return ("--threads %d exceeds the %d schedulable threads (%d cores minus "
                    "a %d-thread reserve)" % (spec["threads"], len(self.pool),
                                              self.ncpu, self.reserve_threads))
        ceiling = Q.mem_total_mib() - self.reserve_mb
        if spec["mem_mib"] > ceiling:
            return ("--mem %dMiB exceeds the %dMiB this box can ever offer "
                    "(MemTotal %d minus a %dMiB reserve)"
                    % (spec["mem_mib"], ceiling, Q.mem_total_mib(), self.reserve_mb))
        if int(spec.get("gpu", 0) or 0) > self.gpu_slots:
            return "--gpu %s exceeds Q_LOCAL_GPU_SLOTS=%d" % (spec.get("gpu"), self.gpu_slots)
        return None

    def fits(self, spec, states):
        ft, fm, nrun = self.capacity(states)
        gpu = int(spec.get("gpu", 0) or 0)
        return (spec["threads"] <= ft and spec["mem_mib"] <= fm
                and nrun < self.max_tasks and spec["threads"] <= len(self.pool)
                and (not gpu or self.used_gpus(states) + gpu <= self.gpu_slots))

    # ---------------------------------------------------------------- adopt
    def adopt(self, st):
        """Re-adopt tasks whose launch was interrupted by a daemon death.

        A task recorded LAUNCHING may or may not have a live process; the
        wrapper's own $OUT/_pid.txt (written before it does anything else) is the
        only trustworthy witness.  Alive -> adopt; finished rc=0 -> DONE; gone ->
        requeue.  Without this, a `kill -9` between fork and state-save leaves a
        running task recorded as PENDING and the next cycle runs it AGAIN."""
        n = 0
        for i, t in st.get("tasks", {}).items():
            if t.get("state") != "LAUNCHING":
                continue
            n += 1
            out = self.outdir(st["name"], int(i))
            pid = None
            try:
                with open(os.path.join(out, "_pid.txt")) as fh:
                    pid = int(fh.read().strip())
            except (IOError, OSError, ValueError):
                pid = None
            if pid and Q.pid_alive(pid) and Q.proc_starttime(pid) is not None:
                t.update({"state": "RUNNING", "pid": pid, "pgid": pid,
                          "starttime": Q.proc_starttime(pid)})
                Q.event(st, "task %s re-adopted after a daemon restart (pid %d from "
                            "$OUT/_pid.txt) — NOT re-run" % (i, pid))
            elif Q.read_rc(out) == 0:
                t.update({"state": "DONE", "rc": 0,
                          "reason": "resume: finished during the daemon restart"})
                Q.event(st, "task %s finished while the daemon was down (rc=0)" % i)
            else:
                t["state"] = "PENDING"
                t["reason"] = "interrupted launch (no live pid, no rc=0) — requeued"
                Q.event(st, "task %s launch was interrupted by a daemon restart — "
                            "requeued" % i)
        return n

    # ---------------------------------------------------------------- launch
    def launch(self, st, i, line, states):
        spec = st["spec"]
        free = [c for c in self.pool if c not in self.used_cpus(states)]
        cpus = free[:spec["threads"]]
        if len(cpus) < spec["threads"]:
            return False
        out = self.outdir(st["name"], i)
        os.makedirs(out, exist_ok=True)
        for stale in ("_pid.txt", "_rc.txt"):
            try:
                os.unlink(os.path.join(out, stale))
            except OSError:
                pass
        env = dict(os.environ)
        indir = Q.P["inputs"]
        if spec.get("inputs"):
            indir = os.path.join(indir, spec["inputs"])
        nthr = str(spec["threads"])
        env.update({
            "IN": indir, "OUT": out, "Q_LINE": line,
            "Q_WORKDIR": spec.get("workdir") or Q.ROOT,
            "Q_JOB": st["name"], "Q_TASK": str(i), "Q_THREADS": nthr,
            "OMP_NUM_THREADS": nthr, "MKL_NUM_THREADS": nthr,
            "OPENBLAS_NUM_THREADS": nthr, "NUMEXPR_NUM_THREADS": nthr,
            "OMP_THREAD_LIMIT": nthr, "RAYON_NUM_THREADS": nthr,
            "POLARS_MAX_THREADS": nthr, "JULIA_NUM_THREADS": nthr,
            "TF_NUM_INTRAOP_THREADS": nthr, "TF_NUM_INTEROP_THREADS": "1",
        })
        t = st["tasks"].setdefault(str(i), {})
        # PERSIST THE INTENT FIRST: a daemon killed between here and the state
        # save at the end of the cycle must never re-run this task.
        t.update({"state": "LAUNCHING", "cpus": cpus, "mem_mib": spec["mem_mib"],
                  "gpu": int(spec.get("gpu", 0) or 0), "rss_mib": 0,
                  "rss_peak_mib": t.get("rss_peak_mib", 0),
                  "attempts": t.get("attempts", 0) + 1,
                  "mem_kills": t.get("mem_kills", 0), "over_polls": 0,
                  "timeout_sec": int(spec.get("timeout_sec", 0) or 0),
                  "started": time.time(), "rc": None, "pid": None, "pgid": None})
        Q.save_state(st)
        argv = ["taskset", "-c", ",".join(str(c) for c in cpus),
                "nice", "-n", str(self.nice), "bash", "-c", WRAPPER]
        devnull = open(os.devnull, "wb")
        try:
            pr = subprocess.Popen(argv, env=env, cwd=spec.get("workdir") or Q.ROOT,
                                  stdin=subprocess.DEVNULL, stdout=devnull,
                                  stderr=devnull, start_new_session=True)
        finally:
            devnull.close()
        self.procs[(st["name"], i)] = pr
        t.update({"state": "RUNNING", "pid": pr.pid, "pgid": pr.pid,
                  "starttime": Q.proc_starttime(pr.pid)})
        Q.save_state(st)
        Q.event(st, "local task %d launched pid=%d cpus=%s mem_cap=%dMiB%s"
                % (i, pr.pid, cpus, spec["mem_mib"],
                   " gpu=1" if t["gpu"] else ""))
        return True

    # ---------------------------------------------------------------- poll
    def poll(self, states, table=None):
        """Update every live local task: timeout + memory watchdog + exit
        collection.  Returns True if any task changed state."""
        run = self.running(states)
        if not run:
            return False
        for key, pr in list(self.procs.items()):
            if pr.poll() is not None:      # reap: no zombie is left behind
                del self.procs[key]
        table = table if table is not None else Q.proc_table()
        now = time.time()
        changed = False
        for st, i, t in run:
            if t.get("state") == "LAUNCHING":
                continue                   # no pid yet; adopt() owns this case
            pgid = t.get("pgid")
            rss = sum(r for (p, r) in table.values() if p == pgid) // (1024 * 1024)
            alive = t.get("pid") in table and (
                t.get("starttime") is None
                or Q.proc_starttime(t["pid"]) == t["starttime"])
            t["rss_mib"] = rss
            t["rss_peak_mib"] = max(t.get("rss_peak_mib", 0), rss)
            cap = int(t.get("mem_mib", 0))
            tmo = int(t.get("timeout_sec", 0) or 0)
            if alive and tmo and now - t.get("started", now) > tmo:
                self._kill(st, i, t, "timeout: ran %ds > --timeout %ds"
                           % (int(now - t.get("started", now)), tmo), fatal=True)
                changed = True
                continue
            if alive and cap and rss > cap:
                # RSS sums shared pages once PER PROCESS, so a task that forks
                # workers looks 2-4x bigger than it is.  Confirm with PSS, and
                # require the group to stay over for Q_MEM_GRACE_POLLS polls, so a
                # momentary spike is not a kill.
                pss = Q.pgid_pss_mib(pgid, table)
                t["pss_mib"] = pss
                real = pss if pss is not None else rss
                if real > cap:
                    t["over_polls"] = t.get("over_polls", 0) + 1
                    if t["over_polls"] >= self.grace_polls:
                        self._kill(st, i, t, "memory %dMiB (PSS%s) > cap %dMiB for "
                                             "%d consecutive polls"
                                   % (real, "" if pss is not None else "=RSS, "
                                      "no smaps_rollup", cap, t["over_polls"]))
                        changed = True
                        continue
                else:
                    t["over_polls"] = 0
            elif alive:
                t["over_polls"] = 0
            if not alive:
                rc = Q.read_rc(self.outdir(st["name"], i))
                t["ended"] = time.time()
                t["rc"] = rc
                strays = [p for p in Q.pgid_pids(pgid, table) if p != t.get("pid")]
                if strays and t.get("pid") in table:
                    # The leader PID is alive but failed the start-time check.
                    # Its new process group belongs to somebody else.
                    Q.event(st, "local task %d leader pid was reused — not signaling "
                            "the new process group" % i)
                elif strays:
                    # the leader is gone but the group is not: sweep, or those
                    # processes keep the box busy with nothing accounting for them
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except OSError:
                        pass
                    Q.event(st, "local task %d left %d stray process(es) in its "
                                "group after exiting — killed" % (i, len(strays)))
                if rc == 0:
                    t["state"] = "DONE"
                else:
                    t["state"] = "FAILED"
                    t["reason"] = "rc=%s" % rc if rc is not None \
                        else "no _rc.txt (killed externally: OOM killer, reboot, " \
                             "or an outside kill)"
                Q.event(st, "local task %d %s rc=%s peak_rss=%dMiB"
                        % (i, t["state"], rc, t["rss_peak_mib"]))
                changed = True
        return changed

    def _kill(self, st, i, t, why, fatal=False):
        try:
            os.killpg(t["pgid"], signal.SIGKILL)
        except OSError:
            pass
        t["ended"] = time.time()
        t["reason"] = why
        t["over_polls"] = 0
        if fatal:
            t["state"] = "FAILED"
            t["rc"] = None
            Q.event(st, "local task %d KILLED (%s) — not requeued" % (i, why))
        else:
            t["mem_kills"] = t.get("mem_kills", 0) + 1
            if t["mem_kills"] >= self.max_mem_kills:
                t["state"] = "FAILED"
                t["rc"] = None
                Q.event(st, "local task %d MEM-KILLED (%s) attempt %d — giving up"
                        % (i, why, t["mem_kills"]))
            else:
                t["state"] = "PENDING"
                Q.event(st, "local task %d MEM-KILLED (%s) attempt %d — requeued"
                        % (i, why, t["mem_kills"]))
        # the wrapper died with the group: make sure no stale rc or pid lingers
        out = self.outdir(st["name"], i)
        try:
            if os.path.exists(os.path.join(out, "_rc.txt")) and Q.read_rc(out) != 0:
                os.unlink(os.path.join(out, "_rc.txt"))
        except OSError:
            pass
        try:
            os.unlink(os.path.join(out, "_pid.txt"))
        except OSError:
            pass

    def cancel(self, st):
        for i, t in st.get("tasks", {}).items():
            if t.get("state") in LIVE and t.get("pgid"):
                try:
                    os.killpg(t["pgid"], signal.SIGKILL)
                except OSError:
                    pass
                t["state"] = "CANCELLED"
                t["ended"] = time.time()
