#!/usr/bin/env python3
"""Run one queue result transfer outside qd's scheduler loop.

The worker writes an atomic completion marker under spool/tmp.  It never reads
or writes a job state file: qd remains the sole owner of scheduling state.

On timeout the worker kills the WHOLE process group of the transfer, not just
the process it spawned.  `gsutil` (and `gcloud storage`) is a /bin/sh wrapper
that execs a bootstrap python that spawns the real tool, which in turn forks a
worker pool.  SIGTERM reliably kills the wrapper but NOT the innermost gsutil,
so waiting on the wrapper's exit and only then escalating to SIGKILL left a live
orphan behind on every timeout (4 stalled Batch waves in 24 h on 2026-09-15:
qd then "adopted" the orphan every cycle and the job's pull never ran again).
The rule here is: after the grace period, SIGKILL the group unconditionally and
record what was still alive in the marker.
"""
import argparse
import json
import os
import signal
import subprocess
import time

KILL_GRACE_SEC = 10.0


def write_status(path, obj):
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as fh:
        json.dump(obj, fh, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def pgid_members(pgid):
    """Live (non-zombie) pids in this process group, straight from /proc."""
    out = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for e in entries:
        if not e.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % e) as fh:
                data = fh.read()
            fields = data[data.rindex(")") + 2:].split()
            if fields[0] == "Z":
                continue
            if int(fields[2]) == pgid:
                out.append(int(e))
        except (IOError, OSError, ValueError, IndexError):
            continue
    return out


def kill_group(pgid, grace=KILL_GRACE_SEC):
    """SIGTERM the group, then SIGKILL whatever is still alive after `grace`.

    Returns (survivors_after_term, survivors_after_kill).  Refuses to signal our
    own process group or pgid <= 1 — killpg(0) would take down the daemon side.
    """
    if not pgid or pgid <= 1 or pgid == os.getpgrp():
        return ([], [])
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + max(0.0, grace)
    left = pgid_members(pgid)
    while left and time.time() < deadline:
        time.sleep(0.25)
        left = pgid_members(pgid)
    termed = list(left)
    # Unconditional, even when the group looks empty: killpg on an empty group
    # is a harmless ESRCH, and the "looks empty" read is exactly the race that
    # let the innermost gsutil survive.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    deadline = time.time() + 5.0
    left = pgid_members(pgid)
    while left and time.time() < deadline:
        time.sleep(0.2)
        left = pgid_members(pgid)
    return (termed, left)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--timeout", type=int, required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--kill-grace", type=float, default=KILL_GRACE_SEC)
    ap.add_argument("argv", nargs=argparse.REMAINDER)
    ns = ap.parse_args()
    argv = ns.argv[1:] if ns.argv[:1] == ["--"] else ns.argv
    if not argv:
        ap.error("missing transfer command")
    os.makedirs(os.path.dirname(ns.status), exist_ok=True)
    started = time.time()
    write_status(ns.status, {"token": ns.token, "pid": os.getpid(),
                             "state": "RUNNING", "started": started,
                             "argv": argv})
    killed = None
    with open(ns.log, "ab", buffering=0) as log:
        log.write(("%s transfer-start %s\n" % (time.strftime("%FT%T%z"), " ".join(argv))).encode())
        pr = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                              start_new_session=True)
        # start_new_session makes the child a session+group leader, so its pgid
        # is its pid and the whole tool tree (wrapper sh -> bootstrap python ->
        # gsutil -> worker pool) lands in that one group.
        try:
            pgid = os.getpgid(pr.pid)
        except OSError:
            pgid = pr.pid
        # If the worker itself is killed, qd can still identify this exact child
        # and avoid launching a duplicate transfer against the same cache tree.
        write_status(ns.status, {"token": ns.token, "pid": os.getpid(),
                                 "child_pid": pr.pid, "child_pgid": pgid,
                                 "state": "RUNNING",
                                 "started": started, "argv": argv})
        try:
            rc = pr.wait(timeout=max(1, ns.timeout))
            err = ""
        except subprocess.TimeoutExpired:
            rc, err = 124, "timeout after %ss" % ns.timeout
            termed, left = kill_group(pgid, ns.kill_grace)
            killed = {"pgid": pgid, "sigterm_survivors": termed,
                      "sigkill_survivors": left}
            err += "; killed pgid=%d (term_survivors=%d kill_survivors=%d)" % (
                pgid, len(termed), len(left))
            log.write(("%s transfer-kill pgid=%d term_survivors=%s "
                       "kill_survivors=%s\n" %
                       (time.strftime("%FT%T%z"), pgid, termed, left)).encode())
            try:
                pr.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        finished = time.time()
        log.write(("%s transfer-done rc=%d %.1fs %s\n" %
                   (time.strftime("%FT%T%z"), rc, finished - started, err)).encode())
    out = {"token": ns.token, "pid": os.getpid(),
           "state": "DONE", "started": started,
           "finished": finished, "rc": rc, "err": err}
    if killed:
        out["killed"] = killed
    write_status(ns.status, out)


if __name__ == "__main__":
    main()
