"""site_vast — run lanes.cmds tasks on rented Vast.ai boxes.

Contract mirrors site_batch/site_local exactly, so a job is portable between
sites (the whole point of --site either):
    $IN  = QVAST_IN on the box (default /workspace/inputs)
    $OUT = <remote_root>/<job>/out/task_<i>
    $OUT/_cmd.txt, _stdout.txt, _stderr.txt, _rc.txt  (atomic; "0" == DONE)
    a task whose _rc.txt is 0 is NEVER re-run

CAPACITY IS LOAD, NOT CORES.  The boxes this was built on were cgroup-capped at
cpu.max 6144000/100000 = 61.4 cores while nproc reported the HOST's 256, which
is a lie for scheduling.  GPU jobs on the same box take a large and MOVING
share of that, so this adapter admits against each box's MEASURED 1-minute
load against the cap (Q_VAST_CORE_CAP, per-box override Q_VAST_CORE_CAP_<id>).  Headroom is re-measured once
per cycle; a box at or over the cap contributes zero.

ONE POLL PER CYCLE.  refresh() opens one ssh per box for load + liveness, and
nothing else polls.  Chunk completion is read from the pulled _rc.txt files, not
from the runner's own log: a submit ledger proves acceptance, never execution.

TRANSFERS: at most one pull per Q_PULL_INTERVAL_SEC per job, run detached by
pull_worker.py.  Q_VAST_RELAY picks the route: unset = direct rsync, `gcs` =
box -> GCS -> local (lib/vast_relay_pull.sh), `cluster` = a two-hop rsync via a
cluster login node you hold an ssh ControlMaster socket to, `r2` = cluster hop
then Cloudflare R2.
"""
import os
import subprocess
import time

import qcommon as Q

TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")
NEVER_RAN = ("SUBMIT_FAILED",)

