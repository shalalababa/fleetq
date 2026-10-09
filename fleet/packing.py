#!/usr/bin/env python3
"""Advisory lane packing over measured durations.

Longest-processing-time-first: sort units by predicted runtime descending, put
each on the lane with the least work so far. It is the standard greedy bound
(within 4/3 of optimal) and it replaces the arithmetic otherwise done by hand
at 2am when deciding what will finish before morning.

Why it matters on rented boxes: a box bills for the whole machine, not per
busy GPU, and cannot be released until its LONGEST lane drains.  Six boxes
once sat half empty at the same time (33.4 idle GPU-hours); balancing on
predicted finish time -- not on cell count -- concentrates the idleness onto
whole boxes that can be pulled and destroyed early.

ADVISORY ONLY. It prints an assignment; a human submits. Nothing here schedules.

The ops doc's "order the file so each lane gets a balanced mix" rule exists
because plain round-robin over an expansion whose slowest dimension varies last
hands one lane every slow cell. LPT makes that rule unnecessary: it balances on
the actual numbers rather than on the ordering.

Usage:
  packing.py --wave <wave_id> [--lanes 0,1,2,3] [--gpu 4090] [--stage NAME]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import durations  # noqa: E402
import fleetpaths  # noqa: E402
import wavespec  # noqa: E402


def pack(units: list[tuple[str, int | None, int]], lanes: list[str]) -> dict:
  """units: (label, predicted_seconds|None, n_runs). Unknowns go last."""
  known = sorted([u for u in units if u[1] is not None], key=lambda u: -u[1])
  unknown = [u for u in units if u[1] is None]
  load = {ln: 0 for ln in lanes}
  assign: dict[str, list] = {ln: [] for ln in lanes}
  for label, secs, n in known:
    ln = min(lanes, key=lambda l: load[l])
    assign[ln].append((label, secs, n))
    load[ln] += secs
  # Unmeasured kinds cannot be balanced; spread them evenly so one lane does
  # not absorb every unknown, and flag them in the output.
  for i, (label, _, n) in enumerate(unknown):
    ln = lanes[i % len(lanes)]
    assign[ln].append((label, None, n))
  return {"assign": assign, "load": load, "unknown": len(unknown)}


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--wave", required=True)
  ap.add_argument("--lanes", default="0,1,2,3")
  ap.add_argument("--gpu", default="unknown")
  ap.add_argument("--stage", action="append", default=None)
  ap.add_argument("--durations", default=str(durations.DEFAULT_OUT))
  args = ap.parse_args()

  wave_dir = fleetpaths.wave_dir(args.wave)
  try:
    spec = wavespec.load(wave_dir)
  except wavespec.SpecError as e:
    print(f"SPEC ERROR: {e}", file=sys.stderr)
    return 2

  db = durations.load(Path(args.durations))
  spec_index = durations.build_spec_index()
  lanes = [x.strip() for x in args.lanes.split(",") if x.strip()]

  for st in spec.stages:
    if st.target != "lanes" or (args.stage and st.name not in args.stage):
      continue
    per_gpu = spec.kinds[st.kind].per_gpu if st.kind in spec.kinds else 1
    runs = st.expanded()
    groups = wavespec.pair_runs(runs, per_gpu)

    units = []
    for g in groups:
      # Predict per RUN, not per stage kind. One kind covered arms that differ
      # by 8x (0.77 h .. 6.28 h), so a kind-level estimate hands every lane the
      # same number and they finish hours apart -- which is the whole failure
      # packing exists to prevent.
      secs, _ = durations.predict(
          db, durations.family_of(g[0].run_id, spec_index), args.gpu)
      # A pair runs concurrently, so the unit costs about one member's runtime
      # (co-located tasks contend, hence the ~25% penalty observed for 2-way
      # co-residency -- applied here rather than assuming free parallelism).
      if secs is not None and len(g) > 1:
        secs = int(secs * 1.25)
      units.append((g[0].run_id if len(g) == 1
                    else f"{g[0].run_id} + {len(g) - 1} more", secs, len(g)))

    res = pack(units, lanes)
    total = sum(res["load"].values())
    print(f"=== stage {st.name}  kind={st.kind}  per_gpu={per_gpu}  "
          f"{len(runs)} run(s) in {len(units)} unit(s) ===")
    for ln in lanes:
      items = res["assign"][ln]
      known = [i for i in items if i[1] is not None]
      eta = res["load"][ln]
      unk = len(items) - len(known)
      print(f"  lane {ln}: {len(items):>2} unit(s)  "
            f"ETA {durations.fmt(eta) if eta else '??':>7}"
            + (f"  (+{unk} unmeasured)" if unk else ""))
      for label, secs, n in items:
        print(f"       {durations.fmt(secs) if secs else '  ??':>7}  {label}")
    if total:
      span = max(res["load"].values()) - min(res["load"].values())
      print(f"  balance: slowest lane {durations.fmt(max(res['load'].values()))}, "
            f"spread {durations.fmt(span)}")
      if span > 0.25 * max(res["load"].values()):
        print("  NOTE: lanes are uneven; with few large units this is unavoidable.")
    if res["unknown"]:
      print(f"  {res['unknown']} unit(s) have no measurement. Run one first while")
      print("  awake, then re-pack -- overnight waves should use measured kinds.")
    print()
  return 0


if __name__ == "__main__":
  sys.exit(main())
