"""site_batch — Google Batch adapter.

Ported from an earlier standalone wrapper (gbatch.sh) whose waves kept running
alongside the queue for a while.  The wire contract is deliberately identical,
so a lanes.cmds file written for that wrapper runs here unchanged:
    $IN  = /mnt/disks/share/inputs[/<--inputs subpath>]     (GCS fuse mount, ro use)
    $OUT = /mnt/disks/share/jobs/<job>/out/task_<i>
    e2-standard-8 by default, NO external IP (8-address quota), spot by default,
    logs to Cloud Logging, outputs pulled with a SINGLE-STREAM
    `gcloud storage rsync` (gsutil rsync as fallback).

Three defects of the gbatch runner are fixed here (docs/ARCHITECTURE.md lists them):
  1. `set -e` meant a failing task never wrote $OUT/_rc.txt; the runner now
     always writes it and exits with the real rc so Batch sees FAILED.
  2. `--inputs` was parsed and then ignored; it now moves $IN.
  3. task index -> line was physical-line based while the task count was
     non-blank-line based; `q` strips BLANK lines at submit time (comment lines
     are NOT stripped — a `#` line becomes a task that trivially succeeds).
Added: a task whose $OUT/_rc.txt is already 0 exits 0 immediately, which makes an
on-demand resubmit after a correlated spot preemption a true resume.
"""
import hashlib
import json
import math
import os
import time

import qcommon as Q

TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "DELETION_IN_PROGRESS",
            "CANCELLATION_IN_PROGRESS", "SUBMIT_FAILED", "SUBMIT_LOST")
# chunk states of OUR OWN making: the API never returns these
NEVER_RAN = ("SUBMIT_FAILED", "SUBMIT_LOST")

RUNNER = r'''set +e
JOBDIR=/mnt/disks/share/jobs/%(job)s
IDX=$(sed -n "$((BATCH_TASK_INDEX+1))p" "$JOBDIR/chunks/%(chunk)s.idx")
export OUT="$JOBDIR/out/task_$IDX"
mkdir -p "$OUT" || exit 97
if [ -f "$OUT/_rc.txt" ] && [ "$(cat "$OUT/_rc.txt")" = "0" ]; then
  echo "SKIP task $IDX: already rc=0"; exit 0
fi
line=$(sed -n "$((IDX+1))p" "$JOBDIR/lanes.cmds")
echo "TASK $IDX: $line" | tee "$OUT/_cmd.txt"
( cd /mnt/disks/share && bash -c "$line" ) > "$OUT/_stdout.txt" 2> "$OUT/_stderr.txt"
rc=$?
printf '%%s\n' "$rc" > "$OUT/_rc.txt.tmp" && mv "$OUT/_rc.txt.tmp" "$OUT/_rc.txt"
exit $rc
'''

_MACHINE_CPUS = {"micro": 2, "small": 2, "medium": 2}
# MiB of RAM per vCPU by family — Batch packs tasks by BOTH cpu and memory, so a
# memory-hungry task gets fewer per VM than the cpu arithmetic alone suggests.
_MEM_PER_CPU_MIB = {"highmem": 8192, "highcpu": 1024, "standard": 4096}


def machine_cpus(machine):
    tail = machine.rsplit("-", 1)[-1]
    if tail.isdigit():
        return int(tail)
    return _MACHINE_CPUS.get(tail, 8)


def machine_mem_mib(machine):
    parts = machine.split("-")
    fam = parts[1] if len(parts) > 2 else "standard"
    return machine_cpus(machine) * _MEM_PER_CPU_MIB.get(fam, 4096)