SSH_OPTS = ["-n", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30"]


def _read_instances(path):
    """Boxes come from `fleet refresh`'s generated instances.env (IP<N>/PORT<N>)."""
    ips, ports = {}, {}
    try:
        for line in open(os.path.expanduser(path)):
            line = line.strip()
            if not line.startswith("export "):
                continue
            k, _, v = line[len("export "):].partition("=")
            k = k.strip(); v = v.strip().strip('"').strip("'")
            if k.startswith("IP") and k[2:].isdigit():
                ips[k[2:]] = v
            elif k.startswith("PORT") and k[4:].isdigit():
                ports[k[4:]] = v
    except IOError:
        return []
    return [{"id": n, "ip": ips[n], "port": ports[n]}
            for n in sorted(ips) if n in ports]


class VastSite(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.dry = os.environ.get("Q_DRY_RUN") == "1"
        self.enabled = str(cfg.get("Q_VAST_ENABLE", "1")) not in ("0", "")
        self.cap = float(cfg.get("Q_VAST_CORE_CAP", 61.4))
        self.root = cfg.get("Q_VAST_REMOTE_ROOT", "/workspace/qvast")
        self.user = cfg.get("Q_VAST_USER", "root")
        self.timeout = Q.cint(cfg, "Q_VAST_SSH_TIMEOUT") or 60
        self.pull_interval = Q.cint(cfg, "Q_PULL_INTERVAL_SEC")
        self.boxes = _read_instances(Q.root_path(cfg.get(
            "Q_VAST_INSTANCES", "fleet_state/instances.env")))
        self.runner_src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "vast_runner.sh")
        self.load = {}          # box id -> 1-min load, refreshed once per cycle
        self.alive = {}
        self.list_ok = False

    # ------------------------------------------------------------ plumbing
    def _ssh(self, box, remote, timeout=None):
        argv = ["ssh"] + SSH_OPTS + ["-p", box["port"],
                                     "%s@%s" % (self.user, box["ip"]), remote]
        return Q.run_cmd(argv, timeout or self.timeout, kind="vast-ssh", name=box["id"])

    def _rsync(self, args, timeout):
        return Q.run_cmd(["rsync"] + args, timeout, kind="vast-rsync", name="-")

    def _ssh_e(self, box):
        return "ssh %s -p %s" % (" ".join(SSH_OPTS[1:]), box["port"])

    def box_by_id(self, bid):
        for b in self.boxes:
            if b["id"] == bid:
                return b
        return None

    # ------------------------------------------------------------- capacity
    def refresh(self):
        """One ssh per box per cycle: 1-min load and nothing else."""
        self.load, self.alive = {}, {}
        if not self.enabled or not self.boxes or self.dry:
            self.list_ok = bool(self.dry and self.boxes)
            if self.dry:
                for b in self.boxes:
                    self.load[b["id"]] = 0.0
                    self.alive[b["id"]] = True
            return
        ok = False
        for b in self.boxes:
            r = self._ssh(b, "cut -d' ' -f1 /proc/loadavg", timeout=30)
            if r.rc == 0 and r.out.strip():
                try:
                    self.load[b["id"]] = float(r.out.strip())
                    self.alive[b["id"]] = True
                    ok = True
                    continue
                except ValueError:
                    pass
            self.alive[b["id"]] = False
        self.list_ok = ok

    def cap_for(self, bid):
        """Per-box cap override Q_VAST_CORE_CAP_<id>, else the global cap.

        WHY: /proc/loadavg is not container-isolated on every host.  One box
        reported load ~65 while its OWN container was idle (total %CPU of every
        process in it measured at 0.3): that load belonged to other tenants of
        the physical host.  Against the
        global 61.4 cap its headroom is permanently 0, so a job pinned there
        can never be scheduled even though both its GPUs are free.  The
        override states the box's usable cores ON TOP of its foreign baseline
        and leaves every other box on the measured-load rule unchanged."""
        v = self.cfg.get("Q_VAST_CORE_CAP_%s" % bid)
        if v in (None, ""):
            return self.cap
        try:
            return float(v)
        except (TypeError, ValueError):
            return self.cap

    def headroom(self, bid):
        if not self.alive.get(bid):
            return 0.0
        cap = self.cap_for(bid)
        return max(0.0, cap - self.load.get(bid, cap))

    def free_cpus(self):
        return int(sum(self.headroom(b["id"]) for b in self.boxes))

    def best_box(self, only=None):
        """Lowest load wins, which is the same as most headroom at a common cap.
        `only` (a box id from instances.env, the N of IP<N>) pins the choice to
        that box; a pinned box that is not alive yields None so the job waits."""
        live = [b for b in self.boxes if self.alive.get(b["id"])]
        if only is not None:
            live = [b for b in live if str(b["id"]) == str(only)]
        if not live:
            return None
        return min(live, key=lambda b: self.load.get(b["id"], 1e9))

    def plan_chunk(self, spec, remaining):
        remaining = list(remaining)
        if not self.list_ok or not remaining:
            return None
        b = self.best_box(spec.get("vast_box"))
        if b is None:
            return None
        per = max(1.0, float(spec.get("cpus", 1)))
        n = int(self.headroom(b["id"]) // per)
        if n < 1:
            return None
        # CHUNK SIZE vs PARALLELISM.  These used to be the same number, which
        # starves short tasks: a job at --cpus 45 gets n=1, so the daemon ships
        # ONE task per chunk and the box idles a whole dispatch cycle (~2 min)
        # between tasks that each take seconds.  Measured 15 Sep on box 4:
        # 6-7 s of compute per 2 min of wall clock, a ~5% duty cycle.
        #
        # --vast-chunk N ships up to N tasks in one chunk while leaving the
        # WORKER count derived from cpus (submit_chunk recomputes `par`
        # independently), so the runner executes them back-to-back on the box
        # with no daemon round trip, and concurrency -- and therefore any
        # timing or memory measurement -- is unchanged.
        #
        # Cost: a chunk is bound to one box until it drains, so a large
        # --vast-chunk reduces cross-box spreading.  Default 0 keeps the old
        # one-chunk-per-slot behaviour; it is most useful with --vast-box.
        take = min(n, len(remaining))
        chunk = int(spec.get("vast_chunk") or 0)
        if chunk > 0:
            take = min(max(n, chunk), len(remaining))
        return {"indices": remaining[:take], "box": b["id"], "parallelism": n}

    # --------------------------------------------------------------- submit
    def submit_chunk(self, st, indices, spot, kind, first):
        """Stage lanes.cmds + the runner, then launch it DETACHED.

        An ssh whose remote command backgrounds a process does not return even
        with every fd redirected — it blocks to the client timeout while the
        setsid'd process runs fine.  So the launch is fire-and-verify: we do not
        trust the launch call's rc, we confirm the pid file on the next cycle.
        """
        name = st["name"]
        plan = {"indices": list(indices)}
        box = self.box_by_id(st.get("pending_box") or "") or self.best_box()
        if box is None:
            return None
        jobdir = "%s/%s" % (self.root, name)
        tag = "%s-c%d" % (name, len(st.get("chunks", [])))
        rec = {"job": tag, "tag": tag, "box": box["id"], "indices": list(indices),
               "state": "SUBMITTING", "submitted": time.time(), "counts": {},
               "kind": kind, "spot": 0}
        # record BEFORE the remote call, so a daemon killed mid-submit still
        # knows the chunk exists (site_batch B2 was exactly this bug)
        st.setdefault("chunks", []).append(rec)
        Q.save_state(st)

        cmds_src = os.path.join(Q.P["cmds"], name + ".cmds")
        idx_local = os.path.join(Q.P["tmp"], "%s.idx" % tag)
        with open(idx_local, "w") as f:
            f.write("\n".join(str(i) for i in indices) + "\n")
        if self.dry:
            rec["state"] = "RUNNING"
            Q.log("VASTCALL submit (dry) job=%s box=%s n=%d" % (tag, box["id"], len(indices)))
            Q.save_state(st)
            return rec
        r = self._ssh(box, "mkdir -p %s/out" % jobdir, timeout=40)
        if r.rc != 0:
            rec["state"] = "SUBMIT_FAILED"; rec["err"] = r.err[:200]
            st["submit_fails"] = st.get("submit_fails", 0) + 1
            Q.event(st, "submit FAILED on box %s: mkdir rc=%d %s" % (box["id"], r.rc, r.err[:120]))
            return rec
        staging = ((cmds_src, "%s/lanes.cmds" % jobdir),
                   (idx_local, "%s/%s.idx" % (jobdir, tag)),
                   (self.runner_src, "%s/runner.sh" % jobdir))
        # Optional inbound leg over GCS (Q_VAST_INPUTS_VIA_GCS=1).  local->box
        # rsync is 0.8 MB/s; local->GCS ~15 MB/s and box<-GCS 24.4 MB/s, so for
        # anything above ~1 MB this is a ~30x win.  For the few-KB control files
        # below the round trips dominate, so rsync stays the default and is also
        # the automatic fallback whenever the GCS path returns non-zero.
        staged_ok = False
        if str(self.cfg.get("Q_VAST_INPUTS_VIA_GCS", "")) == "1":
            for k in ("Q_BUCKET", "Q_GCLOUD", "Q_VAST_GCS_SA",
                      "Q_VAST_GCS_CLEANUP"):
                v = self.cfg.get(k)
                if v not in (None, ""):
                    os.environ[k] = str(v)
            helper = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "vast_inputs_push.sh")
            gargv = [helper, self.user, box["ip"], str(box["port"]), tag] \
                + ["%s:%s" % (a, b) for a, b in staging]
            rg = Q.run_cmd(gargv, 300, kind="vast-inputs-gcs", name=tag)
            staged_ok = (rg.rc == 0)
            if not staged_ok:
                Q.event(st, "inputs via GCS rc=%d on box %s, falling back to rsync"
                        % (rg.rc, box["id"]))
        if not staged_ok:
            for src, dst in staging:
                r = self._rsync(["-q", "-e", self._ssh_e(box), src,
                                 "%s@%s:%s" % (self.user, box["ip"], dst)], 120)
                if r.rc != 0:
                    rec["state"] = "SUBMIT_FAILED"; rec["err"] = r.err[:200]
                    st["submit_fails"] = st.get("submit_fails", 0) + 1
                    Q.event(st, "submit FAILED on box %s: staging %s rc=%d %s"
                            % (box["id"], os.path.basename(src), r.rc, r.err[:120]))
                    return rec
        par = min(len(indices), max(1, int(self.headroom(box["id"]) //
                                           max(1.0, float(st["spec"].get("cpus", 1))))))
        launch = ("chmod +x %s/runner.sh; cd %s && QVAST_THREADS=%d nohup setsid "
                  "bash -c 'exec %s/runner.sh %s %s/%s.idx %d' "
                  ">> %s/_runner.out 2>&1 < /dev/null & exit 0"
                  % (jobdir, jobdir, int(st["spec"].get("threads", 1) or 1),
                     jobdir, jobdir, jobdir, tag, par, jobdir))
        self._ssh(box, launch, timeout=25)      # rc is meaningless here, by design
        rec["state"] = "RUNNING"
        rec["parallelism"] = par
        st["submit_fails"] = 0
        Q.log("VASTCALL submit job=%s box=%s n=%d par=%d" % (tag, box["id"], len(indices), par))
        Q.event(st, "chunk %s launched on box %s: %d task(s), %d worker(s)"
                % (tag, box["id"], len(indices), par))
        Q.save_state(st)
        return rec

    # --------------------------------------------------------------- status
    def update_chunks(self, st):
        """Liveness only. Completion is decided by pulled _rc.txt, never by this."""
        for c in st.get("chunks", []):
            if c["state"] in TERMINAL or c["state"] in NEVER_RAN:
                continue
            box = self.box_by_id(c["box"])
            if box is None or not self.alive.get(c["box"]):
                continue
            jobdir = "%s/%s" % (self.root, st["name"])
            if self.dry:
                c["state"] = "SUCCEEDED"
                continue
            r = self._ssh(
                box, "test -f %s/_runner.pid && echo LIVE || echo GONE" % jobdir, timeout=30)
            if r.rc != 0:
                continue                       # transient ssh trouble, not a verdict
            if "GONE" in r.out:
                c["state"] = "SUCCEEDED"       # the runner exited; rc files are the truth

    # ----------------------------------------------------------------- pull
    def pull(self, st, force=False):
        dest = os.path.join(Q.P["cache"], st["name"])
        Q.ensure_dir(dest) if hasattr(Q, "ensure_dir") else os.makedirs(dest, exist_ok=True)
        boxes = sorted({c["box"] for c in st.get("chunks", [])})
        argv = None
        for bid in boxes:
            box = self.box_by_id(bid)
            if box is None or not self.alive.get(bid):
                continue
            if self.dry:
                continue
            src = "%s@%s:%s/%s/out/" % (self.user, box["ip"], self.root, st["name"])
            # A job is scheduled to one box at a time, so a single rsync covers
            # its current outputs.  Keep that one stream detached from qd.
            argv = ["rsync", "-a", "--checksum", "-e", self._ssh_e(box), src, dest + "/"]
            relay_mode = str(self.cfg.get("Q_VAST_RELAY", "") or "")
            if relay_mode in ("cluster", "r2", "gcs"):
                # Two-hop relay: a cluster login node initiates the pull from
                # the box (hop 1, 14.3 MB/s measured) so results are safe there
                # first.  Hop 2 is then either rsync cluster->local (mode
                # "cluster", ~115 kB/s) or cluster->R2->local over HTTPS (mode
                # "r2", ~12 s for 333 MB).  Mode "gcs" skips the cluster:
                # box->GCS->local, 20.9 MB/s up and 14.3 MB/s down, with a 1 h
                # impersonated token handed to the box over ssh stdin.  Direct
                # box->local measured 79 kB/s on a shaped home link, which is
                # why none of them is that.
                relay = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "vast_relay_pull.sh")
                argv = [relay, self.user, box["ip"], str(box["port"]), self.root,
                        st["name"], dest,
                        self.cfg.get("Q_VAST_RELAY_HOST", ""),
                        self.cfg.get("Q_VAST_RELAY_ROOT", ""),
                        relay_mode]
                # r2 mode tunables reach the helper through the child's env;
                # every one has a default in the script, so absent keys are fine.
                for k in ("Q_VAST_R2_REMOTE", "Q_VAST_R2_PREFIX",
                          "Q_VAST_R2_EXPIRE", "Q_VAST_R2_ZSTD",
                          "Q_VAST_R2_ZSTD_THREADS", "Q_VAST_R2_CLEANUP",
                          "Q_BUCKET", "Q_GCLOUD", "Q_VAST_GCS_SA",
                          "Q_VAST_GCS_ZSTD", "Q_VAST_GCS_ZSTD_THREADS",
                          "Q_VAST_GCS_CLEANUP"):
                    v = self.cfg.get(k)
                    if v not in (None, ""):
                        os.environ[k] = str(v)
            break
        if argv is None:
            return "idle"
        pull_timeout = Q.cint(self.cfg, "Q_VAST_PULL_TIMEOUT") or 900
        phase = Q.async_transfer(st, argv, pull_timeout, force=force,
                                 interval=self.pull_interval, kind="vast-pull")
        if phase != "done":
            return phase
        p = st.setdefault("pull", {})
        p["ok"] = p.get("rc") == 0
        n = len([d for d in os.listdir(dest)]) if os.path.isdir(dest) else 0
        p["files"] = n
        Q.log("VASTCALL pull job=%s boxes=%s dirs=%d ok=%s"
              % (st["name"], ",".join(boxes), n, p["ok"]))
        return "done"

    def done_indices(self, st):
        """Ground truth: a pulled $OUT/task_i/_rc.txt of 0."""
        dest = os.path.join(Q.P["cache"], st["name"])
        done = set()
        for i in range(st["ntasks"]):
            if Q.read_rc(os.path.join(dest, "task_%d" % i)) == 0:
                done.add(i)
        return done

    def cancel(self, st):
        for c in st.get("chunks", []):
            if c["state"] in TERMINAL:
                continue
            box = self.box_by_id(c["box"])
            if box is None or self.dry:
                c["state"] = "CANCELLED"
                continue
            jobdir = "%s/%s" % (self.root, st["name"])
            # bracket form: a bare pkill -f matches the invoking shell itself
            self._ssh(box, "p=$(cat %s/_runner.pid 2>/dev/null); "
                           "[ -n \"$p\" ] && kill -TERM -- -$p 2>/dev/null; "
                           "pkill -f '[v]ast_runner.sh %s' 2>/dev/null; true"
                      % (jobdir, jobdir), timeout=30)
            c["state"] = "CANCELLED"
