"""qcommon — paths, config, atomic state IO, logging, process helpers.

Pure python3 stdlib.  Every path is derived from Q_ROOT so the whole tree can be
relocated for tests (Q_ROOT=/tmp/qtest bin/qd --once).  Q_ROOT defaults to the
checkout itself; all of its data directories are gitignored.
"""
import errno
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time

# ---------------------------------------------------------------- paths / config

def _default_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.abspath(os.environ.get("Q_ROOT") or _default_root())
# code always lives with qcommon.py; only DATA moves with Q_ROOT
CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

P = {
    "pending": os.path.join(ROOT, "spool", "pending"),
    "cmds":    os.path.join(ROOT, "spool", "cmds"),
    "tmp":     os.path.join(ROOT, "spool", "tmp"),
    "cancel":  os.path.join(ROOT, "spool", "cancel"),
    "accepted":os.path.join(ROOT, "spool", "accepted"),
    "state":   os.path.join(ROOT, "state"),
    "cache":   os.path.join(ROOT, "cache"),
    "localout":os.path.join(ROOT, "local_out"),
    "logs":    os.path.join(ROOT, "logs"),
    "stage":   os.path.join(ROOT, "stage"),
    "inputs":  os.path.join(ROOT, "inputs"),
    "qdlog":   os.path.join(ROOT, "qd.log"),
    "board":   os.path.join(ROOT, "BOARD.txt"),
    "lock":    os.path.join(ROOT, "qd.lock"),
    "ledger":  os.path.join(ROOT, "ledger.tsv"),
}

def ensure_tree():
    for k in ("pending", "cmds", "tmp", "cancel", "accepted", "state", "cache",
              "localout", "logs", "stage", "inputs"):
        os.makedirs(P[k], exist_ok=True)

_DEF = {
    "Q_LOCAL_RESERVE_THREADS": "2", "Q_LOCAL_RESERVE_MB": "1024",
    "Q_LOCAL_RESERVE_PCT": "15", "Q_LOCAL_GPU_SLOTS": "1",
    "Q_MEM_GRACE_POLLS": "2", "Q_STALL_ABORT_SEC": "7200",
    "Q_SUBMIT_MAX_FAILS": "3",
    "Q_LOCAL_MAX_TASKS": "8", "Q_LOCAL_NICE": "15",
    "Q_BATCH_CPU_CAP": "32", "Q_BATCH_MACHINE": "e2-standard-8",
    "Q_BATCH_MAXRETRY": "1",
    "Q_PROJECT": "", "Q_REGION": "us-central1", "Q_BUCKET": "", "Q_IMAGE": "",
    "Q_GCLOUD": "gcloud", "Q_GSUTIL": "gsutil",
    "Q_CYCLE_SEC": "60", "Q_RSS_POLL_SEC": "5", "Q_PULL_INTERVAL_SEC": "900",
    "Q_MAX_MEM_KILLS": "2", "Q_MAX_CHUNK_RETRIES": "2", "Q_GCLOUD_TIMEOUT": "300",
    "Q_PULL_TIMEOUT": "900",
    "Q_STALL_WARN_SEC": "1800",
    "Q_VAST_ENABLE": "1", "Q_VAST_INSTANCES": "fleet_state/instances.env",
    "Q_VAST_SSH_TIMEOUT": "60", "Q_VAST_PULL_TIMEOUT": "14400",
    "Q_VAST_CORE_CAP": "61.4", "Q_VAST_REMOTE_ROOT": "/workspace/qvast",
    "Q_VAST_USER": "root",
    "Q_VULTR_ENABLE": "0", "Q_EITHER_ORDER": "local,vast,batch",
}


def root_path(p):
    """A config path: `~` expanded, and RELATIVE paths resolved against Q_ROOT,
    so one config.env works for every checkout and every test sandbox."""
    p = os.path.expanduser(str(p))
    return p if os.path.isabs(p) else os.path.join(ROOT, p)

