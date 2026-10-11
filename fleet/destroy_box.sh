#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Destroy a rented Vast box -- but only once its data is provably somewhere
# else.
#
#   fleet destroy N                 # REPORT only. Never destroys.
#   fleet destroy N --yes           # destroy, after the archive gate + prompt
#   fleet destroy N --force-unsafe  # destroy WITH unarchived data (typed ack)
#
# `vastai destroy instance` is described by Vast itself as "irreversible,
# deletes data". This wrapper exists so that irreversibility is only ever
# reached through a check, not through a memory of having synced.
#
# The standing rule this enforces: nothing is deleted without a VERIFIED
# archive elsewhere. (A "delete-only" cleanup tier once destroyed trained
# models that later turned out to be needed, and GPU nondeterminism means they
# cannot be re-created bit-for-bit.) Destroying a box deletes every run dir on
# it, so the same rule applies here even though no `rm` is typed.
#
# What "archived" means here: every run dir under the box's $RUNROOT must exist
# under FLEET_ARCHIVE_ROOT (on FLEET_ARCHIVE_HOST, or locally when that is
# empty) with counters at least as large -- checkpoint step, score lines, file
# count and bytes -- and matching identity-file hashes. Greater-or-equal, not
# equal: the archive legitimately accumulates runs from several boxes, and a
# bundle may add files.
#
# Two confirmations are typed on stdin (pipe them for an unattended destroy:
# `printf '%s\n' <vast_id> | fleet destroy N --yes`).
#
# Creation is deliberately NOT here. Picking an offer is a price judgement and
# stays in the web UI; this tool only ever spends down, never up.
# ---------------------------------------------------------------------------
set -uo pipefail

FLEET_HOME="${FLEET_HOME:-$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)}"
source "$FLEET_HOME/fleet/common.sh"

ARCHIVE_HOST="${FLEET_ARCHIVE_HOST:-}"
ARCHIVE_ROOT="${FLEET_ARCHIVE_ROOT:-}"
CTL="$HOME/.ssh/cm/%r@%h:%p"
# -n is load-bearing: without it this ssh reads the SCRIPT's stdin, and the
# confirmation prompt further down then reads an already-empty stream and
# aborts with "got ''" -- or, worse on a terminal, silently eats the keystrokes
# meant for the prompt. Same hazard fl_ssh guards against; it is easy to
# reintroduce every time a new bare ssh is added.
ARCHIVE_SSH=(-n -o BatchMode=yes -o ControlMaster=no -o "ControlPath=$CTL")

N=""; DO_IT=0; FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --yes)          DO_IT=1; shift ;;
    --force-unsafe) DO_IT=1; FORCE=1; shift ;;
    -*) die "unknown option $1 (usage: fleet destroy N [--yes|--force-unsafe])" ;;
    *)  N="$1"; shift ;;
  esac
done
[ -n "$N" ] || die "usage: fleet destroy N [--yes|--force-unsafe]"
[ -n "$ARCHIVE_ROOT" ] || die "set FLEET_ARCHIVE_ROOT (where this box's runs are archived) in config.env"

fl_require_instance "$N"
command -v vastai >/dev/null 2>&1 || die "vastai CLI not found. Destroy runs on
the machine that owns the inventory (the controller)."

STATE="$FLEET_STATE/instances.json"
[ -f "$STATE" ] || die "no $STATE -- run 'fleet refresh' first"
VAST_ID="$(python3 -c "
import json,sys
d=json.load(open('$STATE'))
for i in d.get('instances',[]):
    if int(i.get('index',-1))==int('$N'): print(i.get('vast_id','')); break
")"
[ -n "$VAST_ID" ] || die "box $N has no vast_id in $STATE -- run 'fleet refresh'"

echo "=============================================================="
echo " DESTROY box $N   (vast id $VAST_ID, $(fl_ip "$N"):$(fl_port "$N"))"
echo "=============================================================="

