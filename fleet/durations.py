#!/usr/bin/env python3
"""Measured task durations from the logs you already have.

A hand-maintained runtime table goes stale; this one is derived from the
lane supervisors' own START/DONE lines (_cloud_logs/worker_shard*.out), so it
cannot, and it backfills from every existing log on day one.

Keying is (family x gpu), matching how runtimes actually vary: the same
training run measured ~2.5 h on one consumer GPU and ~1.7 h on a faster one.
`family` is the spec `kind` refined by arm where a wave spec knows the run, and
a prefix heuristic otherwise -- historical logs predate specs entirely.

Usage:
  durations.py scan <logdir>... [--gpu 4090] [--out $FLEET_STATE/durations.json]
  durations.py show [--out $FLEET_STATE/durations.json]
  durations.py predict <run_id> [--kind K] [--gpu G] [--per-gpu N]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleetpaths  # noqa: E402

DEFAULT_OUT = fleetpaths.STATE / "durations.json"

# [Fri Aug 14 20:40:52 CDT 2026] START name gpu=0 log=...
LINE_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+(?P<ev>START|DONE|FAIL)\s+(?P<name>\S+)"
    r"(?:\s+gpu=(?P<gpu>\S+))?.*?(?:elapsed=(?P<elapsed>\d+)s)?\s*$")
# Timezone names are not portably parseable; drop the token and use the rest.
TS_RE = re.compile(r"^(\w{3} \w{3} +\d+ \d{2}:\d{2}:\d{2}) +\S+ +(\d{4})$")

TRAILING = re.compile(r"(_seed\d+|_s\d+|_ckpt\d+|_pair\d+|\d+)+$")


def parse_ts(s: str) -> float | None:
  m = TS_RE.match(s.strip())
  if not m:
    return None
  try:
    return time.mktime(time.strptime(f"{m.group(1)} {m.group(2)}", "%a %b %d %H:%M:%S %Y"))
  except ValueError:
    return None


def variant_of(run_id: str) -> str:
  """The part of a run_id that actually predicts runtime: arm and task."""
  base = TRAILING.sub("", run_id).rstrip("_")
  base = base.replace("__", "_").strip("_")
  return base or "unknown"


def family_of(run_id: str, spec_index: dict[str, str]) -> str:
  """Spec kind REFINED BY ARM; otherwise a stripped prefix, clearly marked.

  The kind alone is too coarse to schedule on. Five arms sharing one kind
  once cost 0.77 h, 2.94 h, 3.01 h, 3.72 h and 6.28 h -- an 8x range that the
  kind's median reports as one 3.0 h number. Predicting 3.0h for a 6.3h cell is exactly how lanes on the same
  box end up finishing hours apart, which is the thing packing must avoid.
  Within an arm the spread is under 3%, so arm x gpu is the key that predicts.
  """
  if run_id in spec_index:
    return f"{spec_index[run_id]}/{variant_of(run_id)}"
  return f"~{variant_of(run_id)}"


def build_spec_index() -> dict[str, str]:
  """run_id -> kind, from every generated wave in the repo."""
  idx: dict[str, str] = {}
  for f in glob.glob(str(fleetpaths.WAVES / "*" / "generated*" / "runs.json")):
    try:
      for r in json.load(open(f)).get("runs", []):
        idx[r["run_id"]] = r.get("kind", "default")
    except (OSError, json.JSONDecodeError, KeyError):
      continue
  return idx


def scan(logdirs: list[str], gpu: str) -> list[dict]:
  spec_index = build_spec_index()
  records: list[dict] = []
  for d in logdirs:
    for path in sorted(glob.glob(os.path.join(d, "worker_shard*.out"))):
      open_starts: dict[str, tuple[float, str]] = {}
      with open(path, errors="ignore") as fh:
        for line in fh:
          m = LINE_RE.match(line.rstrip("\n"))
          if not m:
            continue
          ts = parse_ts(m.group("ts"))
          name, ev = m.group("name"), m.group("ev")
          if ev == "START":
            if ts is not None:
              open_starts[name] = (ts, m.group("gpu") or "?")
          else:
            elapsed = m.group("elapsed")
            lane = "?"
            if name in open_starts:
              t0, lane = open_starts.pop(name)
              if elapsed is None and ts is not None:
                elapsed = str(int(ts - t0))
            if elapsed is None:
              continue
            secs = int(elapsed)
            if secs <= 0:
              continue  # skips and instant no-ops are not runtimes
            records.append({
                "run_id": name, "family": family_of(name, spec_index),
                "seconds": secs, "gpu": gpu, "lane": lane,
                "ok": ev == "DONE", "source": os.path.basename(path),
            })
  return records


def aggregate(records: list[dict]) -> dict:
  by: dict[str, list[int]] = {}
  for r in records:
    if not r["ok"]:
      continue  # a failed run's runtime predicts nothing
    by.setdefault(f"{r['family']}|{r['gpu']}", []).append(r["seconds"])
  out = {}
  for key, vals in sorted(by.items()):
    vals.sort()
    out[key] = {
        "n": len(vals),
        "median_s": int(statistics.median(vals)),
        "min_s": vals[0], "max_s": vals[-1],
        # A wide spread means the median is a poor ETA; say so rather than
        # letting a confident-looking number hide it.
        "spread": round(vals[-1] / vals[0], 2) if vals[0] else None,
    }
  return out


def fmt(secs: float) -> str:
  secs = int(secs)
  if secs < 90:
    return f"{secs}s"
  if secs < 5400:
    return f"{secs // 60}m"
  return f"{secs / 3600:.1f}h"


def load(out: Path) -> dict:
  """Never raise. A missing or truncated durations file must degrade to "no
  measurements" -- the board and packer are advisory, and crashing them over a
  cache file would take out the one view that shows a stalled fleet."""
  empty = {"records": [], "aggregate": {}}
  try:
    text = out.read_text()
  except OSError:
    return empty
  if not text.strip():
    return empty
  try:
    db = json.loads(text)
  except json.JSONDecodeError:
    print(f"WARN: {out} is not valid JSON; treating as empty", file=sys.stderr)
    return empty
  db.setdefault("records", [])
  db.setdefault("aggregate", {})
  return db


def predict(db: dict, family: str, gpu: str) -> tuple[int | None, str]:
  agg = db.get("aggregate", {})
  exact = agg.get(f"{family}|{gpu}")
  if exact:
    return exact["median_s"], f"n={exact['n']} on {gpu}"
  # Fall back across GPUs rather than returning nothing: a rough ETA beats "??"
  # for lane planning, as long as it is labelled as cross-GPU.
  hits = [(k, v) for k, v in agg.items() if k.split("|")[0] == family]
  if hits:
    best = max(hits, key=lambda kv: kv[1]["n"])
    return best[1]["median_s"], f"n={best[1]['n']} on {best[0].split('|')[1]} (other GPU)"
  # Same KIND, different arm. Coarse on purpose and labelled as such: it is the
  # estimate that pools arms costing 0.77 h and 6.28 h, so it is a last resort
  # before "??" -- never a number to pack lanes on.
  kind = family.split("/", 1)[0]
  pooled = [v for k, v in agg.items()
            if k.split("|")[0].split("/", 1)[0] == kind and k.split("|")[1] == gpu]
  if pooled:
    vals = sorted(v["median_s"] for v in pooled)
    n = sum(v["n"] for v in pooled)
    return vals[len(vals) // 2], f"n={n} across {len(pooled)} arm(s) of kind {kind} -- COARSE"
  return None, "no measurement"


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
  sub = ap.add_subparsers(dest="cmd", required=True)
  s = sub.add_parser("scan"); s.add_argument("logdirs", nargs="+")
  s.add_argument("--gpu", default="unknown")
  s.add_argument("--out", default=str(DEFAULT_OUT))
  sh = sub.add_parser("show"); sh.add_argument("--out", default=str(DEFAULT_OUT))
  pr = sub.add_parser("predict"); pr.add_argument("run_id")
  pr.add_argument("--kind", default=None); pr.add_argument("--gpu", default="unknown")
  pr.add_argument("--out", default=str(DEFAULT_OUT))
  args = ap.parse_args()
  out = Path(args.out)

  if args.cmd == "scan":
    db = load(out)
    fresh = scan(args.logdirs, args.gpu)
    seen = {(r["run_id"], r["seconds"], r["gpu"]) for r in db["records"]}
    added = [r for r in fresh if (r["run_id"], r["seconds"], r["gpu"]) not in seen]
    db["records"].extend(added)
    db["aggregate"] = aggregate(db["records"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(db, indent=1) + "\n")
    print(f"scanned {len(fresh)} completion(s), {len(added)} new "
          f"-> {len(db['records'])} total in {out}")
    return 0

  if args.cmd == "show":
    db = load(out)
    if not db["aggregate"]:
      print(f"no measurements yet in {out}\n"
            f"backfill with: durations.py scan $RUNROOT/_cloud_logs --gpu <gpu>")
      return 0
    print(f"{'family|gpu':<44} {'n':>4} {'median':>8} {'min':>8} {'max':>8}  spread")
    for k, v in db["aggregate"].items():
      warn = "  <- wide" if (v["spread"] or 1) > 2 else ""
      print(f"{k:<44} {v['n']:>4} {fmt(v['median_s']):>8} {fmt(v['min_s']):>8} "
            f"{fmt(v['max_s']):>8}  {v['spread']}{warn}")
    print("\n'~' prefixes are heuristic families from logs with no wave spec.")
    return 0

  db = load(out)
  fam = args.kind or family_of(args.run_id, build_spec_index())
  secs, why = predict(db, fam, args.gpu)
  print(f"{args.run_id}  family={fam}  "
        f"{'ETA ' + fmt(secs) if secs else 'UNKNOWN'}  ({why})")
  return 0


if __name__ == "__main__":
  sys.exit(main())