def load_config():
    """config.env (KEY=VALUE, # comments) then real env vars win."""
    cfg = dict(_DEF)
    path = os.environ.get("Q_CONFIG") or os.path.join(ROOT, "config.env")
    if not os.path.exists(path):
        path = os.path.join(CODE, "config.env")
    if os.path.exists(path):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.split("#")[0].strip().strip('"').strip("'")
                cfg[k.strip()] = v
    for k in list(cfg):
        if k in os.environ:
            cfg[k] = os.environ[k]
    return cfg

def cint(cfg, key):
    return int(float(cfg[key]))

# ---------------------------------------------------------------- logging

_LOGFH = None
LOG_MAX_BYTES = 20 * 1024 * 1024
JOBLOG_MAX_BYTES = 5 * 1024 * 1024
LOG_GENERATIONS = 3


def rotate(tgt, generations=LOG_GENERATIONS):
    """qd.log -> qd.log.1 -> qd.log.2 -> qd.log.3.  Keeping only ONE generation
    used to destroy the older half of the audit trail exactly during a long
    incident (the incident is what makes the log roll)."""
    for g in range(generations - 1, 0, -1):
        try:
            os.replace("%s.%d" % (tgt, g), "%s.%d" % (tgt, g + 1))
        except OSError:
            pass
    try:
        os.replace(tgt, tgt + ".1")
    except OSError:
        pass


def log(msg, path=None):
    global _LOGFH
    line = "%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), msg)
    tgt = path or P["qdlog"]
    if path is None:
        if _LOGFH is None:
            _LOGFH = open(tgt, "a", buffering=1)
        elif _LOGFH.tell() > LOG_MAX_BYTES:
            _LOGFH.close()
            rotate(tgt)
            _LOGFH = open(tgt, "a", buffering=1)
        _LOGFH.write(line)
    else:
        try:
            if os.path.getsize(tgt) > JOBLOG_MAX_BYTES:
                rotate(tgt, 2)
        except OSError:
            pass
        with open(tgt, "a", buffering=1) as fh:
            fh.write(line)
    if os.environ.get("Q_ECHO") == "1":
        sys.stderr.write(line)

def jlog(name, msg):
    log("[%s] %s" % (name, msg))
    log(msg, path=os.path.join(P["logs"], name + ".log"))

# ---------------------------------------------------------------- json IO

def read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (IOError, OSError, ValueError):
        return default

def write_json_atomic(path, obj):
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

def state_path(name):
    return os.path.join(P["state"], name + ".json")

def load_state(name):
    return read_json(state_path(name))

def save_state(st):
    st["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write_json_atomic(state_path(st["name"]), st)

_BAD_STATE_WARNED = set()


def state_files():
    if not os.path.isdir(P["state"]):
        return []
    return [fn for fn in sorted(os.listdir(P["state"]))
            if fn.endswith(".json") and not fn.endswith(".tmp")]


def all_states():
    """Every readable state file.  An UNREADABLE one is loud: a state file that
    silently disappears from this list takes its running tasks' threads and its
    in-flight Batch chunks out of the scheduler's world with it."""
    out = []
    for fn in state_files():
        pth = os.path.join(P["state"], fn)
        st = read_json(pth)
        if st and isinstance(st, dict) and "name" in st and "tasks" in st:
            out.append(st)
        else:
            if fn not in _BAD_STATE_WARNED:
                _BAD_STATE_WARNED.add(fn)
                log("STATE UNREADABLE %s — this job is INVISIBLE to the scheduler "
                    "(its tasks/chunks are not tracked). Move it aside or restore it."
                    % pth)
    return out


def bad_state_names():
    bad = []
    for fn in state_files():
        st = read_json(os.path.join(P["state"], fn))
        if not (st and isinstance(st, dict) and "name" in st and "tasks" in st):
            bad.append(fn[:-5])
    return bad

def event(st, msg):
    ev = st.setdefault("events", [])
    ev.append("%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), msg))
    del ev[:-60]
    jlog(st["name"], msg)

# ---------------------------------------------------------------- validation

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,44}$")

def valid_name(n):
    return bool(NAME_RE.match(n))

def parse_mem_mib(s):
    s = str(s).strip().upper()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([KMGT]?)(I?B?)$", s)
    if not m:
        raise ValueError("bad memory spec %r" % s)
    v = float(m.group(1))
    mult = {"": 1, "K": 1.0 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[m.group(2)]
    return int(round(v * mult))

def parse_dur_sec(s):
    s = str(s).strip().lower()
    m = re.match(r"^([0-9]*\.?[0-9]+)([hms]?)$", s)
    if not m:
        raise ValueError("bad duration %r" % s)
    return int(float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)])

