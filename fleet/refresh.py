#!/usr/bin/env python3
"""Box inventory from the Vast CLI.

Replaces a hand-edited IP/PORT list, which had already drifted (it listed two
boxes while seven were live).

STICKY INDICES. Your index 4 must stay index 4 across refreshes, because it is
what you type and what alert titles say. Vast instance ids are the stable key,
so the mapping id -> index is persisted and only ever extended; a destroyed
instance frees nothing and a new one takes the next free number. Renumbering
on every refresh would be worse than editing by hand.

Read-only by design. This never creates or destroys anything: picking hardware
is a price judgement, and destroying loses whatever has not been pulled.

Runs on the controller so the API key never lands on a shared cluster; the
derived env file can be pushed to another host with --push-to.

Usage:
  refresh.py [--state $FLEET_STATE] [--push-to HOST:PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleetpaths  # noqa: E402

# Host and port must be chosen as a PAIR. Vast offers two distinct routes in
# and their fields do NOT interleave:
#
#   direct : public_ipaddr  +  ports["22/tcp"][].HostPort   e.g. 1.2.3.4:13124
#   proxy  : ssh_host       +  ssh_port                     e.g. ssh3.vast.ai:17836
#
# Pairing public_ipaddr with ssh_port produces a syntactically fine address
# that nothing is listening on. It would present as a network fault rather than
# a config bug, and it is the exact mistake this file made on 2026-08-14 before
# the full JSON was available -- ssh_port and public_ipaddr had been reported
# together and looked like a pair.
#
# Direct is what this fleet uses.

ID_KEYS = ("id", "instance_id", "machine_id")
GPU_KEYS = ("gpu_name", "gpu_display_name")
NGPU_KEYS = ("num_gpus", "gpus")


def pick(d: dict, keys) -> object | None:
  for k in keys:
    v = d.get(k)
    if v not in (None, "", 0):
      return v
  return None


def direct_ssh_port(d: dict) -> int | None:
  """The host-side port mapped to container port 22, from the `ports` block."""
  mapping = d.get("ports") or {}
  entries = mapping.get("22/tcp") or []
  for e in entries:
    hp = (e or {}).get("HostPort")
    if hp:
      try:
        return int(hp)
      except (TypeError, ValueError):
        continue
  return None


def address(d: dict) -> tuple[str, int, str] | None:
  """(host, port, how), DIRECT ROUTE ONLY, or None.

  There is deliberately no proxy fallback. This fleet only ever uses the direct
  route, and the fallback was actively harmful: an instance that is still
  `loading` has not published ports["22/tcp"] yet, so the fallback wrote a
  ssh_host proxy address into instances.env that nothing here can reach. Every
  ssh then failed in a way that looks like a broken HOST rather than a stale
  address (a healthy box was once nearly written off as dead on that evidence).

  Returning None instead makes a not-yet-addressable instance VISIBLY skipped,
  which is the truthful state: it has no usable address yet, so re-run refresh
  once it reaches `running`.
  """
  ip = d.get("public_ipaddr")
  dp = direct_ssh_port(d)
  if ip and dp:
    return str(ip), dp, "direct(public_ipaddr+ports.22/tcp)"
  return None


def fetch(cmd: list[str]) -> list[dict]:
  if not shutil.which(cmd[0]):
    sys.exit(f"ERROR: '{cmd[0]}' not found.\n"
             "  pip install vastai   (then: vastai set api-key <KEY>)\n"
             "  Install it on the controller, not on a shared cluster: the key "
             "should not sit there.")
  res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
  if res.returncode != 0:
    sys.exit(f"ERROR: {' '.join(cmd)} failed:\n{res.stderr.strip()}")
  try:
    data = json.loads(res.stdout)
  except json.JSONDecodeError:
    sys.exit(f"ERROR: could not parse JSON from {' '.join(cmd)}.\n"
             f"First 300 chars:\n{res.stdout[:300]}")
  if isinstance(data, dict):
    data = data.get("instances", [data])
  return [d for d in data if isinstance(d, dict)]


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--state", default=str(fleetpaths.STATE))
  ap.add_argument("--from-json", default=None,
                  help="read `vastai show instances --raw` output from a file "
                       "instead of calling the CLI (useful for a dry check)")
  ap.add_argument("--push-to", default=os.environ.get("FLEET_PUSH_TARGET", ""),
                  help="scp the inventory to e.g. user@host:/path/to/fleet_state/")
  ap.add_argument("--verify", action="store_true",
                  help="ssh to each resolved address; catches a wrong host/port "
                       "pairing now instead of mid-wave")
  ap.add_argument("--forget", action="append", default=[], metavar="VAST_ID",
                  help="release a retired instance's index for reuse. Deliberate "
                       "by design: the number may still appear in your notes, "
                       "bundles and alert history.")
  ap.add_argument("--take-authority", action="store_true",
                  help="claim ownership of the inventory for THIS machine. Only "
                       "one machine may assign indices; the rest read the file.")
  ap.add_argument("--dry-run", action="store_true")
  args = ap.parse_args()

  state = Path(args.state)
  state.mkdir(parents=True, exist_ok=True)
  idx_file = state / "instances.json"

  raw = (json.loads(Path(args.from_json).read_text()) if args.from_json
         else fetch(["vastai", "show", "instances", "--raw"]))
  if isinstance(raw, dict):
    raw = raw.get("instances", [raw])

  # `retired` is what makes indices genuinely permanent. Without it an index is
  # only reserved until the NEXT refresh -- the destroyed instance drops out of
  # the file, its number frees up, and the following new rental inherits it.
  # A note saying "instance 1 ran the graft" would then point at a different
  # machine, silently. Retired ids are kept so their numbers stay claimed.
  prior, retired, prev_authority = {}, {}, None
  if idx_file.exists():
    try:
      doc0 = json.loads(idx_file.read_text())
      prior = {str(e["vast_id"]): e["index"] for e in doc0.get("instances", [])}
      retired = {str(k): int(v) for k, v in (doc0.get("retired") or {}).items()}
      prev_authority = doc0.get("authority_host")
    except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError):
      prior, retired = {}, {}

  # Single writer, enforced. The numbering only means anything if exactly one
  # machine assigns it; two writers produce two mappings that both look right,
  # and "instance 4" quietly denotes different hardware depending on where you
  # typed it. The file records who owns it and refuses a second owner.
  me = socket.gethostname()
  if prev_authority and prev_authority != me and not args.take_authority:
    print(f"REFUSING: this inventory is owned by '{prev_authority}', not '{me}'.",
          file=sys.stderr)
    print("  Running refresh here would create a second numbering.", file=sys.stderr)
    print(f"  Read-only use is fine -- other fleet verbs just source "
          f"instances.env.", file=sys.stderr)
    print("  To move ownership to this machine deliberately:", file=sys.stderr)
    print("    fleet refresh --take-authority", file=sys.stderr)
    return 2

  for vid in args.forget:
    if vid in retired:
      print(f"reclaimed index {retired.pop(vid)} from retired instance {vid}")
    elif vid in prior:
      print(f"REFUSED to forget {vid}: it is still rented and in use", file=sys.stderr)
      return 2
    else:
      print(f"note: {vid} was not in the retired list")

  entries, unparsed, pairings = [], [], set()
  for inst in raw:
    vid = pick(inst, ID_KEYS)
    addr = address(inst)
    if not (vid and addr):
      unparsed.append({
          "vast_id": str(vid) if vid else "?",
          "status": str(inst.get("actual_status") or inst.get("cur_state") or "?"),
          "keys": sorted(inst.keys())[:14],
      })
      continue
    host, port, how = addr
    pairings.add(how)
    entries.append({
        "vast_id": str(vid), "ip": host, "port": port, "addr_from": how,
        "gpu": str(pick(inst, GPU_KEYS) or "unknown"),
        "n_gpu": int(pick(inst, NGPU_KEYS) or 1),
        "status": str(inst.get("actual_status") or inst.get("cur_state") or "?"),
        # Real $/hr, straight from the listing. Lets the digest report money
        # burned rather than GPU-hours, which is the unit decisions are in.
        "cost_hr": round(float((inst.get("search") or {}).get("totalHour")
                               or inst.get("dph_total") or 0), 4),
    })

  if unparsed and not entries:
    print("ERROR: no instance has a direct address "
          "(public_ipaddr + ports['22/tcp']).", file=sys.stderr)
    for u in unparsed:
      print(f"  {u['vast_id']}  status={u['status']}", file=sys.stderr)
    print("If these are still 'loading', wait for 'running' and re-run refresh; "
          "the direct port is published then.", file=sys.stderr)
    print(f"Keys seen on the first instance: {unparsed[0]['keys']}",
          file=sys.stderr)
    return 2

  # A fleet appearing all at once with no prior state usually means this is a
  # SECOND machine, not a first rental -- and assigning fresh numbers here
  # creates two authorities that disagree. Warn loudly; the fix is one scp.
  if not prior and not retired and len(entries) > 1:
    print(f"WARNING: no index history here, but {len(entries)} instances are "
          "already running.", file=sys.stderr)
    print("  If you already run refresh on another machine, its numbering is "
          "the authority.", file=sys.stderr)
    print("  Copy it over instead of letting this host invent its own:",
          file=sys.stderr)
    print("    scp <primary>:.../fleet_state/instances.json fleet_state/",
          file=sys.stderr)
    print("  Continuing with fresh indices.\n", file=sys.stderr)

  # Sticky indices: keep what an id already had, then fill the lowest free slot
  # that no live OR retired instance holds.
  known = dict(retired); known.update(prior)
  used = set(known.values())
  nxt = 1
  for e in sorted(entries, key=lambda e: e["vast_id"]):
    if e["vast_id"] in known:
      e["index"] = known[e["vast_id"]]
      retired.pop(e["vast_id"], None)   # rented again: no longer retired
    else:
      while nxt in used:
        nxt += 1
      e["index"] = nxt
      used.add(nxt)
  entries.sort(key=lambda e: e["index"])

  # Anything previously live and now absent joins the retired set.
  live_ids = {e["vast_id"] for e in entries}
  for vid, idx in prior.items():
    if vid not in live_ids:
      retired[vid] = idx
  doc = {"authority_host": me,
         "instances": entries,
         "retired": dict(sorted(retired.items()))}
  env_lines = ["# GENERATED by fleet/refresh.py -- do not edit by hand.",
               "# Machine-local state, not a record: gitignored on purpose."]
  for e in entries:
    env_lines += [f"export IP{e['index']}={e['ip']}",
                  f"export PORT{e['index']}={e['port']}"]

  print(f"{'IDX':<5} {'VAST ID':<12} {'GPU':<16} {'N':<3} {'$/HR':<8} "
        f"{'STATUS':<10} ADDRESS")
  for e in entries:
    new = "" if e["vast_id"] in prior else "  (new)"
    print(f"{e['index']:<5} {e['vast_id']:<12} {e['gpu'][:16]:<16} "
          f"{e['n_gpu']:<3} {e['cost_hr']:<8.3f} {e['status'][:10]:<10} "
          f"{e['ip']}:{e['port']}{new}")
  tot = sum(e["cost_hr"] for e in entries)
  if tot:
    print(f"{'':<38} {tot:<8.3f} total, ${tot * 24:.2f}/day")
  if pairings:
    print(f"\naddress fields used: {', '.join(sorted(pairings))}")
  if unparsed:
    print(f"\n{len(unparsed)} instance(s) SKIPPED -- no direct address yet, so "
          "they are NOT in instances.env and cannot be reached:")
    for u in unparsed:
      print(f"  {u['vast_id']}  status={u['status']}")
    print("  A 'loading' instance publishes ports['22/tcp'] only once it is "
          "'running'. Re-run refresh then.")
  gone = sorted(set(prior) - {e["vast_id"] for e in entries})
  if gone:
    print(f"\n{len(gone)} instance(s) no longer rented; their indices stay "
          f"reserved so a new rental cannot inherit them:")
    for vid in gone:
      print(f"  index {prior[vid]} <- {vid}")
  if retired:
    print(f"\nretired indices held: "
          + ", ".join(f"{i}({v})" for v, i in sorted(retired.items(), key=lambda kv: kv[1])))
    print("  reclaim one only when no notes or bundles still refer to it:")
    print("    fleet refresh --forget <vast_id>")
  if unparsed:
    print(f"\nWARN: {len(unparsed)} entr(ies) had no usable ssh host/port and "
          "were skipped.")

  if args.verify:
    print("\nverifying reachability (5s timeout each)")
    for e in entries:
      r = subprocess.run(
          ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
           "-o", "StrictHostKeyChecking=accept-new",
           "-p", str(e["port"]), f"root@{e['ip']}", "true"],
          capture_output=True, text=True)
      e["reachable"] = (r.returncode == 0)
      print(f"  {e['index']:<3} {e['ip']}:{e['port']:<6} "
            f"{'ok' if e['reachable'] else 'UNREACHABLE  ' + r.stderr.strip()[:60]}")

  if args.dry_run:
    print("\n--dry-run: nothing written")
    return 0

  idx_file.write_text(json.dumps(doc, indent=1) + "\n")
  (state / "instances.env").write_text("\n".join(env_lines) + "\n")
  if prev_authority and prev_authority != me:
    print(f"\nauthority moved: {prev_authority} -> {me}")
  print(f"\nwrote {idx_file}  (authority: {me})")
  print(f"wrote {state / 'instances.env'}   (source this, or let fleet/qd load it)")

  if args.push_to:
    # Push the AUTHORITY (instances.json) as well as the derived env. Sending
    # only the env leaves the other machine free to run its own refresh and
    # invent a conflicting numbering -- which is worse than no numbering,
    # because both look right and "instance 4" quietly means two machines.
    tgt = args.push_to
    # scp into a path that does not exist fails (or, with one file, silently
    # creates a FILE with that name). fleet_state is gitignored, so on a fresh
    # second machine it never exists. Create it first.
    #
    # Ride the shared connection fleet/cluster_connect.sh opens. Without these
    # options ssh/scp authenticate from scratch, and on a 2FA host that means
    # an askpass prompt this non-interactive call cannot answer: the push dies
    # with "Too many authentication failures" and the other machine silently
    # keeps a STALE inventory. That is how a freshly rented box once stayed
    # invisible to the cluster while every local command saw it.
    ctl = os.path.expanduser("~/.ssh/cm/%r@%h:%p")
    share = ["-o", "ControlMaster=no", "-o", f"ControlPath={ctl}"]
    if ":" in tgt:
      rhost, rpath = tgt.split(":", 1)
      subprocess.run(["ssh", "-n", *share, rhost, "mkdir", "-p", rpath.rstrip("/")],
                     capture_output=True, text=True)
    r = subprocess.run(["scp", *share, str(state / "instances.json"),
                        str(state / "instances.env"), tgt],
                       capture_output=True, text=True)
    print(f"pushed instances.json + instances.env to {tgt}" if r.returncode == 0
          else f"WARN: push failed: {r.stderr.strip()}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
