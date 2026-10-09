"""Where the fleet tools keep their state.  Stdlib only.

bin/fleet exports FLEET_STATE / FLEET_WAVES as absolute paths (from
config.env, relative entries resolved against Q_ROOT).  When a tool is run
directly the same defaults apply: <Q_ROOT>/fleet_state and <Q_ROOT>/waves,
with Q_ROOT defaulting to the checkout.
"""
from __future__ import annotations

import os
from pathlib import Path

FLEET_HOME = Path(__file__).resolve().parent.parent
Q_ROOT = Path(os.environ.get("Q_ROOT") or FLEET_HOME)


def _resolve(value: str) -> Path:
  p = Path(os.path.expanduser(value))
  return p if p.is_absolute() else Q_ROOT / p


STATE = _resolve(os.environ.get("FLEET_STATE") or "fleet_state")
WAVES = _resolve(os.environ.get("FLEET_WAVES") or "waves")


def wave_dir(wave: str) -> Path:
  """A wave id (looked up under WAVES) or a path to a wave dir/spec file."""
  p = Path(wave)
  if p.exists():
    return p
  return WAVES / wave