# ---------------------------------------------------------------- host capacity

def mem_available_mib():
    with open("/proc/meminfo") as fh:
        for line in fh:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0

def mem_total_mib():
    with open("/proc/meminfo") as fh:
        for line in fh:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    return 0


def local_reserve_mb(cfg):
    """Headroom kept for the harness: a FLOOR in MiB and a FRACTION of the box,
    whichever is larger.  A fixed 2048 MiB floor on a 7 GB box left ~300 MiB
    schedulable, i.e. the default `--mem 2G` job could never be admitted."""
    floor = cint(cfg, "Q_LOCAL_RESERVE_MB")
    try:
        pct = cint(cfg, "Q_LOCAL_RESERVE_PCT")
    except (KeyError, ValueError):
        pct = 0
    return max(floor, mem_total_mib() * pct // 100)


def nproc():
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1

def proc_table():
    """{pid: (pgid, rss_bytes)} for every readable LIVE process.

    ZOMBIES ARE EXCLUDED.  A finished task that the daemon has not reaped still
    has a /proc entry, and treating that as "alive" once deadlocked the whole
    queue (a finished job never released its threads).  A zombie holds no memory
    and can run nothing, so for scheduling purposes it is dead."""
    out = {}
    page = os.sysconf("SC_PAGE_SIZE")
    for e in os.listdir("/proc"):
        if not e.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % e) as fh:
                data = fh.read()
            r = data.rindex(")")
            fields = data[r + 2:].split()
            # after "pid (comm) " fields[0]=state fields[1]=ppid fields[2]=pgrp ...
            if fields[0] == "Z":
                continue
            pgrp = int(fields[2])
            rss = int(fields[21]) * page      # field 24 overall = index 21 here
            out[int(e)] = (pgrp, rss)
        except (IOError, OSError, ValueError, IndexError):
            continue
    return out

def pgid_rss_mib(pgid, table=None):
    t = table if table is not None else proc_table()
    return sum(r for (p, r) in t.values() if p == pgid) // (1024 * 1024)


def pgid_pids(pgid, table=None):
    t = table if table is not None else proc_table()
    return [pid for pid, (p, _r) in t.items() if p == pgid]


def pgid_pss_mib(pgid, table=None):
    """Proportional set size for a whole process group, from smaps_rollup.

    Summing per-process RSS DOUBLE COUNTS every page a forked child shares with
    its parent: a 250 MiB python that forks 3 workers measures 1026 MiB of RSS
    and only ~260 MiB of PSS.  Reading smaps_rollup for every process on the box
    costs ~0.4 s, so this is only ever called for the ONE group that already
    tripped the cheap RSS check.  Returns None if the kernel does not expose it."""
    tot = 0
    seen = False
    for pid in pgid_pids(pgid, table):
        try:
            with open("/proc/%d/smaps_rollup" % pid) as fh:
                for line in fh:
                    if line.startswith("Pss:"):
                        tot += int(line.split()[1])
                        seen = True
                        break
        except (IOError, OSError, ValueError):
            continue
    return (tot // 1024) if seen else None

def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM

# ---------------------------------------------------------------- subprocess

class CallResult(object):
    def __init__(self, rc, out, err):
        self.rc, self.out, self.err = rc, out, err

def run_cmd(argv, timeout, kind="misc", name="-"):
    """Every external process the daemon spawns goes through here, so qd.log has
    an auditable count of gcloud/gsutil calls per cycle."""
    log("GCALL %s job=%s argv=%s" % (kind, name, " ".join(shlex.quote(a) for a in argv)))
    t0 = time.time()
    try:
        pr = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout)
        rc, out, err = pr.returncode, pr.stdout.decode("utf-8", "replace"), \
            pr.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        rc, out, err = 124, "", "timeout after %ss" % timeout
    except OSError as e:
        # A missing CLI (no gcloud on a laptop, say) is a failed call, not a
        # daemon crash: the site simply reports itself unavailable this cycle.
        rc, out, err = 127, "", "%s: %s" % (argv[0], e)
    log("GDONE %s job=%s rc=%d %.1fs" % (kind, name, rc, time.time() - t0))
    return CallResult(rc, out, err)