def tasks_per_vm(machine, cpus_per_task, mem_mib_per_task):
    """How many tasks Batch will actually pack onto one VM — the MIN of what the
    vCPUs allow and what the RAM allows.  Counting cpus only under-estimates the
    VM count (and therefore the quota footprint) for memory-heavy tasks."""
    m = machine_cpus(machine)
    by_cpu = int(m // max(1, math.ceil(cpus_per_task)))
    mem = max(1, int(mem_mib_per_task or 1))
    by_mem = int(machine_mem_mib(machine) // mem)
    return max(1, min(by_cpu if by_cpu else 1, by_mem if by_mem else 1))


class BatchSite(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.cap = Q.cint(cfg, "Q_BATCH_CPU_CAP")
        self.machine = cfg["Q_BATCH_MACHINE"]
        self.project = cfg["Q_PROJECT"]
        self.region = cfg["Q_REGION"]
        self.bucket = cfg["Q_BUCKET"]
        self.image = cfg["Q_IMAGE"]
        self.gcloud = cfg["Q_GCLOUD"]
        self.gsutil = cfg["Q_GSUTIL"]
        self.timeout = Q.cint(cfg, "Q_GCLOUD_TIMEOUT")
        # The pull is the ONE call that legitimately runs for many minutes (many
        # small task_i/ files); every other gcloud call is a control-plane call
        # that should fail fast, so they do not share a timeout.
        try:
            self.pull_timeout = Q.cint(cfg, "Q_PULL_TIMEOUT")
        except (KeyError, ValueError):
            self.pull_timeout = max(900, self.timeout)
        self._gcs_rsync = None    # None = not probed; (ok, has_checksums_only)
        self.maxretry = Q.cint(cfg, "Q_BATCH_MAXRETRY")
        self.dry = os.environ.get("Q_DRY_RUN") == "1"
        self.jobs = {}            # name -> record, refreshed once per cycle
        self.list_ok = False

    # ------------------------------------------------------------ the ONE call
    def refresh(self):
        """Exactly one `gcloud batch jobs list` per scheduler cycle.  Everything
        the scheduler needs (per-job state, per-task counts, cpu footprint) comes
        out of this one response; nothing else calls the Batch API while polling."""
        fake = os.environ.get("Q_FAKE_LIST")
        if not fake and not self.project:
            # Batch is not configured (no Q_PROJECT): stay unavailable without
            # spawning a gcloud call that can only fail.
            self.jobs, self.list_ok = {}, False
            return False
        if fake:
            Q.log("GCALL list job=- argv=(fake %s)" % fake)
            data = Q.read_json(fake, [])
        else:
            fmt = ("json(name,status.state,status.taskGroups,taskGroups.parallelism,"
                   "taskGroups.taskCount,taskGroups.taskSpec.computeResource,"
                   "allocationPolicy.instances,createTime)")
            r = Q.run_cmd([self.gcloud, "batch", "jobs", "list",
                           "--project=" + self.project, "--location=" + self.region,
                           "--format=" + fmt], self.timeout, kind="list")
            if r.rc != 0:
                Q.log("BATCH list FAILED rc=%d: %s" % (r.rc, r.err.strip()[:300]))
                self.list_ok = False
                return False
            try:
                data = json.loads(r.out)
            except ValueError:
                Q.log("BATCH list: unparseable JSON")
                self.list_ok = False
                return False
        jobs = {}
        for j in data or []:
            nm = j.get("name", "").rsplit("/", 1)[-1]
            tg = (j.get("taskGroups") or [{}])[0]
            spec = tg.get("taskSpec", {})
            cr = spec.get("computeResource", {}) or {}
            pol = ((j.get("allocationPolicy", {}).get("instances") or [{}])[0]
                   .get("policy", {}) or {})
            counts = ((j.get("status", {}).get("taskGroups", {}) or {})
                      .get("group0", {}) or {}).get("counts", {}) or {}
            jobs[nm] = {
                "name": nm,
                "state": j.get("status", {}).get("state", "UNKNOWN"),
                "counts": {k: int(v) for k, v in counts.items()},
                "taskcount": int(tg.get("taskCount", 0) or 0),
                "parallelism": int(tg.get("parallelism", tg.get("taskCount", 0)) or 0),
                "cpus": float(cr.get("cpuMilli", 1000) or 1000) / 1000.0,
                "mem_mib": int(cr.get("memoryMib", 0) or 0),
                "machine": pol.get("machineType", self.machine),
                "create": j.get("createTime", ""),
            }
        self.jobs = jobs
        self.list_ok = True
        return True

    @staticmethod
    def vcpu_cost(ntasks_parallel, cpus_per_task, machine, mem_mib=0):
        """Quota is charged on VM vCPUs, not on requested cpuMilli: Batch packs
        min(floor(M/cpus), floor(RAM/mem)) tasks per VM and each VM costs M vCPUs.
        Always round UP — the 3 Sep failure was arrays whose *packed* footprint
        blew CPUS_ALL_REGIONS and then cycled QUEUED<->SCHEDULED forever."""
        m = machine_cpus(machine)
        per_vm = tasks_per_vm(machine, cpus_per_task, mem_mib)
        return int(math.ceil(ntasks_parallel / float(per_vm)) * m), per_vm

    def used_cpus(self):
        """vCPUs in flight across EVERY job in the project — including waves other
        agents submitted with the legacy gbatch wrapper.  The daemon yields to
        them rather than competing with them."""
        tot = 0
        for r in self.jobs.values():
            if r["state"] in TERMINAL:
                continue
            c, _ = self.vcpu_cost(max(1, r["parallelism"]), r["cpus"], r["machine"],
                                  r.get("mem_mib", 0))
            tot += c
        return tot

    def free_cpus(self):
        return self.cap - self.used_cpus()

    # ------------------------------------------------------------ chunking
    def plan_chunk(self, spec, remaining):
        """Largest chunk of `remaining` task indices that fits in the free cap."""
        machine = spec.get("machine") or self.machine
        m = machine_cpus(machine)
        free = self.free_cpus()
        if free < m:
            return None
        per_vm = tasks_per_vm(machine, spec["cpus"], spec.get("mem_mib", 0))
        vms = int(free // m)
        n = min(len(remaining), per_vm * vms)
        if n <= 0:
            return None
        idx = list(remaining)[:n]
        cost, _ = self.vcpu_cost(n, spec["cpus"], machine, spec.get("mem_mib", 0))
        return {"indices": idx, "parallelism": n, "vcpu": cost, "machine": machine}

    # ------------------------------------------------------------ job JSON
    def job_json(self, name, chunk_tag, ntasks, parallelism, spec, spot, machine):
        ext_ip = str(self.cfg.get("Q_BATCH_EXTERNAL_IP", "0")) not in ("0", "", "false", "False")
        runner = RUNNER % {"job": name, "chunk": chunk_tag}
        indir = "/mnt/disks/share/inputs"
        if spec.get("inputs"):
            indir = indir + "/" + spec["inputs"].strip("/")
        return {
            "taskGroups": [{
                "taskCount": int(ntasks), "parallelism": int(parallelism),
                "taskSpec": {
                    "runnables": [{"container": {
                        "imageUri": spec.get("image") or self.image,
                        "entrypoint": "/bin/bash", "commands": ["-c", runner],
                        "volumes": ["/mnt/disks/share:/mnt/disks/share:rw"]}}],
                    "computeResource": {"cpuMilli": int(spec["cpus"] * 1000),
                                        "memoryMib": int(spec["mem_mib"])},
                    "maxRetryCount": self.maxretry,
                    "maxRunDuration": "%ds" % int(spec["timeout_sec"]),
                    "volumes": [{"gcs": {"remotePath": self.bucket},
                                 "mountPath": "/mnt/disks/share"}],
                    "environment": {"variables": {
                        "IN": indir,
                        "OUT_BASE": "/mnt/disks/share/jobs/%s/out" % name}},
                }}],
            "allocationPolicy": {
                "instances": [{"policy": {
                    "provisioningModel": "SPOT" if spot else "STANDARD",
                    "machineType": machine}}],
                "network": {"networkInterfaces": [{
                    "network": "projects/%s/global/networks/default" % self.project,
                    "subnetwork": "projects/%s/regions/%s/subnetworks/default"
                                  % (self.project, self.region),
                    # An external IP is normally OFF (the 8-address quota), but a
                    # network-bound wave NEEDS one per task: each Batch task lands on
                    # its own VM, so external IPs are how we get distinct source IPs
                    # for per-IP-throttled APIs. Q_BATCH_EXTERNAL_IP=1 turns them on.
                    "noExternalIpAddress": not ext_ip}]}},
            "logsPolicy": {"destination": "CLOUD_LOGGING"},
        }

    # ------------------------------------------------------------ submit
    def submit_chunk(self, st, indices, spot, kind, first):
        name = st["name"]
        spec = st["spec"]
        k = len(st.get("chunks", []))
        ep = int(spec.get("epoch", 0) or 0)
        # a RESUBMITTED job (same name, epoch>0) must not reuse c0: the previous
        # epoch's Batch job of that name still exists in the project and the
        # submit would come back ALREADY_EXISTS.
        tag = "c%d" % k if not ep else "e%dc%d" % (ep, k)
        chunk_job = "%s-%s" % (name, tag)
        machine = spec.get("machine") or self.machine
        stage = os.path.join(Q.P["stage"], name)
        os.makedirs(os.path.join(stage, "chunks"), exist_ok=True)
        idxf = os.path.join(stage, "chunks", tag + ".idx")
        with open(idxf, "w") as fh:
            fh.write("".join("%d\n" % i for i in indices))
        gsdir = "gs://%s/jobs/%s" % (self.bucket, name)
        calls = []
        if first:
            calls.append(([self.gsutil, "-q", "cp", st["cmds_path"],
                           gsdir + "/lanes.cmds"], "cp-lanes"))
        calls.append(([self.gsutil, "-q", "cp", idxf,
                       gsdir + "/chunks/" + tag + ".idx"], "cp-idx"))
        for argv, knd in calls:
            if self.dry:
                Q.log("GCALL %s job=%s argv=(dry) %s" % (knd, name, " ".join(argv)))
                continue
            r = Q.run_cmd(argv, self.timeout, kind=knd, name=name)
            if r.rc != 0:
                Q.event(st, "stage upload FAILED rc=%d: %s" % (r.rc, r.err.strip()[:200]))
                return None
        cfg = self.job_json(name, tag, len(indices), len(indices), spec, spot, machine)
        cfgf = os.path.join(stage, "chunks", tag + ".json")
        Q.write_json_atomic(cfgf, cfg)
        argv = [self.gcloud, "batch", "jobs", "submit", chunk_job,
                "--project=" + self.project, "--location=" + self.region,
                "--config=" + cfgf]
        cost, per_vm = self.vcpu_cost(len(indices), spec["cpus"], machine,
                                      spec.get("mem_mib", 0))
        # ---- PERSIST THE INTENT BEFORE THE API CALL ----
        # A `gcloud batch jobs submit` that succeeds while the daemon is killed
        # (OOM, memory reducer, kill -9) would otherwise leave a REAL Batch job
        # burning quota with no record of it anywhere: the restarted daemon would
        # resubmit the same chunk name, get ALREADY_EXISTS, and loop forever.
        rec = {"job": chunk_job, "tag": tag, "kind": kind, "spot": bool(spot),
               "indices": list(indices), "state": "SUBMITTING", "counts": {},
               "vcpu": cost, "machine": machine, "submitted": time.time(),
               "probe": 0}
        st.setdefault("chunks", []).append(rec)
        self._ledger(name, chunk_job, len(indices), kind, spot, "intent")
        Q.event(st, "submitting %s (%s, %d tasks, %d vCPU, %d task/VM, spot=%d)"
                % (chunk_job, kind, len(indices), cost, per_vm, int(bool(spot))))
        Q.save_state(st)
        if self.dry:
            Q.log("GCALL submit job=%s argv=(dry) %s [provisioningModel=%s]"
                  % (name, " ".join(argv),
                     cfg["allocationPolicy"]["instances"][0]["policy"]["provisioningModel"]))
        else:
            r = Q.run_cmd(argv, self.timeout, kind="submit", name=name)
            if r.rc != 0:
                err = (r.err or "") + (r.out or "")
                if "ALREADY_EXISTS" in err or "already exists" in err.lower():
                    # our own earlier attempt got through before we died: adopt it
                    rec["state"] = "QUEUED"
                    rec["adopted"] = True
                    Q.event(st, "submit %s reported ALREADY_EXISTS — ADOPTING the "
                                "existing Batch job instead of submitting again"
                            % chunk_job)
                    self._charge(rec, spec)
                    Q.save_state(st)
                    return rec
                rec["state"] = "SUBMIT_FAILED"
                rec["error"] = err.strip()[:300]
                st["submit_fails"] = st.get("submit_fails", 0) + 1
                Q.event(st, "submit %s FAILED rc=%d (%d/%s): %s"
                        % (chunk_job, r.rc, st["submit_fails"],
                           self.cfg.get("Q_SUBMIT_MAX_FAILS"), err.strip()[:300]))
                self._ledger(name, chunk_job, len(indices), kind, spot, "failed")
                Q.save_state(st)
                return None
        rec["state"] = "QUEUED"
        self._charge(rec, spec)
        Q.event(st, "submitted %s (%s, %d tasks, %d vCPU, %d task/VM, spot=%d)"
                % (chunk_job, kind, len(indices), cost, per_vm, int(bool(spot))))
        self._ledger(name, chunk_job, len(indices), kind, spot, "submitted")
        Q.save_state(st)
        return rec

    def _ledger(self, name, chunk_job, n, kind, spot, phase):
        with open(Q.P["ledger"], "a") as fh:
            fh.write("%s\t%s\t%s\t%d\t%s\tspot=%d\t%s\n"
                     % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), name, chunk_job,
                        n, kind, int(bool(spot)), phase))

    def _charge(self, rec, spec):
        """Book a just-submitted chunk against the cap IMMEDIATELY.

        `refresh()` runs once per cycle, so without this every job admitted in
        the same cycle plans against the same stale free-capacity snapshot: two
        agents' jobs each saw 32 free vCPU and submitted 32, for 64 against a cap
        of 32 — the exact 3 Sep failure the queue exists to prevent."""
        self.jobs[rec["job"]] = {
            "name": rec["job"], "state": "QUEUED", "counts": {},
            "taskcount": len(rec["indices"]), "parallelism": len(rec["indices"]),
            "cpus": float(spec["cpus"]), "mem_mib": int(spec.get("mem_mib", 0)),
            "machine": rec["machine"], "create": "", "synthetic": True,
        }

    # ------------------------------------------------------------ track / pull
    def update_chunks(self, st):
        """Fold the ONE cached listing into our chunk records, and settle any
        chunk whose submit call was interrupted by a daemon death."""
        changed = False
        for c in st.get("chunks", []):
            r = self.jobs.get(c["job"])
            if not r or r.get("synthetic"):
                if c["state"] == "SUBMITTING":
                    c["probe"] = c.get("probe", 0) + 1
                    if c["probe"] >= 3:
                        c["state"] = "SUBMIT_LOST"
                        changed = True
                        Q.event(st, "chunk %s was mid-submit when the daemon died "
                                    "and never appeared in the Batch listing — "
                                    "treating it as NEVER SUBMITTED; its tasks are "
                                    "replanned under a new chunk name" % c["job"])
                continue
            if c["state"] == "SUBMITTING":
                Q.event(st, "chunk %s WAS submitted before the daemon died — "
                            "re-adopted from the Batch listing (state %s), not "
                            "resubmitted" % (c["job"], r["state"]))
                changed = True
            if r["state"] != c["state"] or r["counts"] != c["counts"]:
                changed = True
            c["state"], c["counts"] = r["state"], r["counts"]
        return changed

    def cancel_chunk(self, st, c, why):
        argv = [self.gcloud, "batch", "jobs", "delete", c["job"],
                "--project=" + self.project, "--location=" + self.region, "--quiet"]
        if self.dry:
            Q.log("GCALL cancel job=%s argv=(dry) %s" % (st["name"], " ".join(argv)))
        else:
            Q.run_cmd(argv, self.timeout, kind="cancel", name=st["name"])
        c["state"] = "CANCELLED"
        c["aborted"] = why
        Q.event(st, "chunk %s DELETED: %s" % (c["job"], why))

    def _gcloud_storage_rsync(self):
        """(available, supports --checksums-only) for `gcloud storage rsync`.

        Probed ONCE per daemon process.  `gcloud storage` is the successor to
        gsutil and measured ~14 MB/s against gsutil's ~1.5 MB/s single-stream on
        the same bucket, with no hangs; gsutil stays as the fallback so an older
        SDK keeps working unchanged.
        """
        if self._gcs_rsync is None:
            r = Q.run_cmd([self.gcloud, "storage", "rsync", "--help"],
                          min(120, max(60, self.timeout)), kind="probe")
            ok = (r.rc == 0 and "--recursive" in r.out)
            self._gcs_rsync = (ok, ok and "--checksums-only" in r.out)
            Q.log("BATCH pull transport: %s (checksums-only=%s)" %
                  ("gcloud storage rsync" if ok else "gsutil rsync",
                   self._gcs_rsync[1]))
        return self._gcs_rsync

    def pull_argv(self, src, dest, force=False):
        """Transfer command.  SOURCE and DEST stay the LAST two arguments:
        qcommon._matching_transfer_pid identifies a stranded transfer by exactly
        that pair."""
        ok, has_cksum = self._gcloud_storage_rsync()
        # Q_PULL_EXCLUDE_DIRS: `|`-separated directory names never pulled back
        # (large per-task inputs a task copies into $OUT, say).  Empty = none.
        exd = str(self.cfg.get("Q_PULL_EXCLUDE_DIRS", "") or "").strip("|")
        if ok:
            # --no-user-output-enabled is the -q equivalent (progress//info off,
            # warnings and errors still reach the pull log via --verbosity).
            argv = [self.gcloud, "storage", "rsync", "--recursive",
                    "--no-user-output-enabled"]
            if exd:
                argv.append("--exclude=(^|/)(%s)(/|$)" % exd)
            # gsutil rsync and gcloud storage rsync both compare size+mtime by
            # default; -c / --checksums-only force a hash comparison so a file
            # that was partially written at the last pull is refreshed.
            if force and has_cksum:
                argv.append("--checksums-only")
            return argv + [src, dest]
        return [self.gsutil, "-q", "rsync", "-r"] \
            + (["-x", r"(^|[/\\])(%s)([/\\]|$)" % exd] if exd else []) \
            + (["-c"] if force else []) \
            + [src, dest]

    def pull(self, st, force=False):
        name = st["name"]
        iv = Q.cint(self.cfg, "Q_PULL_INTERVAL_SEC")
        dest = os.path.join(Q.P["cache"], name)
        os.makedirs(dest, exist_ok=True)
        src = "gs://%s/jobs/%s/out" % (self.bucket, name)
        if self.dry:
            Q.log("GCALL pull job=%s argv=(dry) rsync %s %s" % (name, src, dest))
            # A dry pull "succeeds" (rc=0): advance_batch only settles a
            # terminal chunk after a SUCCESSFUL final pull, and without the rc
            # the dry-run harness could never reach the recovery path.
            st.setdefault("pull", {}).update(last=time.time(), rc=0)
            return "done"
        else:
            # SINGLE STREAM per job, at most once per interval.  The worker is
            # detached so a slow rsync cannot delay admission or local reaping.
            argv = self.pull_argv(src, dest, force)
            phase = Q.async_transfer(st, argv, self.pull_timeout, force=force,
                                     interval=iv, kind="batch-pull")
            if phase != "done":
                return phase
        p = st.setdefault("pull", {})
        nf, nb, wit, man = verify_tree(dest)
        p.update({"files": nf, "bytes": nb, "witness": wit, "manifest": man,
                  "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        st["pull"] = p
        return "done"

    def done_indices(self, st):
        """Ground truth for completion: an $OUT/task_i/_rc.txt of 0 in the pulled
        cache.  Batch task counts are progress, never proof."""
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
            argv = [self.gcloud, "batch", "jobs", "delete", c["job"],
                    "--project=" + self.project, "--location=" + self.region, "--quiet"]
            if self.dry:
                Q.log("GCALL cancel job=%s argv=(dry) %s" % (st["name"], " ".join(argv)))
            else:
                Q.run_cmd(argv, self.timeout, kind="cancel", name=st["name"])
            c["state"] = "CANCELLED"


def verify_tree(dest):
    """file count + total bytes from stat + a manifest sha + a witness sha256.

    Never `du -s` (it measures the filesystem, not the files).  The witness is
    the LARGEST file, not the alphabetically first: `task_0/_cmd.txt` is byte
    identical on every pull of every job, so hashing it proves nothing about the
    payload.  The manifest sha covers every (relpath, size) pair, so a missing or
    truncated file changes it."""
    files = []
    for dp, _dn, fn in os.walk(dest):
        for f in fn:
            files.append(os.path.join(dp, f))
    files.sort()
    nb = 0
    sizes = []
    man = hashlib.sha256()
    for f in files:
        try:
            sz = os.path.getsize(f)
        except OSError:
            sz = 0
        nb += sz
        sizes.append((sz, f))
        man.update(("%s\0%d\0" % (os.path.relpath(f, dest), sz)).encode())
    wit = None
    for _sz, f in sorted(sizes, reverse=True):
        try:
            h = hashlib.sha256()
            with open(f, "rb") as fh:
                for blk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(blk)
            wit = {"path": os.path.relpath(f, dest), "sha256": h.hexdigest()}
            break
        except (IOError, OSError):
            continue
    return len(files), nb, wit, man.hexdigest()
