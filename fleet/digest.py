#!/usr/bin/env python3
"""The morning digest.

Reads merged events.jsonl on stdin (`fleet digest` gathers them from every
box) and answers the four questions you actually wake up with:

  what happened, what fixed itself, what needs me, and what did the night cost.

The last one is the KPI. Idle GPU-hours burned overnight is the number that
says whether this system is working, and it is currently invisible -- there is
no way to know today whether a lane drained at 02:00 or 07:00.

Usage:
  fleet digest                       # gathers + renders
  cat events.jsonl | digest.py --since 20
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleetpaths  # noqa: E402

# Events that mean "you have a decision to make" rather than "FYI".
NEEDS_YOU = {"queue_abort", "supervisor_dead", "disk_full", "gpu_stall",
             "instance_idle"}
SELF_HEALED = {"gpu_stall_cleared", "supervisor_restarted"}


def fmt_dur(secs: float) -> str:
  secs = int(secs)
  if secs < 3600:
    return f"{secs // 60}m"
  return f"{secs / 3600:.1f}h"


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--since", type=float, default=16,
                  help="hours back to consider (default 16, i.e. since yesterday evening)")
  ap.add_argument("--instances", default=str(fleetpaths.STATE / "instances.json"),
                  help="used only for $/hr, so idle can be reported in the unit "
                       "decisions are actually made in")
  args = ap.parse_args()

  # label -> $/hr. Alert titles carry FL_INSTANCE_LABEL ("box4"), which is
  # box<index>, so it joins to the refresh inventory by index.
  cost = {}
  try:
    for e in json.loads(Path(args.instances).read_text()).get("instances", []):
      if e.get("cost_hr"):
        cost[f"box{e['index']}"] = float(e["cost_hr"])
  except (OSError, json.JSONDecodeError, KeyError, ValueError):
    pass

  cutoff = time.time() - args.since * 3600
  events = []
  for line in sys.stdin:
    line = line.strip()
    if not line or not line.startswith("{"):
      continue
    try:
      e = json.loads(line)
    except json.JSONDecodeError:
      continue
    if e.get("ts", 0) >= cutoff:
      events.append(e)
  events.sort(key=lambda e: e.get("ts", 0))

  if not events:
    print(f"No events in the last {args.since:g}h.")
    print("Either a quiet night or nothing is reporting -- check "
          "`fleet sh N 'bash $FLEET_BOX_HOME/watchdog.sh status'` "
          "if you expected activity. Silence is only good news when something "
          "is alive to break it.")
    return 0

  span = events[-1]["ts"] - events[0]["ts"]
  print(f"=== digest: {len(events)} event(s) over {fmt_dur(span)} "
        f"(last {args.since:g}h) ===\n")

  by_kind = collections.Counter(e.get("kind", "?") for e in events)
  by_host = collections.defaultdict(list)
  for e in events:
    by_host[e.get("host", "?")].append(e)

  # --- what needs you ------------------------------------------------------
  decisions = [e for e in events if e.get("kind") in NEEDS_YOU]
  if decisions:
    print("--- NEEDS YOU ---")
    # Collapse repeats: three reminders about one stuck lane is one decision.
    seen = {}
    for e in decisions:
      key = (e.get("host"), e.get("kind"), e.get("lane", ""))
      seen.setdefault(key, []).append(e)
    for (host, kind, lane), group in seen.items():
      first, last = group[0], group[-1]
      when = time.strftime("%H:%M", time.localtime(first["ts"]))
      rep = f" (x{len(group)}, latest {time.strftime('%H:%M', time.localtime(last['ts']))})" \
            if len(group) > 1 else ""
      print(f"  {when}  [{host}] {kind}{rep}")
      print(f"         {first.get('title', '')}")
      if kind == "queue_abort":
        print(f"         stranded={first.get('stranded', '?')} "
              f"lane={first.get('lane', '?')}")
    print()

  # --- failures by class ---------------------------------------------------
  fails = [e for e in events if e.get("kind") == "task_fail"]
  if fails:
    print("--- FAILURES BY CLASS ---")
    cls = collections.Counter(e.get("class", "unknown") for e in fails)
    for c, n in cls.most_common():
      retries = {e.get("retry", "?") for e in fails if e.get("class") == c}
      print(f"  {c:<22} {n:>3}  retry={'/'.join(sorted(retries))}")
      if c == "unknown":
        print("       ^ unclassified: diagnose once, then append a line to "
              "failure_signatures.tsv")
    print()

  # --- self-healed ---------------------------------------------------------
  healed = [e for e in events if e.get("kind") in SELF_HEALED]
  if healed:
    print("--- SELF-HEALED (no action needed) ---")
    for e in healed:
      print(f"  {time.strftime('%H:%M', time.localtime(e['ts']))}  "
            f"[{e.get('host')}] {e.get('title', '')}")
    print()

  # --- the KPI -------------------------------------------------------------
  # Idle episodes report cumulative seconds, so the largest value per host is
  # that episode's length. Approximate by construction: it undercounts idle
  # that ended before the first alert fired (10 min) and cannot see an
  # instance that never reported. Stated rather than silently rounded.
  print("--- COST ---")
  total_idle_gpu_h = 0.0
  total_money = 0.0
  for host, evs in sorted(by_host.items()):
    idles = [e for e in evs if e.get("kind") == "instance_idle"]
    if not idles:
      continue
    longest = max(e.get("idle_s", 0) for e in idles)
    ngpu = max((e.get("gpus", 1) or 1) for e in idles)
    gpu_h = longest / 3600 * ngpu
    total_idle_gpu_h += gpu_h
    money = ""
    if host in cost:
      spent = longest / 3600 * cost[host]
      total_money += spent
      money = f"  = ${spent:.2f}"
    print(f"  {host}: idle {fmt_dur(longest)} x {ngpu} GPU "
          f"= {gpu_h:.1f} GPU-hours{money}")
  if total_idle_gpu_h:
    print(f"  TOTAL IDLE: {total_idle_gpu_h:.1f} GPU-hours burned"
          + (f" = ${total_money:.2f}" if total_money else ""))
    print("  (lower bound: idle shorter than the 10m alert threshold is invisible,")
    print("   and an instance that stopped reporting entirely cannot be counted)")
  else:
    print("  No idle episodes reported. Every instance that reported stayed busy.")
  if not cost:
    print("  ($/hr unavailable -- run `fleet refresh` to record box costs)")
  print()

  print("--- ALL EVENT KINDS ---")
  for k, n in by_kind.most_common():
    print(f"  {k:<22} {n:>3}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