# ---------------------------------------------------------------- result transfers

def _transfer_status(name):
    return os.path.join(P["tmp"], "pull-%s.json" % name)


def _pid_alive(pid):
    """Live and NOT a zombie.

    os.kill(pid, 0) succeeds on a zombie, and a zombie transfer that nobody has
    reaped yet would otherwise hold a job's pull "active" forever — the same
    stall shape as the orphaned gsutil, with no process left to blame.  This
    matches proc_table()'s rule: a zombie holds no memory and copies nothing.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        with open("/proc/%d/stat" % pid) as fh:
            data = fh.read()
        return data[data.rindex(")") + 2:].split()[0] != "Z"
    except (IOError, OSError, ValueError, IndexError):
        pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# Every transfer program the queue starts: the GCS tools, plain rsync/scp for
# the vast legs, and the queue's own transfer scripts (vultr_transfer.py,
# vast_relay_pull.sh).  A process that matches none of these is NOT killed.
TRANSFER_TOOLS = ("gsutil", "gcloud", "rsync", "scp", "storage",
                  "transfer", "relay")
# A transfer is "hung" once it has outlived 2x its own timeout, never sooner
# than this floor (tests lower it; nothing else should).
HUNG_FLOOR_SEC = 60.0


def _proc_cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as fh:
            return fh.read().decode("utf-8", "replace")
    except (IOError, OSError, TypeError, ValueError):
        return ""


def _proc_start_epoch(pid):
    """Wall-clock start time of a pid (seconds since epoch), or None.

    Derived from /proc/<pid>/stat field 22 plus /proc/uptime, so it is immune to
    the daemon's own uptime and to PID reuse across a restart.
    """
    try:
        with open("/proc/%d/stat" % int(pid)) as fh:
            data = fh.read()
        fields = data[data.rindex(")") + 2:].split()
        ticks = int(fields[19])
        hz = os.sysconf("SC_CLK_TCK") or 100
        with open("/proc/uptime") as fh:
            up = float(fh.read().split()[0])
        return time.time() - up + (float(ticks) / hz)
    except (IOError, OSError, TypeError, ValueError, IndexError, AttributeError):
        return None


def proc_age_sec(pid):
    st = _proc_start_epoch(pid)
    return None if st is None else max(0.0, time.time() - st)


def _is_transfer_proc(pid, argv):
    """True only for a live process that is THIS job's transfer tool.

    Two independent conditions, because this function gates a SIGKILL: the
    cmdline must name a transfer tool, and it must contain this transfer's own
    destination path (argv[-1]), which is the per-job cache directory.
    """
    raw = _proc_cmdline(pid)
    if not raw:
        return False
    if not any(t in raw for t in TRANSFER_TOOLS):
        return False
    dest = str(argv[-1]) if argv else ""
    return bool(dest) and dest in raw


def _kill_transfer_proc(pid, argv, why="hung"):
    """SIGKILL a stranded transfer process and its group.  Returns True if the
    argv check passed and the signal was sent."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if not _pid_alive(pid) or not _is_transfer_proc(pid, argv):
        return False
    sent = False
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None
    if pgid and pgid > 1 and pgid != os.getpgrp():
        try:
            os.killpg(pgid, signal.SIGKILL)
            sent = True
        except OSError:
            pass
    try:
        os.kill(pid, signal.SIGKILL)
        sent = True
    except OSError:
        pass
    return sent


