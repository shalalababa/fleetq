#!/usr/bin/env python3
"""Classify failures and write decision-ready briefs.

Deterministic classification against failure_signatures.tsv -- the same table
box/lanes.sh greps on the box, so the phone alert and this brief agree.  What
this cannot match is escalated with its evidence attached, never guessed at.

The output is shaped for a decision, not for a log reader: what failed, why,
whether a retry can possibly help, what it would cost, and what is still
burning money while you decide. Everything here is mechanically derivable --
it is the reconstruction you would otherwise do by hand across four logs.

Usage:
  triage.py classify <logfile>...            one line per file
  triage.py brief --wave W [--events F]      full escalation brief
  triage.py signatures                       show the table
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleetpaths  # noqa: E402

SIGS = Path(__file__).resolve().parent / "failure_signatures.tsv"

RETRY_MEANING = {
    "same": "retry as-is is reasonable",
    "other": "will fail again here; needs different hardware",
    "never": "retrying cannot help",
    "unknown": "unclassified",
}


def load_signatures(path: Path = SIGS) -> list[dict]:
  out = []
  for line in path.read_text().splitlines():
    if not line.strip() or line.lstrip().startswith("#"):
      continue
    parts = line.split("\t")
    if len(parts) < 4:
      continue
    out.append({"class": parts[0].strip(), "retry": parts[1].strip(),
                "pattern": parts[2].strip(), "action": parts[3].strip()})
  return out


def classify(text: str, sigs: list[dict]) -> dict:
  for s in sigs:
    try:
      if re.search(s["pattern"], text, re.MULTILINE):
        return s
    except re.error:
      continue
  # Never guess. An unrecognised failure escalates with its evidence attached;
  # inventing a plausible cause is how a wrong fix gets applied at 3am.
  return {"class": "unknown", "retry": "unknown", "pattern": "",
          "action": "Not matched by any known signature. Read the log tail below. "
                    "Once diagnosed, append a line to failure_signatures.tsv so "
                    "the next occurrence classifies itself."}


def tail(path: Path, n: int = 25) -> str:
  try:
    return "\n".join(path.read_text(errors="ignore").splitlines()[-n:])
  except OSError:
    return ""


def cmd_classify(args) -> int:
  sigs = load_signatures()
  for f in args.logs:
    p = Path(f)
    s = classify(tail(p, 200), sigs)
    print(f"{s['class']:<24} retry={s['retry']:<8} {p.name}")
    if args.verbose:
      print(f"    {s['action']}")
  return 0


def cmd_brief(args) -> int:
  """Assemble the escalation brief for a wave from events + checker state."""
  sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
  import wavecheck, wavespec, durations  # noqa: E402

  wave_dir = fleetpaths.wave_dir(args.wave)
  spec = wavespec.load(wave_dir)
  runroot = Path(args.runroot or os.environ.get("RUNROOT", ""))
  if not str(runroot):
    print("ERROR: set --runroot or $RUNROOT", file=sys.stderr)
    return 2

  rep = wavecheck.check(spec, runroot)
  sigs = load_signatures()
  db = durations.load(Path(args.durations))

  by_stage = collections.defaultdict(lambda: {"done": 0, "total": 0, "bad": []})
  for r in rep["results"]:
    st = by_stage[r["stage"]]
    st["total"] += 1
    if r["status"] == "DONE":
      st["done"] += 1
    else:
      st["bad"].append(r)

  print(f"### {spec.wave_id}")
  for stage, s in by_stage.items():
    print(f"{stage}: {s['done']}/{s['total']} DONE")

  # Classify each incomplete run from its own log.
  buckets = collections.defaultdict(list)
  for r in rep["results"]:
    if r["status"] == "DONE":
      continue
    log = runroot / "_cloud_logs" / f"{r['run_id']}.out"
    s = classify(tail(log, 200), sigs) if log.exists() else \
        {"class": "never_started", "retry": "same",
         "action": "No log: the task never ran. It is still queued, was removed, "
                   "or its lane aborted before reaching it."}
    buckets[(s["class"], s["retry"], s["action"])].append(r["run_id"])

  if not buckets:
    print("\nAll runs complete.")
  for (cls, retry, action), runs in sorted(buckets.items()):
    print(f"\n--- {cls}  ({len(runs)} run(s)) ---")
    print(f"    retry: {retry} -- {RETRY_MEANING.get(retry, '')}")
    for rid in sorted(runs)[:8]:
      print(f"      {rid}")
    if len(runs) > 8:
      print(f"      ... and {len(runs) - 8} more")
    # Cost of redoing them, from measurement rather than guesswork.
    kinds = {r["kind"] for r in rep["results"] if r["run_id"] in set(runs)}
    for k in kinds:
      secs, why = durations.predict(db, k, args.gpu)
      if secs:
        print(f"    re-run cost: ~{durations.fmt(secs)} each "
              f"({len(runs)} x, {why})")
    print(f"    {action}")

  if rep["outliers"]:
    print(f"\n--- realized-training outliers ({len(rep['outliers'])}) ---")
    print("    Passed their predicates but disagree with their own cell.")
    for o in rep["outliers"][:8]:
      print(f"      {o['run_id']}  {o['field']}={o['value']} vs modal {o['modal']}")
  return 0


def cmd_signatures(args) -> int:
  sigs = load_signatures()
  print(f"{'class':<24} {'retry':<8} pattern")
  for s in sigs:
    print(f"{s['class']:<24} {s['retry']:<8} {s['pattern'][:60]}")
  print(f"\n{len(sigs)} signature(s) in {SIGS}")
  print("Append a line whenever you diagnose something new -- that is how "
        "coverage grows instead of being re-derived.")
  return 0


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
  sub = ap.add_subparsers(dest="cmd", required=True)
  c = sub.add_parser("classify"); c.add_argument("logs", nargs="+")
  c.add_argument("-v", "--verbose", action="store_true")
  b = sub.add_parser("brief"); b.add_argument("--wave", required=True)
  b.add_argument("--runroot", default=None); b.add_argument("--gpu", default="unknown")
  b.add_argument("--durations", default=None)
  sub.add_parser("signatures")
  args = ap.parse_args()

  if args.cmd == "brief" and not args.durations:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import durations as _d
    args.durations = str(_d.DEFAULT_OUT)

  return {"classify": cmd_classify, "brief": cmd_brief,
          "signatures": cmd_signatures}[args.cmd](args)


if __name__ == "__main__":
  sys.exit(main())