# -- gate 1: nothing may be in flight --------------------------------------
echo
echo "== work in flight =="
busy="$(fl_ssh_box "$N" '
  n=0
  for q in "$RUNROOT"/_queue_control/lane_*/queue_*; do
    [ -d "$q" ] || continue
    pid="$(cat "$q/supervisor.pid" 2>/dev/null || echo)"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && n=$((n+1))
  done
  util=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader 2>/dev/null \
         | tr -d " %" | sort -rn | head -1)
  procs=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null \
          | grep -c . || true)
  mem=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader 2>/dev/null \
        | tr -d " MiB" | sort -rn | head -1)
  echo "queues=$n procs=${procs:-0} util=${util:-0} mem=${mem:-0}"')" ||
  die "could not check work in flight on box $N"
echo "  $busy"
q_live="$(printf '%s' "$busy" | sed -n 's/.*queues=\([0-9]*\).*/\1/p')"
p_live="$(printf '%s' "$busy" | sed -n 's/.*procs=\([0-9]*\).*/\1/p')"
u_live="$(printf '%s' "$busy" | sed -n 's/.*util=\([0-9]*\).*/\1/p')"
m_live="$(printf '%s' "$busy" | sed -n 's/.*mem=\([0-9]*\).*/\1/p')"
for count in "$q_live" "$p_live" "$u_live" "$m_live"; do
  [[ "$count" =~ ^[0-9]+$ ]] || die "invalid work-in-flight inventory from box $N"
done
in_flight=0
[ "${q_live:-0}" -gt 0 ] && in_flight=1
[ "${p_live:-0}" -gt 0 ] && in_flight=1
# Utilization ALONE is not evidence of work. Consumer cards leave the busy
# percentage latched at 100 for minutes after a job is killed -- one box
# reported GPUs 1 and 2 at 100% with 2 MiB allocated and zero compute
# processes, which blocked a destroy whose work was already
# cancelled and archived. A real training job holds GPU memory, so utilization
# only counts when memory is held too. Kept as a second path (rather than
# dropping util entirely) because some container images hide the compute-apps
# list, and there memory is the only witness left.
[ "${u_live:-0}" -ge 20 ] && [ "${m_live:-0}" -ge 512 ] && in_flight=1
if [ "$in_flight" -eq 1 ]; then
  echo "  WARNING: this box is still working (live supervisors or a busy GPU)."
  [ "$FORCE" -eq 1 ] || die "refusing to destroy a box with work in flight.
       Let it drain, or cancel its lanes deliberately first."
fi

# -- quiesce: freeze the logs before we compare anything --------------------
# The watchdog and util samplers keep appending to _cloud_logs, so the archive
# check below can never see a settled tree and a per-file verification never
# converges. Gate 1 has just established the box is idle, which is exactly the
# condition under which stopping the watchdog is safe. fl_quiesce refuses on
# its own if any lane is still active, so this cannot silence a working box.
echo
echo "== quiesce: stopping log writers so the tree settles =="
fl_ssh_box "$N" 'fl_quiesce' \
  || echo "  (quiesce unavailable or refused -- continuing; logs may lag)"

# -- gate 2: every run dir must exist in the archive with >= counters -------
echo
echo "== archive check: box vs ${ARCHIVE_HOST:-local}:$ARCHIVE_ROOT =="
INV="$FLEET_HOME/fleet/runroot_inventory.py"
[ -f "$INV" ] || die "missing $INV"

inst_json="$(fl_ssh_box "$N" "python3 - \"\$RUNROOT\" <<'PYEOF'
$(cat "$INV")
PYEOF" 2>/dev/null | tail -1)"
# '{'* is NOT enough: runroot_inventory.py emits {"error": ...} for a bad
# runroot, which starts with '{' and sails through. Require the payload we
# actually consume.
case "$inst_json" in *'"entries"'*) ;; *) die "could not inventory box $N (got: ${inst_json:0:160})" ;; esac