def _hung_transfer(st, pid, argv, timeout, kind, source):
    """Kill a transfer process that has outlived 2x its own timeout.

    A worker that times out now kills its whole process group (pull_worker.kill_group),
    but an orphan from an OLDER daemon -- or one whose worker was itself killed --
    still shows up here as a live process matching this job's cache destination.
    Adopting it forever is what stalled four Batch waves on 2026-09-15: the job's
    pull stayed "active", so site_batch never backfilled another chunk.
    """
    age = proc_age_sec(pid)
    limit = max(HUNG_FLOOR_SEC, 2.0 * float(timeout))
    if age is None or age <= limit:
        return False
    if not _kill_transfer_proc(pid, argv):
        # Could not confirm this process is THIS job's transfer, so it keeps its
        # adoption: running two copies into one cache tree is worse than waiting.
        log("%s job=%s hung pull NOT killed (unrecognised pid=%s age=%.0fs): %s"
            % (kind, st.get("name", "-"), pid, age, _proc_cmdline(pid)[:200]))
        return False
    event(st, "%s hung pull killed pid=%s age=%.0fs limit=%.0fs source=%s"
          % (kind, pid, age, limit, source))
    return True


def clear_transfer_marker(name):
    try:
        os.unlink(_transfer_status(name))
    except OSError:
        pass


def _matching_transfer_pid(argv):
    """Return a live process for this exact queue source/destination pair.

    This is only used to adopt a transfer stranded by a killed daemon/worker;
    matching the queue cache destination keeps unrelated archive rsyncs out of
    scope.  It never signals the discovered process.
    """
    if len(argv) < 2:
        return None
    needles = [str(argv[-2]), str(argv[-1])]
    try:
        pids = os.listdir("/proc")
    except OSError:
        return None
    for p in pids:
        if not p.isdigit() or int(p) == os.getpid():
            continue
        try:
            raw = open("/proc/%s/cmdline" % p, "rb").read().decode("utf-8", "replace")
        except IOError:
            continue
        if all(n in raw for n in needles):
            return int(p)
    return None


