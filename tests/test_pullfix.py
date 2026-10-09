#!/usr/bin/env python3
"""Regression tests for the 2026-09-15 stalled-pull incident.

Two independent defects let a timed-out `gsutil rsync` survive as an orphan and
then freeze a job's pull forever:

  1. pull_worker only escalated to SIGKILL when the process it spawned was still
     alive.  gsutil is a /bin/sh wrapper that dies on SIGTERM while the real
     gsutil underneath ignores it, so the wrapper's clean exit looked like a
     successful kill and the innermost process ran on for hours.
  2. qcommon.async_transfer ADOPTED any live process matching the job's cache
     destination, with no upper bound on its age, so the orphan kept the job's
     pull "active" and site_batch never backfilled another chunk.

Run:  python3 tests/test_pullfix.py     (or pytest tests/test_pullfix.py)
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
QUEUE = os.path.dirname(HERE)
LIB = os.path.join(QUEUE, "lib")

TMPROOT = tempfile.mkdtemp(prefix="qpullfix-")
os.environ["Q_ROOT"] = TMPROOT
os.environ.setdefault("Q_CONFIG", os.path.join(TMPROOT, "config.env"))
open(os.environ["Q_CONFIG"], "w").write("Q_BUCKET=fake\n")
sys.path.insert(0, LIB)

import qcommon as Q                                          # noqa: E402

Q.ensure_tree()
WORKER = os.path.join(LIB, "pull_worker.py")


# ------------------------------------------------------------------ helpers

def alive(pid):
    """Live and not a zombie — an unreaped corpse still answers kill(pid, 0)."""
    try:
        with open("/proc/%d/stat" % int(pid)) as fh:
            data = fh.read()
        return data[data.rindex(")") + 2:].split()[0] != "Z"
    except (IOError, OSError, ValueError, IndexError):
        return False


def write_fake_tool(path, body):
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, 0o755)
    return path


def kill_pgid(pid):
    """Best-effort teardown; never lets a test leak a process."""
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
    except OSError:
        pass
    try:
        os.kill(int(pid), signal.SIGKILL)
    except OSError:
        pass


def wait_until(fn, limit=10.0, step=0.1):
    end = time.time() + limit
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return fn()


# ------------------------------------------------- 1. worker kills the group

def test_worker_timeout_kills_whole_group():
    """A SIGTERM-immune grandchild must not outlive the worker's timeout."""
    d = tempfile.mkdtemp(dir=TMPROOT)
    marker = os.path.join(d, "pull-fake.json")
    # The shape of the real bug: wrapper dies on TERM, inner process ignores it.
    tool = write_fake_tool(os.path.join(d, "gsutil"),
                           '#!/bin/sh\nsh -c \'trap "" TERM; echo $$ > "%s"; '
                           'exec sleep 900\' &\nwait\n' % os.path.join(d, "inner.pid"))
    pr = subprocess.run([sys.executable, WORKER, "--status", marker,
                         "--token", "t1", "--timeout", "3", "--kill-grace", "2",
                         "--log", os.path.join(d, "pull.log"), "--",
                         tool, "gs://fake/src", d], timeout=120)
    assert pr.returncode == 0
    st = Q.read_json(marker)
    assert st["state"] == "DONE" and st["rc"] == 124, st
    assert "killed" in st and st["killed"]["sigkill_survivors"] == [], st
    inner = int(open(os.path.join(d, "inner.pid")).read().strip())
    # The whole point: the TERM-immune process is GONE, not merely orphaned.
    assert st["killed"]["sigterm_survivors"], "test tool did not survive SIGTERM"
    assert not alive(inner), "orphan %d survived the worker timeout" % inner


def test_worker_normal_exit_is_untouched():
    d = tempfile.mkdtemp(dir=TMPROOT)
    marker = os.path.join(d, "pull-ok.json")
    tool = write_fake_tool(os.path.join(d, "gsutil"), "#!/bin/sh\nexit 0\n")
    subprocess.run([sys.executable, WORKER, "--status", marker, "--token", "t2",
                    "--timeout", "30", "--log", os.path.join(d, "pull.log"), "--",
                    tool, "gs://fake/src", d], timeout=120, check=True)
    st = Q.read_json(marker)
    assert st["state"] == "DONE" and st["rc"] == 0 and "killed" not in st, st


# --------------------------------------------- 2. qd retires a hung transfer

