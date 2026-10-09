#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Open (or reuse) one shared SSH connection to a cluster login node.
#
#   fleet cluster-connect            # open if needed, else report the live one
#   fleet cluster-connect --status   # report only, never authenticate
#   fleet cluster-connect --close    # drop the shared connection
#
# Run this once when you sit down.  Everything afterwards that talks to the
# cluster -- the `cluster` relay mode, destroy's archive check, inventory
# pushes -- rides the same socket and never asks for a password or a 2FA code
# again until it expires.
#
# Why a master socket rather than an agent or a key: clusters that require
# interactive 2FA have no unattended key to install.  ControlMaster pays that
# cost ONCE and multiplexes every later session over the same TCP connection.
#
# Idempotent on purpose: running it twice must not open a second connection or
# re-prompt, because "did I already start it?" is exactly the question you have
# when you sit down.
# ---------------------------------------------------------------------------
set -uo pipefail

CLUSTER_HOST="${FLEET_CLUSTER_HOST:-}"
PERSIST="${FLEET_CLUSTER_PERSIST:-24h}"
CTL_DIR="$HOME/.ssh/cm"
CTL="$CTL_DIR/%r@%h:%p"

MODE=open
while [ $# -gt 0 ]; do
  case "$1" in
    --status) MODE=status; shift ;;
    --close)  MODE=close;  shift ;;
    --host)   CLUSTER_HOST="${2:?--host needs a value}"; shift 2 ;;
    *) echo "usage: fleet cluster-connect [--status|--close] [--host user@host]" >&2
       exit 2 ;;
  esac
done
[ -n "$CLUSTER_HOST" ] || { echo "cluster: set FLEET_CLUSTER_HOST (user@login-node) or pass --host" >&2; exit 2; }

mkdir -p "$CTL_DIR"
chmod 700 "$CTL_DIR" 2>/dev/null || true

alive () { ssh -O check -o "ControlPath=$CTL" "$CLUSTER_HOST" >/dev/null 2>&1; }

# A dead master routinely leaves its socket FILE behind -- WSL sleeping, the
# network changing, a server-side timeout, or the master being SIGKILLed. The
# process may even linger while its connection is gone. `ssh -O check` then
# fails (Connection refused) while the file is still on disk, and ssh REFUSES
# to create a new master over an existing socket path: it prints
#   ControlSocket ... already exists, disabling multiplexing
# then connects WITHOUT a master, backgrounds via -f, and exits 0. The caller
# sees rc=0 and no connection, which reads like a credentials problem and is
# not one. So clear the corpse before trying to open.
sock_path () {
  local u h
  case "$CLUSTER_HOST" in
    *@*) u="${CLUSTER_HOST%%@*}"; h="${CLUSTER_HOST#*@}" ;;
    *)   u="${USER:-$(id -un)}"; h="$CLUSTER_HOST" ;;
  esac
  ls -1 "$CTL_DIR/$u@$h:"* 2>/dev/null   # glob the port: non-22 sockets count
}

reap_stale () {
  local s
  while IFS= read -r s; do
    [ -n "$s" ] || continue
    echo "cluster: stale control socket ($s) -- removing"
    rm -f "$s"
  done < <(sock_path)
  # A master whose socket is dead holds nothing worth keeping.
  if pkill -f "ssh -MNf.*ControlPersist.*$CLUSTER_HOST" 2>/dev/null; then
    echo "cluster: reaped a defunct master process"
    sleep 1
  fi
  return 0
}

case "$MODE" in
  status)
    if alive; then echo "cluster: CONNECTED  $CLUSTER_HOST"; exit 0
    else echo "cluster: not connected ($CLUSTER_HOST)"; exit 1; fi
    ;;
  close)
    if alive; then
      ssh -O exit -o "ControlPath=$CTL" "$CLUSTER_HOST" >/dev/null 2>&1
      echo "cluster: closed $CLUSTER_HOST"
    elif [ -n "$(sock_path)" ]; then
      # Not alive, but a socket file remains -- this is exactly the state that
      # makes a later `cluster-connect` fail with rc=0, so --close must clear it
      # rather than report "nothing to close".
      reap_stale
      echo "cluster: cleared a stale socket (there was no live connection)"
    else
      echo "cluster: nothing to close"
    fi
    exit 0
    ;;
esac

if alive; then
  echo "cluster: already connected to $CLUSTER_HOST -- nothing to do"
  exit 0
fi

reap_stale   # otherwise ssh declines to multiplex and exits 0
echo "cluster: opening a shared connection to $CLUSTER_HOST (persists ${PERSIST})"
echo "     you will be asked for your password + 2FA once"
# -M master, -N no command, -f background AFTER authenticating (so the prompt
# still reaches your terminal). No BatchMode: 2FA is interactive by nature.
ssh -MNf \
    -o ControlMaster=yes \
    -o "ControlPath=$CTL" \
    -o "ControlPersist=$PERSIST" \
    -o ServerAliveInterval=60 \
    -o ServerAliveCountMax=10 \
    "$CLUSTER_HOST"
rc=$?

if [ "$rc" -ne 0 ] || ! alive; then
  echo "cluster: FAILED to open a shared connection (ssh rc=$rc)" >&2
  echo "     try plain 'ssh $CLUSTER_HOST' first -- if that works, the problem is" >&2
  echo "     the socket path, not your credentials: $CTL_DIR" >&2
  if [ "$rc" -eq 0 ]; then
    echo "     rc=0 with no connection means ssh declined to multiplex over an" >&2
    echo "     existing socket file. Clear it with: fleet cluster-connect --close" >&2
  fi
  exit 1
fi

echo "cluster: CONNECTED. Verifying it actually carries commands..."
# A socket that exists is not a socket that works. Prove it end to end.
who="$(ssh -o BatchMode=yes -o ControlMaster=no -o "ControlPath=$CTL" \
        "$CLUSTER_HOST" 'hostname' 2>/dev/null | tail -1)"
if [ -n "$who" ]; then
  echo "cluster: verified -> $who   (expires after ${PERSIST} idle)"
else
  echo "cluster: socket opened but a test command returned nothing" >&2
  exit 1
fi