# Ask the archive only about the names THIS BOX holds. The question is "is
# what lives here also safe there?", so the archive's other ~2260 dirs are
# irrelevant -- and walking all of them cost 249 s per call on a 591 GB tree,
# paid two or three times per destroy. A name the box does not have cannot be
# data at risk, so this narrows the walk without weakening the gate: anything
# the box has that the archive lacks still reports MISSING.
INST_NAMES="$(printf '%s' "$inst_json" | python3 -c '
import json, shlex, sys
print(" ".join(shlex.quote(k) for k in sorted(json.load(sys.stdin)["entries"])))')"
# An EMPTY inventory is not the same as a FAILED one. The check above already
# asserted the box inventory returned valid JSON carrying an "entries"
# key, so zero entries means zero run dirs -- not a probe that silently died.
# Zero run dirs is trivially safe: nothing here can be the only copy of
# anything. It is also the NORMAL end state under continuous harvest (pull +
# verify + delete each round as it finishes), which otherwise leaves every
# fully-drained box undestroyable: the old die fired BEFORE the --force-unsafe
# check, so there was no way through at all.
#
# NOTE: the else-block below is deliberately NOT indented -- it carries two
# heredocs (PYEOF, PY) whose terminators must stay at column 0.
if [ -z "$INST_NAMES" ]; then
  echo "  box $N holds NO run dirs -- nothing at risk on this box."
  echo "  (skipping the box-vs-archive comparison: it has no subject)"
  report='{"n_inst":0,"covered":[],"missing":[],"short":[],"differ":[],"unverifiable":[],"boxes":[]}'
else
if [ -n "$ARCHIVE_HOST" ]; then
  arch_json="$(ssh "${ARCHIVE_SSH[@]}" "$ARCHIVE_HOST" "python3 - '$ARCHIVE_ROOT' --only $INST_NAMES <<'PYEOF'
$(cat "$INV")
PYEOF" 2>/dev/null | tail -1)"
else
  arch_json="$(eval "python3 \"\$INV\" \"\$ARCHIVE_ROOT\" --only $INST_NAMES" 2>/dev/null | tail -1)"
fi
case "$arch_json" in *'"entries"'*) ;; *) die "could not inventory the archive at ${ARCHIVE_HOST:-local}:$ARCHIVE_ROOT (got: ${arch_json:0:160}).
       Remote archive?  Is the shared connection up?  fleet cluster-connect" ;; esac

printf '%s' "$inst_json" > /tmp/fleet_inv_inst_$$.json
printf '%s' "$arch_json"  > /tmp/fleet_inv_arch_$$.json
trap 'rm -f /tmp/fleet_inv_inst_$$.json /tmp/fleet_inv_arch_$$.json' EXIT

report="$(python3 - /tmp/fleet_inv_inst_$$.json /tmp/fleet_inv_arch_$$.json <<'PY'
import json, sys
inst = json.load(open(sys.argv[1]))["entries"]
arch = json.load(open(sys.argv[2]))["entries"]
FIELDS = ("ckpt_step", "n_scores", "n_files", "bytes")
covered, missing, short, differ, unverifiable, boxes = [], [], [], [], [], []
for name, a in sorted(inst.items()):
    b = arch.get(name)
    if b is None:
        missing.append(name); continue
    # A container is a NAMESPACE, not a unit. Comparing one against a same-named
    # namespace on the other host is exactly how 16 live runs were once
    # deleted; the descent normally prevents it, and this refuses whatever the
    # descent could not flatten (deeper nesting, unreadable dirs).
    if a.get("is_container") or b.get("is_container"):
        boxes.append((name, "container -- compare its runs, not its name")); continue
    bad = [f for f in FIELDS if b.get(f, 0) < a.get(f, 0)]
    if bad:
        short.append((name, ", ".join(f"{f} {a.get(f,0)}>{b.get(f,0)}" for f in bad)))
        continue
    wa, wb = a.get("witness", {}) or {}, b.get("witness", {}) or {}
    if "ERROR" in wa.values() or "ERROR" in wb.values():
        differ.append((name, "identity file unreadable -- cannot verify")); continue
    shared = set(wa) & set(wb)
    if not wa or not wb or not shared:
        unverifiable.append((name, "no identity file to compare"))
    elif any(wa[k] != wb[k] for k in shared):
        k = next(k for k in sorted(shared) if wa[k] != wb[k])
        differ.append((name, f"{k} differs: box {wa[k][:12]} vs archive {wb[k][:12]}"))
    elif set(wa) - set(wb):
        # The archive is allowed to hold MORE, never less.
        differ.append((name, "archive missing identity file(s): "
                             + ",".join(sorted(set(wa) - set(wb)))))
    else:
        covered.append((name, ""))
