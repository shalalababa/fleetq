#!/usr/bin/env python3
"""A recycled local leader PID must not cause a signal to the new group."""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import qcommon as Q
from site_local import LocalSite


site = LocalSite(Q.load_config())
st = {"name": "reuse", "site": "local", "tasks": {"0": {
    "state": "RUNNING", "pid": 55555, "pgid": 55555,
    "starttime": 100, "mem_mib": 0, "timeout_sec": 0,
    "started": 0, "rss_peak_mib": 0,
}}}
table = {55555: (55555, 0), 55556: (55555, 0)}
signals = []
with patch.object(Q, "proc_starttime", return_value=200), \
        patch.object(Q, "read_rc", return_value=None), \
        patch.object(Q, "event"), \
        patch("site_local.os.killpg", side_effect=lambda pgid, sig: signals.append((pgid, sig))):
    site.poll([st], table=table)

assert st["tasks"]["0"]["state"] == "FAILED"
assert signals == [], signals
print("local PID reuse: no unrelated process group signaled")