def _fake_transfer(dest, seconds=900):
    """A live process that looks exactly like this job's gsutil pull."""
    # NO exec: the wrapper must keep its argv, or nothing identifies it as this
    # job's transfer (real gsutil keeps src/dest on every level of its wrapper
    # chain, which is what _matching_transfer_pid keys on).
    tool = write_fake_tool(os.path.join(dest, "gsutil"),
                           "#!/bin/sh\nsleep %d\n" % seconds)
    return subprocess.Popen([tool, "gs://fake/jobs/j/out", dest],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


def test_hung_adopted_transfer_is_killed_and_pull_restarts():
    d = tempfile.mkdtemp(dir=TMPROOT)
    st = {"name": "hungjob", "tasks": []}
    pr = _fake_transfer(d)
    argv = [os.path.join(d, "gsutil"), "gs://fake/jobs/j/out", d]
    old_floor = Q.HUNG_FLOOR_SEC
    Q.HUNG_FLOOR_SEC = 0.5                 # 2*timeout(0.5) vs a 0.5 s floor
    try:
        # young: adopted, never signalled
        phase = Q.async_transfer(st, argv, 30, force=True, kind="pull")
        assert phase == "active", phase
        assert alive(pr.pid), "a young transfer must NOT be killed"
        # now age past 2x the timeout
        time.sleep(2.0)
        st["pull"].pop("active", None)
        phase = Q.async_transfer(st, argv, 0.5, force=True, kind="pull")
        assert wait_until(lambda: not alive(pr.pid)), "hung transfer survived"
        assert phase == "started", phase
        ev = " ".join(st.get("events", []))
        assert "hung pull killed" in ev, ev
        new_pid = st["pull"]["active"]["pid"]
        assert new_pid != pr.pid
    finally:
        Q.HUNG_FLOOR_SEC = old_floor
        kill_pgid(pr.pid)
        try:
            kill_pgid(st["pull"]["active"]["pid"])
        except (KeyError, TypeError):
            pass


def test_hung_marker_child_is_killed_and_marker_cleared():
    """Worker dead, its child alive and older than 2x the timeout."""
    d = tempfile.mkdtemp(dir=TMPROOT)
    st = {"name": "markerjob", "tasks": []}
    pr = _fake_transfer(d)
    argv = [os.path.join(d, "gsutil"), "gs://fake/jobs/j/out", d]
    status = Q._transfer_status("markerjob")
    Q.write_json_atomic(status, {"token": "tok", "pid": 999999999,
                                 "child_pid": pr.pid, "state": "RUNNING",
                                 "started": time.time() - 3600, "argv": argv})
    old_floor = Q.HUNG_FLOOR_SEC
    Q.HUNG_FLOOR_SEC = 0.5
    try:
        time.sleep(1.5)
        phase = Q.async_transfer(st, argv, 0.5, force=True, kind="pull")
        assert wait_until(lambda: not alive(pr.pid)), "hung child survived"
        assert phase == "started", phase
        assert not os.path.exists(status) or \
            Q.read_json(status, {}).get("token") != "tok", "stale marker kept"
        assert "hung pull killed" in " ".join(st.get("events", []))
    finally:
        Q.HUNG_FLOOR_SEC = old_floor
        kill_pgid(pr.pid)
        try:
            kill_pgid(st["pull"]["active"]["pid"])
        except (KeyError, TypeError):
            pass


def test_unrelated_process_is_never_killed():
    """The argv check is what makes a SIGKILL from qd safe."""
    d = tempfile.mkdtemp(dir=TMPROOT)
    pr = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        assert not Q._is_transfer_proc(pr.pid, ["gsutil", "gs://x", d])
        assert not Q._kill_transfer_proc(pr.pid, ["gsutil", "gs://x", d])
        assert alive(pr.pid), "an unrelated process was signalled"
    finally:
        kill_pgid(pr.pid)


def test_unrecognised_transfer_keeps_its_adoption():
    """Fail-safe: if we cannot confirm the tool, we must NOT release the slot.

    Releasing it without killing would let qd start a SECOND transfer into the
    same cache tree, which is worse than the stall this fix removes.
    """
    d = tempfile.mkdtemp(dir=TMPROOT)
    st = {"name": "oddjob", "tasks": []}
    tool = write_fake_tool(os.path.join(d, "mover.sh"), "#!/bin/sh\nsleep 60\n")
    pr = subprocess.Popen([tool, "gs://fake/jobs/j/out", d],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          start_new_session=True)
    argv = [tool, "gs://fake/jobs/j/out", d]
    old_floor = Q.HUNG_FLOOR_SEC
    Q.HUNG_FLOOR_SEC = 0.5
    try:
        time.sleep(2.0)
        phase = Q.async_transfer(st, argv, 0.5, force=True, kind="pull")
        assert phase == "active", phase
        assert alive(pr.pid), "an unrecognised transfer must not be killed"
    finally:
        Q.HUNG_FLOOR_SEC = old_floor
        kill_pgid(pr.pid)


# --------------------------------------------------- 3. batch pull transport

def test_batch_pull_argv_shape():
    sys.path.insert(0, LIB)
    import site_batch
    cfg = Q.load_config()
    cfg["Q_PULL_EXCLUDE_DIRS"] = "deps|replay_deps"
    site = site_batch.BatchSite(cfg)
    site._gcs_rsync = (True, True)                  # pretend gcloud storage is there
    a = site.pull_argv("gs://b/jobs/j/out", "/cache/j", force=False)
    assert a[1:3] == ["storage", "rsync"] and "--recursive" in a
    assert "--exclude=(^|/)(deps|replay_deps)(/|$)" in a
    assert "--checksums-only" not in a
    assert a[-2:] == ["gs://b/jobs/j/out", "/cache/j"], a
    b = site.pull_argv("gs://b/jobs/j/out", "/cache/j", force=True)
    assert "--checksums-only" in b and b[-2:] == a[-2:]
    site._gcs_rsync = (False, False)                # fallback path
    c = site.pull_argv("gs://b/jobs/j/out", "/cache/j", force=True)
    assert c[1:4] == ["-q", "rsync", "-r"] and "-c" in c and c[-2:] == a[-2:], c
    assert c[c.index("-x") + 1] == r"(^|[/\\])(deps|replay_deps)([/\\]|$)", c
    assert site.pull_timeout >= 900
    cfg["Q_PULL_EXCLUDE_DIRS"] = ""                 # default: exclude nothing
    site = site_batch.BatchSite(cfg)
    site._gcs_rsync = (True, True)
    d = site.pull_argv("gs://b/jobs/j/out", "/cache/j", force=False)
    assert not any(x.startswith("--exclude") for x in d), d


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for fn in fns:
        t0 = time.time()
        try:
            fn()
            print("PASS %-52s %.1fs" % (fn.__name__, time.time() - t0))
        except Exception as e:                       # noqa: BLE001
            bad += 1
            print("FAIL %-52s %s: %s" % (fn.__name__, type(e).__name__, e))
    shutil.rmtree(TMPROOT, ignore_errors=True)
    print("%d/%d passed" % (len(fns) - bad, len(fns)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