print(json.dumps({
    "n_inst": len(inst),
    "covered": [c[0] for c in covered],
    "missing": missing,
    "short": short,
    "differ": differ,
    "unverifiable": unverifiable,
    "boxes": boxes,
}))
PY
)"
fi
python3 - "$report" <<'PY'
import json, sys
r = json.loads(sys.argv[1])
print(f"  {len(r['covered'])}/{r['n_inst']} dirs covered in the archive")
for n in r["missing"]:
    print(f"  NOT ARCHIVED   {n}")
for n, why in r["short"]:
    print(f"  SHORT          {n}   ({why})")
for n, why in r.get("differ", []):
    print(f"  DIFFERENT      {n}   ({why})")
for n, why in r.get("boxes", []):
    print(f"  CONTAINER      {n}   ({why})")
for n, why in r.get("unverifiable", []):
    print(f"  UNVERIFIABLE   {n}   ({why}; counters cover it)")
PY
unsafe="$(python3 -c "
import json,sys; r=json.loads(sys.argv[1]); print(len(r['missing'])+len(r['short'])+len(r.get('differ',[])))" "$report")"

if [ "$unsafe" -gt 0 ]; then
  echo
  echo "  $unsafe directory(ies) exist ONLY on this box."
  echo "  Destroying now deletes the only copy. Pull them first:"
  echo "      fleet pull --wave <wave> $N"
  if [ "$FORCE" -ne 1 ]; then
    die "refusing to destroy: unarchived data"
  fi
else
  ncov="$(python3 -c "import json,sys;r=json.loads(sys.argv[1]);print(len(r['covered']))" "$report" 2>/dev/null || echo 0)"
  ninst="$(python3 -c "import json,sys;r=json.loads(sys.argv[1]);print(r['n_inst'])" "$report" 2>/dev/null || echo -1)"
  if [ "$ncov" = "$ninst" ]; then
    echo "  ALL CLEAR: all $ncov run dir(s) on box $N are archived, counters and identity verified."
  else
    echo "  PROCEEDING: $ncov/$ninst verified; the rest were accepted by override."
  fi
fi

# -- report-only unless --yes ----------------------------------------------
if [ "$DO_IT" -ne 1 ]; then
  echo
  echo "REPORT ONLY -- nothing destroyed."
  echo "  to proceed:  fleet destroy $N --yes"
  exit 0
fi

# -- confirmation ----------------------------------------------------------
echo
if [ "$FORCE" -eq 1 ] && [ "$unsafe" -gt 0 ]; then
  echo "!! You are destroying UNARCHIVED data. This cannot be undone."
  read -r -p "Type DESTROY UNARCHIVED to continue: " ack
  [ "$ack" = "DESTROY UNARCHIVED" ] || die "aborted"
fi
read -r -p "Type the vast id ($VAST_ID) to destroy box $N: " ack
[ "$ack" = "$VAST_ID" ] || die "aborted (got '$ack')"

# -- record BEFORE destroying ----------------------------------------------
mkdir -p "$FLEET_STATE"
python3 - "$FLEET_STATE/destroyed.jsonl" "$N" "$VAST_ID" "$report" <<'PY'
import json, sys, datetime
path, idx, vid, report = sys.argv[1:5]
rec = {"ts": datetime.datetime.now().astimezone().isoformat(),
       "index": int(idx), "vast_id": vid, "archive": json.loads(report)}
with open(path, "a") as f:
    f.write(json.dumps(rec) + "\n")
print(f"  recorded -> {path}")
PY

echo "  vastai destroy instance $VAST_ID"
vastai destroy instance "$VAST_ID" -y || die "vastai destroy failed"
echo "  destroyed."

# Retire the index so it is never silently reused for a different machine.
echo
echo "== refreshing inventory (retires index $N) =="
python3 "$FLEET_HOME/fleet/refresh.py" || true
