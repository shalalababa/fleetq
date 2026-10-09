#!/usr/bin/env python3
"""Spec-driven completion check.

Replaces the per-wave Python heredoc. Same predicates that the spec declared
for submission decide completion, so the two cannot drift.

STANDING RULE ENCODED HERE: markers are not evidence. A run that exits 0 and
writes its *_DONE marker can still have trained nothing -- that is exactly
what a resume-from-latest-checkpoint + idempotent-skip hole produced,
heterogeneously, across two waves, and it was never recorded until a design
review found it months later. So every observed counter is printed whether or not it passed,
and a wave-level outlier scan flags runs whose realized training disagrees
with the rest of their own cell even when their predicates pass.

Run-directory conventions read here (all optional; predicates in the spec are
what decide DONE):
  <run>/ckpt/<name>-<step>/done   finished checkpoints; highest step is reported
  <run>/scores.jsonl              one line per evaluation; count is reported
  <run>/*_DONE                    driver completion markers (reported, never trusted)
  <run>/PROGRESS                  `update=<n>` lines for drivers without step-named ckpts

Usage:
  wavecheck.py <wave_dir> [--runroot DIR] [--stage NAME] [--json] [--pending]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wavespec  # noqa: E402

STEP_RE = re.compile(r"(\d+)$")

# The checkpoint layout this was written against names each directory either `<timestamp>-<step>` or,
# when the driver writes no step, `<timestamp>` alone -- where the timestamp is
# `YYYYMMDDThhmmssF<microseconds>`. The bare form carries NO step, but it ends
# in digits, so the trailing-number match above happily reads the MICROSECOND
# field as a training step. One run type was written in exactly that bare form,
# so every such run in a wave reported a six-digit pseudo-step drawn from the
# clock, and the realized-training outlier check then compared those to each
# other and flagged the disagreement it had itself manufactured
# ("ckpt_step=669262 vs modal 649186", both pure timestamp noise, on two runs
# that were in fact complete).
#
# Recognising the bare timestamp is enough to fix it, and is deliberately
# narrow: any other naming convention still falls through to the loose match,
# so drivers whose layout has not been observed here keep their old behaviour.
TIMESTAMP_ONLY_RE = re.compile(r"^\d{8}T\d{6}F\d+$")


# --------------------------------------------------------------------------
# observations
# --------------------------------------------------------------------------

def ckpt_step(rundir: Path) -> int:
  """Highest step among checkpoints that actually finished writing.

  Returns 0 when the checkpoints carry no step in their names. That is the
  honest answer -- it means this run's progress cannot be read off the
  filesystem -- so a spec should only assert ckpt_step_min on runs whose
  checkpoints ARE step-named.
  """
  best = 0
  for p in glob.glob(str(rundir / "ckpt" / "*")):
    if os.path.exists(os.path.join(p, "done")):
      name = os.path.basename(p)
      if TIMESTAMP_ONLY_RE.match(name):
        continue
      m = STEP_RE.search(name)
      if m:
        best = max(best, int(m.group(1)))
  # Some drivers write a flat ckpt dir with its own `done`.
  if not best and (rundir / "ckpt" / "done").exists():
    best = -1
  return best


def count_lines(path: Path) -> int:
  try:
    with open(path, errors="ignore") as fh:
      return sum(1 for _ in fh)
  except OSError:
    return 0


def observe(rundir: Path) -> dict:
  o = {"exists": rundir.is_dir(), "ckpt_step": 0, "n_scores": 0,
       "markers": [], "progress": None}
  if not o["exists"]:
    return o
  o["ckpt_step"] = ckpt_step(rundir)
  o["n_scores"] = count_lines(rundir / "scores.jsonl")
  o["markers"] = sorted(p.name for p in rundir.glob("*_DONE"))
  pf = rundir / "PROGRESS"
  if pf.exists():
    for line in pf.read_text(errors="ignore").splitlines():
      if line.startswith("update="):
        try:
          o["progress"] = int(float(line.split("=", 1)[1]))
        except ValueError:
          pass
  return o


# --------------------------------------------------------------------------
# predicates
# --------------------------------------------------------------------------

def _resolve(rundir: Path, rel: str) -> Path:
  return Path(os.path.normpath(str(rundir / rel)))


def evaluate(rundir: Path, kind: str, arg) -> tuple[bool, str]:
  """Return (passed, observed-description). Observed is printed either way."""
  if kind == "exists":
    p = _resolve(rundir, str(arg))
    return p.exists(), f"{arg}={'yes' if p.exists() else 'NO'}"

  if kind == "absent":
    p = _resolve(rundir, str(arg))
    return not p.exists(), f"{arg}={'absent' if not p.exists() else 'PRESENT'}"

  if kind == "ckpt_step_min":
    # Plain int checks this run's dir. The dict form checks a sibling, because
    # a two-phase task (train, then a dependent second phase) writes TWO dirs,
    # and the first half is where truncation hides: the second phase can
    # finish normally on top of a model that stopped short.
    if isinstance(arg, dict):
      target = _resolve(rundir, str(arg["dir"]))
      got, want = ckpt_step(target), int(arg["min"])
      return got >= want, f"{arg['dir']}:ckpt_step={got}/{want}"
    got = ckpt_step(rundir)
    return got >= int(arg), f"ckpt_step={got}/{arg}"

  if kind == "jsonl_min_lines":
    p = _resolve(rundir, str(arg["file"]))
    got = count_lines(p)
    return got >= int(arg["n"]), f"{arg['file']}={got}/{arg['n']} lines"

  if kind == "json_field_min":
    p = _resolve(rundir, str(arg["field_file"] if "field_file" in arg else arg["file"]))
    if not p.exists():
      return False, f"{arg['file']}=MISSING"
    try:
      val = json.loads(p.read_text()).get(arg["field"])
    except (json.JSONDecodeError, OSError):
      return False, f"{arg['file']}=UNPARSEABLE"
    if val is None:
      return False, f"{arg['file']}:{arg['field']}=absent"
    return float(val) >= float(arg["min"]), f"{arg['field']}={val}/{arg['min']}"

  if kind == "file_contains":
    p = _resolve(rundir, str(arg["file"]))
    if not p.exists():
      return False, f"{arg['file']}=MISSING"
    hit = re.search(str(arg["pattern"]), p.read_text(errors="ignore")) is not None
    return hit, f"{arg['file']}~/{arg['pattern']}/={'yes' if hit else 'NO'}"

  if kind == "n_ep_vs_modal":
    # Evaluated wave-wide after the per-run pass; always passes here.
    return True, "n_ep=deferred"

  return False, f"UNKNOWN PREDICATE {kind}"


# --------------------------------------------------------------------------
# checking
# --------------------------------------------------------------------------

def check(spec: wavespec.WaveSpec, runroot: Path, stages=None) -> dict:
  results = []
  for r in spec.runs(stages):
    rundir = runroot / (r.logdir or r.run_id)
    try:
      obs = observe(rundir)
    except OSError as e:
      obs = {"exists": True, "ckpt_step": 0, "n_scores": 0, "markers": [],
             "progress": None, "unreadable": f"{e.__class__.__name__}: {e.filename or e}"}
    checks = []
    for p in r.done_when:
      # A predicate that cannot be READ is not a predicate that passed, and it
      # is not a reason to abandon the whole wave either. A single
      # mode-000 file in one run dir raised PermissionError out of
      # Path.exists() and killed the entire wave check -- 31 other runs went
      # unreported because of one file.
      # Unreadable is reported as NOT done, including for `absent`: an
      # unreadable path is unknown, and unknown must never read as satisfied.
      try:
        passed, desc = evaluate(rundir, p.kind, p.arg)
      except OSError as e:
        passed, desc = False, f"UNREADABLE ({e.__class__.__name__}: {e.filename or e})"
      checks.append({"predicate": p.describe(), "passed": passed, "observed": desc})
    if not obs["exists"]:
      status = "MISSING"
    elif all(c["passed"] for c in checks):
      status = "DONE"
    else:
      status = "PARTIAL"
    results.append({"stage": r.stage, "run_id": r.run_id, "kind": r.kind,
                    "target": r.target, "status": status, "checks": checks,
                    "observed": obs})

  # -- wave-level outlier scan ------------------------------------------------
  # Realized training that disagrees with the rest of the same cell is the
  # signature of a silently truncated resume. It passes every per-run
  # predicate, so only a comparison catches it.
  outliers = []
  by_cell = collections.defaultdict(list)
  for res in results:
    if res["status"] == "DONE":
      by_cell[(res["stage"], res["kind"])].append(res)
  for cell, members in by_cell.items():
    for field_ in ("ckpt_step", "n_scores"):
      vals = [m["observed"][field_] for m in members if m["observed"][field_]]
      if len(vals) < 3:
        continue
      modal = collections.Counter(vals).most_common(1)[0][0]
      if not modal:
        continue
      for m in members:
        got = m["observed"][field_]
        if got and abs(got - modal) / modal > 0.02:
          outliers.append({"run_id": m["run_id"], "field": field_,
                           "value": got, "modal": modal,
                           "cell": f"{cell[0]}/{cell[1]}"})
  return {"wave_id": spec.wave_id, "runroot": str(runroot),
          "results": results, "outliers": outliers}


def render(rep: dict) -> str:
  out = [f"wave: {rep['wave_id']}", f"runroot: {rep['runroot']}", ""]
  by_stage = collections.defaultdict(list)
  for r in rep["results"]:
    by_stage[r["stage"]].append(r)

  total_done = 0
  for stage, rows in by_stage.items():
    done = sum(1 for r in rows if r["status"] == "DONE")
    total_done += done
    out.append(f"=== stage {stage}: {done}/{len(rows)} DONE ===")
    for r in rows:
      if r["status"] == "DONE":
        # Print the counters even on success: this is where truncation shows.
        o = r["observed"]
        bits = []
        if o["ckpt_step"]:
          bits.append(f"step={o['ckpt_step']}")
        if o["n_scores"]:
          bits.append(f"scores={o['n_scores']}")
        out.append(f"  DONE    {r['run_id']}  {' '.join(bits)}")
      else:
        failed = [c for c in r["checks"] if not c["passed"]]
        detail = "; ".join(c["observed"] for c in failed) or "run dir absent"
        out.append(f"  {r['status']:<7} {r['run_id']}  {detail}")
    out.append("")

  if rep["outliers"]:
    out.append("=== REALIZED-TRAINING OUTLIERS ===")
    out.append("These passed their predicates but disagree with their own cell.")
    out.append("That is the shape of a silently truncated resume -- verify before reading.")
    for o in rep["outliers"]:
      out.append(f"  {o['run_id']}  {o['field']}={o['value']} vs modal {o['modal']}"
                 f"  [{o['cell']}]")
    out.append("")

  n = len(rep["results"])
  out.append(f"SUMMARY {total_done}/{n} DONE"
             + (f", {len(rep['outliers'])} outlier(s)" if rep["outliers"] else ""))
  return "\n".join(out)


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("wave")
  ap.add_argument("--runroot", default=os.environ.get("RUNROOT", ""))
  ap.add_argument("--stage", action="append", default=None)
  ap.add_argument("--json", action="store_true")
  ap.add_argument("--pending", action="store_true",
                  help="print only run_ids that are not DONE (requeue input)")
  args = ap.parse_args()

  if not args.runroot:
    print("ERROR: set --runroot or $RUNROOT", file=sys.stderr)
    return 2
  try:
    spec = wavespec.load(args.wave)
  except wavespec.SpecError as e:
    print(f"SPEC ERROR: {e}", file=sys.stderr)
    return 2

  rep = check(spec, Path(args.runroot), args.stage)
  if args.pending:
    for r in rep["results"]:
      if r["status"] != "DONE":
        print(r["run_id"])
  elif args.json:
    print(json.dumps(rep, indent=1))
  else:
    print(render(rep))

  incomplete = any(r["status"] != "DONE" for r in rep["results"])
  return 1 if (incomplete or rep["outliers"]) else 0


if __name__ == "__main__":
  sys.exit(main())
