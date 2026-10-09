#!/usr/bin/env python3
"""Wave spec model: load, validate, expand.

One spec file is the single source of truth for a wave. Everything that must
agree about "which runs are in this wave" -- the submitted commands, the
completion check, the rsync filters, the bundle contents -- is derived from
the run table this module produces, so they cannot silently disagree.

Deliberate non-goals:
  * run_ids are never invented. They come from an explicit list or from a
    template the user wrote, and the expanded set is printed for approval
    before anything is submitted.
  * no defaults that change science. steps/cfg/seed live in the spec's own
    command strings; this module substitutes variables and nothing else.

Shared by wavegen.py, wavecheck.py, and preflight generation.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # YAML specs are optional; JSON specs need only the stdlib.
  import yaml
except ImportError:  # pragma: no cover
  yaml = None

# lanes  = queued on a rented box's per-GPU lanes (box/lanes.sh)
# slurm  = submitted as sbatch jobs by the generated submit_slurm.sh
# manual = run some other way; still expanded, checked and pulled
TARGETS = ("lanes", "slurm", "manual")
ON_FAILURE = ("abort", "continue")

# ${...} names left for the remote shell to expand. Anything else unresolved is
# a typo, and a typo in a run_id template is how a wave quietly submits the
# wrong cells -- so it is an error, not a warning.
SHELL_PASSTHROUGH = {
    "RUNROOT", "REPO", "CONDA_ENV", "PYTHONPATH", "PATH",
    "HOME", "USER", "SLURM_JOB_ID", "CUDA_VISIBLE_DEVICES",
}

VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class SpecError(Exception):
  """Raised with a message that names the offending field."""


# --------------------------------------------------------------------------
# predicates
# --------------------------------------------------------------------------

PREDICATE_KEYS = {
    "exists", "absent", "ckpt_step_min", "jsonl_min_lines",
    "json_field_min", "file_contains", "n_ep_vs_modal",
}


@dataclass
class Predicate:
  kind: str
  arg: Any

  def describe(self) -> str:
    if isinstance(self.arg, dict):
      inner = " ".join(f"{k}={v}" for k, v in sorted(self.arg.items()))
      return f"{self.kind}({inner})"
    return f"{self.kind}({self.arg})"


def _parse_predicate(raw: Any, where: str) -> Predicate:
  if not isinstance(raw, dict) or len(raw) != 1:
    raise SpecError(f"{where}: each done_when entry must be a single-key mapping, got {raw!r}")
  (kind, arg), = raw.items()
  if kind not in PREDICATE_KEYS:
    raise SpecError(f"{where}: unknown predicate {kind!r}; known: {sorted(PREDICATE_KEYS)}")
  if kind in ("jsonl_min_lines",) and not (isinstance(arg, dict) and {"file", "n"} <= arg.keys()):
    raise SpecError(f"{where}: {kind} needs {{file, n}}")
  if kind == "json_field_min" and not (isinstance(arg, dict) and {"file", "field", "min"} <= arg.keys()):
    raise SpecError(f"{where}: json_field_min needs {{file, field, min}}")
  if (kind == "ckpt_step_min" and isinstance(arg, dict)
      and not {"dir", "min"} <= arg.keys()):
    raise SpecError(f"{where}: ckpt_step_min dict form needs {{dir, min}}")
  if kind == "file_contains" and not (isinstance(arg, dict) and {"file", "pattern"} <= arg.keys()):
    raise SpecError(f"{where}: file_contains needs {{file, pattern}}")
  return Predicate(kind, arg)


# --------------------------------------------------------------------------
# substitution
# --------------------------------------------------------------------------

def substitute(template: str, vars_: dict[str, Any], where: str) -> str:
  """Replace ${var} from vars_, leave shell variables alone, reject typos."""
  def repl(m: re.Match) -> str:
    name = m.group(1)
    if name in vars_:
      return str(vars_[name])
    if name in SHELL_PASSTHROUGH:
      return m.group(0)
    raise SpecError(
        f"{where}: unknown variable ${{{name}}}. "
        f"Declared: {sorted(vars_)}; shell passthrough: {sorted(SHELL_PASSTHROUGH)}")
  return VAR_RE.sub(repl, template)


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------

@dataclass
class Kind:
  name: str
  per_gpu: int = 1
  pairing: str = "homogeneous"

  def validate(self) -> None:
    if self.per_gpu < 1:
      raise SpecError(f"kind {self.name}: per_gpu must be >= 1")
    if self.per_gpu > 1 and self.pairing != "homogeneous":
      raise SpecError(
          f"kind {self.name}: per_gpu>1 requires pairing: homogeneous. "
          "Heterogeneous co-residency is not supported: the pair barrier is "
          "the substitute for a VRAM-compatibility model, and it only holds "
          "when both members are the same kind.")


@dataclass
class Run:
  stage: str
  kind: str
  target: str
  run_id: str
  cmd: str
  vars: dict[str, Any]
  inputs: list[str] = field(default_factory=list)
  done_when: list[Predicate] = field(default_factory=list)
  # Where this run's done_when paths resolve. Defaults to ${run_id}, because
  # training runs are self-contained directories -- but evaluation stages often
  # write per-run FILES into one shared folder ($RUNROOT/probe/run_s0.json),
  # and externally-trained models land wherever their trainer puts them. A stage
  # declares its own layout rather than being forced into one.
  logdir: str = ""
  # What to pull and bundle. Defaults to [logdir]; a stage whose outputs are
  # loose files names them explicitly so a shared folder is not swept whole.
  outputs: list[str] = field(default_factory=list)
  # slurm stages submit an sbatch script with --export instead of running a
  # shell command in a queue lane. Resolved per run so every ${var} is already
  # substituted by the time the submit script is written.
  sbatch: dict = field(default_factory=dict)


@dataclass
class Stage:
  name: str
  kind: str
  target: str
  on_failure: str
  run_id_tmpl: str
  cmd_tmpl: str
  logdir_tmpl: str = "${run_id}"
  outputs_tmpl: list[str] = field(default_factory=list)
  sbatch_tmpl: dict = field(default_factory=dict)
  needs: str | None = None
  expand: dict[str, list] = field(default_factory=dict)
  explicit_runs: list[dict] | None = None
  inputs_tmpl: list[str] = field(default_factory=list)
  done_when_tmpl: list[dict] = field(default_factory=list)
  raw: bool = False

  def expanded(self) -> list[Run]:
    where = f"stage {self.name}"
    combos: list[dict[str, Any]] = []
    if self.explicit_runs is not None:
      combos = [dict(r) for r in self.explicit_runs]
    elif self.expand:
      # Declaration order matters: it fixes the run ordering, which in turn
      # fixes lane packing. Sorting keys here would silently reshuffle waves.
      keys = list(self.expand)
      for values in itertools.product(*(self.expand[k] for k in keys)):
        combos.append(dict(zip(keys, values)))
    else:
      combos = [{}]

    runs = []
    for c in combos:
      # An explicit run may carry its own run_id/cmd; that is the raw escape
      # hatch for waves the template shape does not fit.
      rid = c.pop("run_id", None) or substitute(self.run_id_tmpl, c, where)
      if VAR_RE.search(rid):
        raise SpecError(f"{where}: run_id still has unexpanded variables: {rid}")
      scope = dict(c, run_id=rid)
      cmd = c.pop("cmd", None) or substitute(self.cmd_tmpl, scope, where)
      sb = _subst_pred(self.sbatch_tmpl, scope, where) if self.sbatch_tmpl else {}
      logdir = substitute(self.logdir_tmpl, scope, where)
      outputs = [substitute(o, scope, where) for o in self.outputs_tmpl] or [logdir]
      runs.append(Run(
          stage=self.name, kind=self.kind, target=self.target, run_id=rid,
          cmd=cmd, vars=scope, logdir=logdir, outputs=outputs, sbatch=sb,
          inputs=[substitute(i, scope, where) for i in self.inputs_tmpl],
          done_when=[_parse_predicate(_subst_pred(p, scope, where), where)
                     for p in self.done_when_tmpl],
      ))
    ids = [r.run_id for r in runs]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
      raise SpecError(f"{where}: duplicate run_ids: {sorted(dupes)}")
    return runs


def _subst_pred(raw: dict, scope: dict, where: str) -> dict:
  def walk(v):
    if isinstance(v, str):
      return substitute(v, scope, where)
    if isinstance(v, dict):
      return {k: walk(x) for k, x in v.items()}
    if isinstance(v, list):
      return [walk(x) for x in v]
    return v
  return walk(raw)


@dataclass
class Bundle:
  prefix: str
  include_stages: list[str]
  cloud_logs: list[str] = field(default_factory=list)


@dataclass
class WaveSpec:
  wave_id: str
  path: Path
  stages: list[Stage]
  kinds: dict[str, Kind]
  bundle: Bundle | None = None
  notes: str | None = None
  night: dict = field(default_factory=dict)

  def stage(self, name: str) -> Stage:
    for s in self.stages:
      if s.name == name:
        return s
    raise SpecError(f"no such stage: {name}")

  def runs(self, stages: list[str] | None = None) -> list[Run]:
    out = []
    for s in self.stages:
      if stages and s.name not in stages:
        continue
      out.extend(s.expanded())
    return out


def load(path: str | Path) -> WaveSpec:
  path = Path(path)
  if path.is_dir():
    for name in ("spec.yaml", "spec.yml", "spec.json"):
      if (path / name).exists():
        path = path / name
        break
    else:
      raise SpecError(f"no spec.yaml / spec.json in {path}")
  if not path.exists():
    raise SpecError(f"no spec at {path}")
  with open(path) as fh:
    if path.suffix == ".json":
      doc = json.load(fh)
    else:
      if yaml is None:
        raise SpecError(f"{path} is YAML but PyYAML is not installed "
                        "(pip install pyyaml, or write the spec as spec.json)")
      doc = yaml.safe_load(fh)
  if not isinstance(doc, dict):
    raise SpecError(f"{path}: top level must be a mapping")

  for req in ("wave_id", "stages"):
    if req not in doc:
      raise SpecError(f"{path}: missing required key {req!r}")

  kinds = {}
  for name, k in (doc.get("kinds") or {}).items():
    k = k or {}
    kind = Kind(name=name, per_gpu=int(k.get("per_gpu", 1)),
                pairing=k.get("pairing", "homogeneous"))
    kind.validate()
    kinds[name] = kind

  defaults = doc.get("defaults") or {}
  stages = []
  for i, s in enumerate(doc["stages"]):
    where = f"{path}: stages[{i}]"
    if "name" not in s:
      raise SpecError(f"{where}: missing name")
    name = s["name"]
    raw = s.get("kind") == "raw" or bool(s.get("raw"))
    kind = s.get("kind", defaults.get("kind", "default"))
    if kind != "raw" and kind not in kinds:
      kinds[kind] = Kind(name=kind)  # per_gpu 1; declaring kinds is optional
    target = s.get("target", defaults.get("target", "lanes"))
    if target not in TARGETS:
      raise SpecError(f"{where}: target must be one of {TARGETS}, got {target!r}")
    on_failure = s.get("on_failure", defaults.get("on_failure", "abort"))
    if on_failure not in ON_FAILURE:
      raise SpecError(f"{where}: on_failure must be one of {ON_FAILURE}")
    explicit = s.get("runs")
    if explicit is None and not s.get("run_id"):
      raise SpecError(f"{where}: needs either run_id (template) or runs (explicit list)")
    if explicit is None and not s.get("cmd") and not s.get("sbatch"):
      raise SpecError(f"{where}: needs cmd (or an sbatch block for slurm)")
    if s.get("sbatch"):
      if target != "slurm":
        raise SpecError(f"{where}: an sbatch block only applies to target: slurm")
      if "script" not in s["sbatch"]:
        raise SpecError(f"{where}: sbatch needs a script path")
    if target == "slurm" and not s.get("sbatch"):
      raise SpecError(
          f"{where}: target slurm needs an sbatch block. Slurm stages are "
          "submitted, not queued -- without one the stage would be expanded, "
          "checked and bundled but never actually run.")
    stages.append(Stage(
        name=name, kind=kind, target=target, on_failure=on_failure,
        run_id_tmpl=s.get("run_id", ""), cmd_tmpl=s.get("cmd", ""),
        logdir_tmpl=s.get("logdir", "${run_id}"),
        outputs_tmpl=s.get("outputs") or [],
        sbatch_tmpl=s.get("sbatch") or {},
        needs=s.get("needs"), expand=s.get("expand") or {},
        explicit_runs=explicit, inputs_tmpl=s.get("inputs") or [],
        done_when_tmpl=s.get("done_when") or [], raw=raw,
    ))

  names = [s.name for s in stages]
  if len(set(names)) != len(names):
    raise SpecError(f"{path}: duplicate stage names in {names}")
  for s in stages:
    if s.needs and s.needs not in names:
      raise SpecError(f"stage {s.name}: needs unknown stage {s.needs!r}")
    if s.needs and names.index(s.needs) >= names.index(s.name):
      raise SpecError(f"stage {s.name}: needs {s.needs!r}, which is not declared earlier")

  b = doc.get("bundle")
  bundle = None
  if b:
    if "prefix" not in b:
      raise SpecError(f"{path}: bundle needs a prefix")
    inc = b.get("include_stages") or names
    for st in inc:
      if st not in names:
        raise SpecError(f"{path}: bundle.include_stages names unknown stage {st!r}")
    bundle = Bundle(prefix=b["prefix"], include_stages=inc,
                    cloud_logs=b.get("cloud_logs") or [])

  spec = WaveSpec(
      wave_id=doc["wave_id"], path=path, stages=stages, kinds=kinds,
      bundle=bundle,
      notes=doc.get("notes"), night=doc.get("night") or {},
  )
  spec.runs()  # fail fast on template errors at load time
  return spec


# --------------------------------------------------------------------------
# lane packing (balanced-mix round robin; LPT over measured durations is
# packing.py)
# --------------------------------------------------------------------------

def balanced_order(runs: list[Run]) -> list[Run]:
  """Interleave runs so adjacent entries come from different classes.

  Plain round-robin over an expansion that varies its slowest dimension last
  hands one lane every slow cell -- the imbalance the ops doc warns about.
  Dealing one run from each class in rotation removes it without needing
  durations.

  KNOWN TRAP: the class key is the FIRST declared expand variable.  If that
  variable is not the one that varies runtime (or GPU affinity), the
  interleave does nothing useful -- one wave once mapped its first variable
  straight onto GPUs.  Put the slow dimension first, or check the lane files.
  """
  classes: dict[Any, list[Run]] = {}
  for r in runs:
    key = (r.stage, r.kind, next(iter(r.vars.values())) if r.vars else None)
    classes.setdefault(key, []).append(r)
  out, buckets = [], list(classes.values())
  for tier in itertools.zip_longest(*buckets):
    out.extend(r for r in tier if r is not None)
  return out


def pair_runs(runs: list[Run], per_gpu: int) -> list[list[Run]]:
  """Group into co-resident units. Pairs are homogeneous by construction."""
  if per_gpu <= 1:
    return [[r] for r in runs]
  groups: dict[tuple, list[Run]] = {}
  for r in runs:
    groups.setdefault((r.stage, r.kind), []).append(r)
  units = []
  for members in groups.values():
    for i in range(0, len(members), per_gpu):
      units.append(members[i:i + per_gpu])
  return units


def assign_lanes(units: list[list[Run]], lanes: list[str]) -> dict[str, list[list[Run]]]:
  out: dict[str, list[list[Run]]] = {ln: [] for ln in lanes}
  for i, u in enumerate(units):
    out[lanes[i % len(lanes)]].append(u)
  return out