def async_transfer(st, argv, timeout, force=False, interval=0, kind="pull"):
    """Poll or start a result transfer without holding qd's event loop.

    The detached worker records only a cache-side marker in spool/tmp; this
    function, called by qd, is the only writer of ``st['pull']``.  A preserved
    active token lets a restarted qd adopt an already-running transfer rather
    than starting another rsync/gsutil process.
    """
    now = time.time()
    p = st.setdefault("pull", {"last": 0})
    active = p.get("active")
    status_path = _transfer_status(st["name"])
    marker = read_json(status_path, {}) or {}
    # qd can be killed after spawning a worker but before its end-of-cycle state
    # save.  The marker is enough to adopt that transfer on restart.
    if not active and marker.get("state") == "DONE":
        p["last"] = marker.get("finished", now)
        p["rc"] = marker.get("rc")
        p["at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        # This completion has now been incorporated into the sole queue state.
        # Leaving the marker in place makes every later cycle return here,
        # preventing a failed transfer from reaching the normal retry interval.
        # The worker is terminal, so removing its cache-side handoff is safe.
        try:
            os.unlink(status_path)
        except OSError:
            pass
        return "done"
    if not active and marker.get("state") == "RUNNING":
        worker_pid = marker.get("pid")
        child_pid = marker.get("child_pid")
        if not _pid_alive(worker_pid) and _pid_alive(child_pid) and \
                _hung_transfer(st, child_pid, argv, timeout, kind, "marker"):
            # Orphan: its worker is gone and it has outlived 2x the timeout, so
            # it will never finish.  Drop the marker and pull again this cycle.
            clear_transfer_marker(st["name"])
            marker = {}
        elif _pid_alive(worker_pid) or _pid_alive(child_pid):
            p["active"] = {"token": marker.get("token"),
                           "pid": worker_pid if _pid_alive(worker_pid) else child_pid,
                           "started": marker.get("started", now), "kind": kind,
                           "external": not _pid_alive(worker_pid)}
            return "active"
    if active:
        if marker.get("token") == active.get("token") and marker.get("state") == "DONE":
            p.pop("active", None)
            p["last"] = marker.get("finished", now)
            p["rc"] = marker.get("rc")
            p["at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            if marker.get("rc") != 0:
                event(st, "%s worker rc=%s: %s" %
                      (kind, marker.get("rc"), marker.get("err", "")[:200]))
            log("GDONE %s job=%s rc=%s %.1fs" %
                (kind, st["name"], marker.get("rc"),
                 now - active.get("started", now)))
            return "done"
        hung = False
        if _pid_alive(active.get("pid")):
            # An adopted (external) transfer has no worker to time it out, so
            # this is the only place that can ever retire one.
            if active.get("external") and _hung_transfer(
                    st, active.get("pid"), argv, timeout, kind, "adopted"):
                hung = True
            else:
                return "active"
        if not hung and marker.get("token") == active.get("token") and \
                _pid_alive(marker.get("child_pid")):
            # The worker was killed but its exact child is still copying.  Keep
            # waiting unless the child has outlived 2x the timeout, in which case
            # nothing will ever retire it and this job's pull stalls forever.
            if _hung_transfer(st, marker.get("child_pid"), argv, timeout, kind,
                              "orphan-child"):
                hung = True
            else:
                active["pid"] = marker.get("child_pid")
                active["external"] = True
                return "active"
        p.pop("active", None)
        if not _pid_alive(marker.get("pid")):
            # Its worker is gone, so this marker can never be updated again.
            clear_transfer_marker(st["name"])
            marker = {}
        if hung:
            # Killed just now: fall through and start a clean pull below.
            pass
        elif active.get("external"):
            # An old synchronous pull had no completion marker.  Wait one
            # cycle after it exits, then retry normally if a final pull is still
            # required; do not mistake a partial cache for completion.
            event(st, "%s adopted transfer ended without a worker marker; retrying if needed" % kind)
            return "idle"
        else:
            # A worker can die before writing DONE (for example, a host-level kill).
            # Clear only the queue's in-memory record; never touch outputs or state
            # produced by the previous transfer.
            event(st, "%s worker disappeared before completion; it may be retried" % kind)
    if not force and now - p.get("last", 0) < interval:
        return "idle"
    existing = _matching_transfer_pid(argv)
    if existing is not None:
        if _hung_transfer(st, existing, argv, timeout, kind, "argv-scan"):
            existing = _matching_transfer_pid(argv)
    if existing is not None:
        p["active"] = {"pid": existing, "started": now, "kind": kind,
                       "external": True, "adopted_at": now}
        event(st, "%s adopted existing queue transfer pid=%d; no duplicate started" %
              (kind, existing))
        return "active"
    token = "%s-%d-%d" % (st["name"], os.getpid(), int(now * 1000000))
    worker = os.path.join(os.path.dirname(__file__), "pull_worker.py")
    logpath = os.path.join(P["logs"], st["name"] + ".pull.log")
    cmd = [sys.executable, worker, "--status", status_path, "--token", token,
           "--timeout", str(int(timeout)), "--log", logpath, "--"] + list(argv)
    pr = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          start_new_session=True)
    p["active"] = {"token": token, "pid": pr.pid, "started": now, "kind": kind}
    log("GCALL %s job=%s argv=%s (async worker pid=%d)" %
        (kind, st["name"], " ".join(shlex.quote(a) for a in argv), pr.pid))
    return "started"

def proc_starttime(pid):
    """Field 22 of /proc/pid/stat — guards against PID reuse across a daemon restart."""
    try:
        with open("/proc/%d/stat" % pid) as fh:
            data = fh.read()
        fields = data[data.rindex(")") + 2:].split()
        return int(fields[19])
    except (IOError, OSError, ValueError, IndexError):
        return None

def read_rc(outdir):
    try:
        with open(os.path.join(outdir, "_rc.txt")) as fh:
            return int(fh.read().strip())
    except (IOError, OSError, ValueError):
        return None
