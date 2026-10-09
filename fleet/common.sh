# ---------------------------------------------------------------------------
# fleet shared library (controller side): config, box inventory, ssh helpers.
#
# The canonical command shapes, once, in code.  The ssh helpers exist
# specifically to contain one hazard: ssh joins its arguments into ONE string
# and hands it to a remote login shell that re-parses it, so every argument
# containing a space silently splits and shifts every later positional.  That
# once queued 2 commands instead of 8 with no error anywhere.  fl_ssh_script
# pre-quotes with printf %q so it cannot recur.
# ---------------------------------------------------------------------------

FLEET_HOME="${FLEET_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
Q_ROOT="${Q_ROOT:-$FLEET_HOME}"
# One config file for the queue and the fleet tools.  Shell-sourceable.
FLEET_CONFIG="${Q_CONFIG:-$Q_ROOT/config.env}"
[ -f "$FLEET_CONFIG" ] || FLEET_CONFIG="$FLEET_HOME/config.env"
# shellcheck disable=SC1090
[ -f "$FLEET_CONFIG" ] && { set -a; . "$FLEET_CONFIG"; set +a; }   # exported: child tools read it too
fl_abspath () { case "$1" in /*) printf '%s' "$1" ;; "~"*) printf '%s' "${1/#\~/$HOME}" ;; *) printf '%s/%s' "$Q_ROOT" "$1" ;; esac; }
FLEET_STATE="$(fl_abspath "${FLEET_STATE:-fleet_state}")"
FLEET_WAVES="$(fl_abspath "${FLEET_WAVES:-waves}")"
: "${FLEET_REMOTE_REPO:=/workspace/repo}"
: "${FLEET_REMOTE_RUNROOT:=/workspace/runs}"
: "${FLEET_BOX_HOME:=/root/fleet}"
# Local runroot used by wave checks / pulls on the controller.
: "${RUNROOT:=${FLEET_RUNROOT:-$Q_ROOT/runs}}"
export FLEET_HOME Q_ROOT FLEET_STATE FLEET_WAVES RUNROOT

FL_SSH_OPTS=(-o ServerAliveInterval=30 -o ServerAliveCountMax=120
              -o StrictHostKeyChecking=accept-new)

# Two option sets, because code and payload need opposite things.
#
# PAYLOAD (checkpoints, replay, results): --append-verify is right. Transfers
# are huge and resumable, and the files are immutable once written.
# --copy-unsafe-links: a symlink pointing OUTSIDE the synced tree is
# meaningless on the destination, so copy what it points at instead of the
# link.  A trainer that wrote its checkpoint under ~/.cache and linked to it
# from the run dir once had every pull re-plant dangling links on the archive
# host -- and a routine re-pull did that ON TOP OF the dereferenced 3.46 GB
# archive, silently destroying it (16/16 runs -> 0/16).  Copying the referent
# is the only behaviour that makes an archive an archive.
FL_RSYNC_OPTS=(-az --partial --append-verify --copy-unsafe-links --info=progress2 --stats)
#
# ...with ONE exception, and it is not hypothetical. --append-verify skips any
# file whose size already matches, so a MUTABLE, CONSTANT-LENGTH file never
# updates after its first copy.  `ckpt/latest` is exactly that: rewritten on
# every save, always a fixed-length timestamp.  Ten pulled runs once still
# pointed at their FIRST-pulled checkpoint (~105k-133k steps while the
# 494k-499k ones sat beside them), and a downstream probe evaluated the wrong
# model and reported OK.  The generated pull_results.sh pass 2 (checksum-forced,
# --ignore-times) is where those pointers are corrected.
#
# CODE: --append-verify is CATASTROPHIC here. It only ever appends to a file
# that is SHORTER on the destination and skips it outright when the size
# matches -- so an edited file of unchanged length never propagates.  That is
# why a VERSION stamp once kept reporting the previous night's commit after a
# push: the stamp is the same length every time, so rsync skipped it, and every
# same-size source edit was skipped with it. --checksum costs a hash
# of a small tree and removes the whole guessing game.
FL_CODE_RSYNC_OPTS=(-az --checksum --info=progress2 --stats)

die () { echo "ERROR: $*" >&2; exit 1; }
note () { echo "[fleet] $*"; }

# --- box inventory ------------------------------------------------------------
# `fleet refresh` writes $FLEET_STATE/instances.env (IP<N>/PORT<N>).  Falls back
# to IP<N>/PORT<N> already exported in the shell.

fl_load_instances () {
  if [ -r "$FLEET_STATE/instances.env" ]; then
    # shellcheck disable=SC1091
    source "$FLEET_STATE/instances.env"
    return 0
  fi
  if [ -z "${FL_QUIET_STATE:-}" ] && [ -z "${IP1:-}" ]; then
    echo "note: no box inventory at $FLEET_STATE/instances.env" >&2
    if [ -e "$FLEET_STATE" ] && [ ! -d "$FLEET_STATE" ]; then
      echo "      ($FLEET_STATE exists but is NOT a directory -- an scp into a" >&2
      echo "       missing path can create a file with that name; remove it)" >&2
    fi
    echo "      fix: bin/fleet refresh   (or export IP<N>/PORT<N> in this shell)" >&2
  fi
  return 0
}

fl_ip ()   { local n="${1:?box}"; local v="IP${n}";   printf '%s' "${!v:-}"; }
fl_port () { local n="${1:?box}"; local v="PORT${n}"; printf '%s' "${!v:-}"; }

fl_require_instance () {
  local n="${1:?box}"
  fl_load_instances
  [ -n "$(fl_ip "$n")" ] && [ -n "$(fl_port "$n")" ] ||
    die "box $n unknown: set IP${n}/PORT${n}, or run 'fleet refresh'"
}

# --- ssh --------------------------------------------------------------------

# Run a command string on an instance. The string is passed as ONE argument to
# a login shell -- never build it by concatenating unquoted variables.
#
# `-n` is load-bearing, not tidiness. ssh reads stdin by default, so an ssh
# call inside a script that is ITSELF being fed on stdin -- the
# `ssh host 'bash -s' <<'EOF' ... ssh host 'mkdir -p ...' ... EOF` shape --
# swallows the remainder of the enclosing script. Every command after it
# silently never runs, with no error: exactly the "rsync after an ssh mkdir
# doesn't execute" symptom. `-n` redirects stdin from /dev/null so an inner
# ssh cannot eat its parent.
fl_ssh () {
  local n="${1:?instance}"; shift
  fl_require_instance "$n"
  ssh -n -p "$(fl_port "$n")" "${FL_SSH_OPTS[@]}" "root@$(fl_ip "$n")" "$@"
}

# Feed a heredoc/script to an instance with positional args that survive the
# remote re-parse. Usage:
#   fl_ssh_script 4 "$script_text" arg1 "arg with spaces" arg3
fl_ssh_script () {
  local n="${1:?instance}" script="${2:?script}"; shift 2
  local quoted=""
  [ $# -gt 0 ] && quoted="$(printf '%q ' "$@")"
  fl_require_instance "$n"
  printf '%s\n' "$script" |
    ssh -p "$(fl_port "$n")" "${FL_SSH_OPTS[@]}" "root@$(fl_ip "$n")" \
      "bash -s -- $quoted"
}

# Same, but sources the box env + lane helpers first: the common case.
fl_ssh_box () {
  local n="${1:?box}" script="${2:?script}"; shift 2
  fl_ssh_script "$n" "$(printf 'set -uo pipefail\nsource /root/.fleet_env\nsource "$FLEET_BOX_HOME/lanes.sh"\ncd "$REPO" 2>/dev/null || true\n%s\n' "$script")" "$@"
}

# --- repo sync --------------------------------------------------------------
# Canonical whole-repo shape. Do NOT replace with multi-source trailing-slash
# syncs: those copy package contents into the repo root and create top-level
# package dirs that shadow installed packages.

FL_REPO_EXCLUDES=(--exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc')

fl_stamp_version () {
  local repo="$1" sha dirty=""
  sha="$(git -C "$repo" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  git -C "$repo" diff --quiet 2>/dev/null || dirty="-dirty"
  printf '%s%s pushed=%s by=%s' "$sha" "$dirty" "$(date -Is)" "${USER:-unknown}"
}

# push-repo: the project code + the box-side fleet scripts, then (re)start the
# box watchdog.  Everything a lane command needs from the controller arrives
# through this one verb, so a box never runs stale code by accident.
fl_push_repo () {
  local n="${1:?box}" ver
  fl_require_instance "$n"
  local e="ssh -p $(fl_port "$n") ${FL_SSH_OPTS[*]}"
  local dst="root@$(fl_ip "$n")"
  if [ -n "${FLEET_PROJECT_REPO:-}" ]; then
    [ -d "$FLEET_PROJECT_REPO" ] || die "FLEET_PROJECT_REPO=$FLEET_PROJECT_REPO is not a directory"
    ver="$(fl_stamp_version "$FLEET_PROJECT_REPO")"
    note "push-repo -> box $n  ($FLEET_PROJECT_REPO @ $ver -> $FLEET_REMOTE_REPO)"
    fl_ssh "$n" "mkdir -p $(printf %q "$FLEET_REMOTE_REPO")"
    rsync "${FL_CODE_RSYNC_OPTS[@]}" --delete "${FL_REPO_EXCLUDES[@]}" ${FLEET_REPO_EXCLUDES:-} \
      -e "$e" "$FLEET_PROJECT_REPO/" "$dst:$FLEET_REMOTE_REPO/" || die "repo rsync failed"
  else
    ver="none (FLEET_PROJECT_REPO unset)"
    note "push-repo -> box $n  (fleet scripts only; FLEET_PROJECT_REPO unset)"
  fi
  fl_ssh "$n" "mkdir -p $(printf %q "$FLEET_BOX_HOME")"
  rsync "${FL_CODE_RSYNC_OPTS[@]}" -e "$e" \
    "$FLEET_HOME/box/lanes.sh" "$FLEET_HOME/box/watchdog.sh" \
    "$FLEET_HOME/fleet/failure_signatures.tsv" "$dst:$FLEET_BOX_HOME/" || die "box script rsync failed"

  # The box env file.  Rewritten on every push so a box that rebooted under an
  # old template cannot silently keep stale paths or a missing alert topic --
  # a watchdog that reports to nobody looks exactly like a healthy night.
  fl_ssh_script "$n" '
set -uo pipefail
cat > /root/.fleet_env <<ENV
export REPO=$1
export RUNROOT=$2
export FLEET_BOX_HOME=$3
export FL_INSTANCE_LABEL=$4
export FL_NTFY_TOPIC=$5
ENV
printf "%s\n" "$6" > "$3/VERSION"
mkdir -p "$2"
echo "  wrote /root/.fleet_env and $3/VERSION"
' "$FLEET_REMOTE_REPO" "$FLEET_REMOTE_RUNROOT" "$FLEET_BOX_HOME" "box${n}" "${FLEET_NTFY_TOPIC:-}" "$ver"

  fl_ssh_box "$n" '
    echo "helpers: $(fl_version)"
    # RESTART, not start: `start` sees the pidfile and leaves the old process
    # running the old code, so a push that fixes the watchdog would not take
    # effect until the box rebooted. Safe to bounce -- it only observes, holds
    # no queue state, and its dedupe markers live on disk.
    bash "$FLEET_BOX_HOME/watchdog.sh" stop >/dev/null 2>&1 || true
    bash "$FLEET_BOX_HOME/watchdog.sh" start || echo "WARN: watchdog did not start"
    bash "$FLEET_BOX_HOME/watchdog.sh" status
  '
}
