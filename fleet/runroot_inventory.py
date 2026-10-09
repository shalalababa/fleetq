#!/usr/bin/env python3
"""Emit realized counters for every top-level entry in a runroot.

Written to answer exactly one question: *is what lives on this box also
somewhere safe?* It is deliberately self-contained (stdlib only, no repo
imports) so the same file can be piped to a rented box, to an archive host, or
run locally and produce comparable JSON on all three.

Counters, not markers -- the same rule the wave checker follows. A directory is
described by what it actually contains (checkpoint step, score lines, file
count, bytes), because a name matching on both sides proves nothing about
whether the bytes arrived.

Usage:
  runroot_inventory.py <runroot> [--exclude name ...] [--only name ...]

--only restricts the walk to the named entries. The archive check asks
"is what lives on THIS BOX also on the archive host?", which needs counters
for the ~40 names the box holds, not for all 2300 dirs in the archive.
Walking the whole 591 GB archive tree took 249 s per call, and every
`fleet destroy` paid it two or three times. Restricting the archive side to
the box's own names is not a weaker check: a name absent from --only is a name
the box does not have, so it cannot be data at risk.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import time

# `<timestamp>-<step>` carries a step; a bare `<timestamp>` does not. Matching
# trailing digits on the bare form reads the microsecond field as a training
# step -- see fleet/wavecheck.py for the incident this mirrors.
TIMESTAMP_ONLY_RE = re.compile(r"^\d{8}T\d{6}F\d+$")
STEP_RE = re.compile(r"(\d+)$")

# Operational scaffolding, not results. These are recreated by the lanes and
# the watchdog on any box and are never worth protecting.
DEFAULT_EXCLUDE = {"_cloud_logs", "_queue_control", "_waves", "lost+found"}


def ckpt_step(d: str) -> int:
  best = 0
  for p in glob.glob(os.path.join(d, "ckpt", "*")):
    if not os.path.exists(os.path.join(p, "done")):
      continue
    name = os.path.basename(p)
    if TIMESTAMP_ONLY_RE.match(name):
      continue
    m = STEP_RE.search(name)
    if m:
      best = max(best, int(m.group(1)))
  return best


def count_lines(path: str) -> int:
  try:
    with open(path, errors="ignore") as fh:
      return sum(1 for _ in fh)
  except OSError:
    return 0


def walk(d: str, shallow: bool = False) -> tuple[int, int, float]:
  """(file count, total bytes, newest mtime). Symlinks counted, never followed.

  The newest mtime is what tells a caller the subtree is still being written.
  It is the only signal here that does not depend on names matching up, which
  is why a reclaimer should refuse on it."""
  n = size = 0
  newest = 0.0
  for root, dirs, files in os.walk(d, followlinks=False):
    if shallow:
      dirs[:] = []
    for f in files:
      p = os.path.join(root, f)
      n += 1
      try:
        st = os.lstat(p)
        size += st.st_size
        if st.st_mtime > newest:
          newest = st.st_mtime
      except OSError:
        pass
  return n, size, newest


# The files that IDENTIFY a run, as opposed to the bytes that merely accumulate
# in it. A config is written once and says which arm this is; checkpoints say
# only how far it got. Counters (n_files/bytes) cannot distinguish two runs of
# the same shape under the same name -- e.g. a run trained with the wrong arm
# and its re-run: identical in size, opposite in one config flag. Hashing
# ~10 MB of configs across a 591 GB tree costs 0.03 s; hashing the tree costs
# 28 min and would catch nothing this does not.
IDENTITY_FILES = ("config.yaml", "config.json", "manifest.json", "MANIFEST.sha256")

WITNESS_ERROR = "ERROR"


def witness(d: str) -> dict:
  """{filename: sha256} over the run's identity files.

  A MAP, not one hash over the concatenation: the archive may hold MORE than
  a box (a bundle may add files), so the comparison must be "same hash on
  every file both sides have, and the archive is not missing any" -- concatenating made
  an extra config.json on one side read as a hard mismatch and would have
  trained the operator to type the unsafe override past real ones.

  An unreadable file yields WITNESS_ERROR for that name, never "": in a safety
  gate "cannot compare" and "I/O failed" must not be the same value."""
  out = {}
  for nm in IDENTITY_FILES:
    p = os.path.join(d, nm)
    if not os.path.isfile(p):
      continue
    h = hashlib.sha256()
    try:
      with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
          h.update(chunk)
      out[nm] = h.hexdigest()
    except OSError:
      out[nm] = WITNESS_ERROR
  return out


# A run dir carries its own producer output. An entry with none of these is a
# CONTAINER holding other runs (e.g. `sweep_a/<run_id>/`), and its name says
# nothing about its contents -- two directories can share a name on two hosts
# and hold entirely different data.
# What a PRODUCER leaves behind. `config.yaml` alone is deliberately NOT here:
# a wave-level config sitting in a container would otherwise make the container
# read as a run, its children would never be compared, and two unrelated trees
# sharing that generator-written config would compare as COVERED -- the
# container incident below, one directory deeper, with the witness actively
# confirming it.
PRODUCER_MARKS = ("scores.jsonl", "metrics.jsonl", "ckpt")


def _looks_like_run(d: str) -> bool:
  if any(os.path.exists(os.path.join(d, m)) for m in PRODUCER_MARKS):
    return True
  return any(os.path.isfile(os.path.join(d, m)) for m in IDENTITY_FILES)


def _is_own_run(d: str) -> bool:
  """d is unambiguously a run dir in its own right: it carries BOTH a producer
  mark and an identity file DIRECTLY. The namespace dir that once cost 16 live
  runs had neither at its top level, so this cannot readmit that case."""
  return (any(os.path.exists(os.path.join(d, m)) for m in PRODUCER_MARKS)
          and any(os.path.isfile(os.path.join(d, m)) for m in IDENTITY_FILES))


def is_container(d: str) -> bool:
  """Structure decides, not marks: a dir holding run-shaped children is a
  namespace even if it also carries a file that looks like a mark."""
  # ...unless d is itself a run. Some runs write <subdir>/manifest.json, and
  # manifest.json is an IDENTITY file, so the child test alone declared every
  # such run a container. That
  # is a fail-open, not just noise: destroy sorts containers into `boxes`,
  # which are skipped by the coverage gate -- so the run's own scores.jsonl,
  # metrics.jsonl and config.yaml went unchecked and unwitnessed.
  if _is_own_run(d):
    return False
  try:
    kids = [e for e in os.listdir(d) if os.path.isdir(os.path.join(d, e))]
  except OSError:
    return False
  if any(_looks_like_run(os.path.join(d, k)) for k in kids):
    return True
  if any(os.path.exists(os.path.join(d, m)) for m in PRODUCER_MARKS):
    return False
  return bool(kids)


def main() -> None:
  ap = argparse.ArgumentParser()
  ap.add_argument("runroot")
  ap.add_argument("--exclude", nargs="*", default=[])
  ap.add_argument("--only", nargs="*", default=None,
                  help="restrict to these entries (the caller's own names)")
  a = ap.parse_args()

  root = a.runroot
  if not os.path.isdir(root):
    print(json.dumps({"error": f"no such runroot: {root}"}))
    sys.exit(1)

  skip = DEFAULT_EXCLUDE | set(a.exclude)

  def entries_under(root_dir: str):
    """Yield (key, path) at RUN-DIR granularity, descending through containers.

    Comparing top-level names is what once let a namespace dir on a box
    match an unrelated same-named dir on the archive host and be judged
    covered -- 16 completed runs were deleted on that verdict. A container is
    not a unit of comparison: it is a namespace, and two namespaces sharing a
    name say nothing about the runs inside them. Descending makes the key
    `<container>/<run>`, which is a real run on both sides or on neither. One level only -- deeper nesting is not a layout we use, and
    unbounded recursion would walk replay shards as if they were runs."""
    for nm in sorted(os.listdir(root_dir)):
      if nm in skip:
        continue
      pp = os.path.join(root_dir, nm)
      if not os.path.isdir(pp):
        continue
      if is_container(pp):
        kids = [k for k in sorted(os.listdir(pp))
                if os.path.isdir(os.path.join(pp, k))]
        if kids:
          for k in kids:
            yield f"{nm}/{k}", os.path.join(pp, k)
          # Files sitting DIRECTLY in the container (a manifest beside its
          # sibling run dirs) belong to nobody once the kids are keyed
          # separately, so they would be neither compared nor missable. Keep
          # the container as its own shallow entry for exactly those bytes.
          if any(os.path.isfile(os.path.join(pp, e)) for e in os.listdir(pp)):
            yield nm, pp, True          # shallow=True
          continue
      yield nm, pp
  # --only is a filter, never a source: a requested name that is absent stays
  # absent from the output, which is what makes the caller report it MISSING
  # rather than silently treating it as covered.
  want = set(a.only) if a.only is not None else None
  out = {}
  for item in entries_under(root):
    shallow = len(item) == 3
    name, p = item[0], item[1]
    # --only may name either a container or a specific run; accept both so a
    # caller that knows only the top-level name still gets its children.
    if want is not None and not (name in want or name.split("/", 1)[0] in want):
      continue
    n_files, n_bytes, newest = walk(p, shallow=shallow)
    out[name] = {
        "ckpt_step": ckpt_step(p),
        "n_scores": count_lines(os.path.join(p, "scores.jsonl")),
        "markers": sorted(os.path.basename(m)
                          for m in glob.glob(os.path.join(p, "*_DONE"))),
        "n_files": n_files,
        "bytes": n_bytes,
        "newest_mtime": newest,
        "is_container": is_container(p),
        "witness": witness(p),
        "shallow": shallow,
    }
  # Schema version: a consumer given an older inventory would silently skip
  # the container / witness / freshness guards and still print SAFE.
  print(json.dumps({"schema": 3, "runroot": root, "now": time.time(),
                    "entries": out}))


if __name__ == "__main__":
  main()
