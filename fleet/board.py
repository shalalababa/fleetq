#!/usr/bin/env python3
"""Fleet board: every lane on every box, what it is running, when it drains.

Replaces a hand-maintained "box 4 (4 GPUs): <wave> [RUNNING | ETA 23:00]"
block.  The ETA is re-projected from measured durations on every call, so it
updates itself as tasks complete instead of being recomputed by hand at 2am.

Reads `fleet board-raw` on stdin (one JSON object per box).

Usage:
  fleet board-raw | board.py [--gpu 4090]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import durations  # noqa: E402


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--gpu", default="unknown")
  ap.add_argument("--durations", default=str(durations.DEFAULT_OUT))
  args = ap.parse_args()

  db = durations.load(Path(args.durations))
  spec_index = durations.build_spec_index()

  # Decode a STREAM of JSON values rather than assuming one per line. A
  # producer that emits pretty-printed or newline-terminated records would
  # otherwise be silently dropped whole -- which is exactly how this reported
  # "no instances" while every instance was answering fine.
  raw = sys.stdin.read()
  records, dec, i = [], json.JSONDecoder(), 0
  while i < len(raw):
    while i < len(raw) and raw[i] in " \t\r\n":
      i += 1
    if i >= len(raw):
      break
    try:
      obj, end = dec.raw_decode(raw, i)
    except json.JSONDecodeError:
      nl = raw.find("\n", i)
      if nl == -1:
        break
      i = nl + 1
      continue
    records.append(obj)
    i = end

  rows, idle, dead, unmeasured = [], [], [], 0
  for rec in records:
    if not isinstance(rec, dict):
      continue
    inst, state = rec.get("instance", "?"), rec.get("state")
    if not state:
      rows.append((inst, "-", "UNREACHABLE", "", ""))
      continue
    gpus = {str(g["index"]): g for g in state.get("gpus", [])}
    lanes = state.get("lanes", [])
    if not lanes:
      idle.append(inst)
    for ln in lanes:
      pending = ln.get("pending", 0)
      running = ln.get("running_task", "")
      # family_of() applies the same kind/arm refinement the durations DB
      # is keyed by; the bare spec kind would miss every bucket.
      fam = durations.family_of(running or "", spec_index)
      per, why = durations.predict(db, fam, args.gpu)
      if per is None:
        unmeasured += 1
        eta = "??"
      else:
        # Remaining = the running task (assume half elapsed on average, since
        # we do not know its start) + everything still queued behind it.
        eta_s = per * (pending - 1 if pending else 0) + per // 2
        eta = time.strftime("%H:%M", time.localtime(time.time() + eta_s)) \
            + f" (+{durations.fmt(eta_s)})"
      status = "RUNNING"
      if not ln.get("alive", True):
        status = "DEAD"
        dead.append(f"{inst}/lane{ln['lane']}")
      elif ln.get("aborted"):
        status = "ABORTED"
      g = gpus.get(str(ln.get("lane")))
      util = f"{g['util']}% {g['mem_used']}/{g['mem_total']}MiB" if g else ""
      rows.append((inst, ln.get("lane", "?"), status,
                   f"{ln.get('next', 0) - 1}/{ln.get('total', 0)} done, {pending} queued",
                   f"{eta}  {util}"))

  if not rows:
    print("no instances reported. Is IP<N>/PORT<N> exported?")
    return 0

  w = max(len(str(r[3])) for r in rows)
  print(f"{'INST':<6} {'LANE':<5} {'STATUS':<8} {'PROGRESS':<{w}}  DRAINS / GPU")
  last = None
  for inst, lane, status, prog, eta in rows:
    print(f"{inst if inst != last else '':<6} {str(lane):<5} {status:<8} {prog:<{w}}  {eta}")
    last = inst

  print()
  if dead:
    print(f"!! DEAD supervisors: {', '.join(dead)} -- lane claims active, nothing running")
  if idle:
    print(f"!! IDLE and billing: instance(s) {', '.join(idle)} have no active queue")
  if unmeasured:
    print(f"   {unmeasured} lane(s) show ?? -- first run of a kind with no measurement yet")
  if not (dead or idle):
    print("   all reporting lanes are running")
  return 0


if __name__ == "__main__":
  sys.exit(main())
