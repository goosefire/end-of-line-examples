#!/bin/bash
# Push this repo's speak.py to citizens and restart them, without forfeiting a match.
#
#   ./deploy-speak.sh <slot> [<slot> ...]
#   ./deploy-speak.sh --all
#
# For a CODE change only. A change that also needs the journal started over is
# reset-memory-epoch.sh, which stops the service rather than restarting it.
#
# Each citizen: wait for a gap, keep the build it had, push, prove the new one
# imports IN THE VM, restart, prove it came up. Any failure puts the old build back
# and starts the citizen again — a verified import is not a verified run, and a
# citizen left crash-looping under Restart=always is worse than one not upgraded.
set -euo pipefail

# THE ROSTER IS ASKED FOR, NOT KEPT HERE. It used to be a literal, and when the
# chess and checkers pairs joined in August it went stale in silence: --all upgraded
# twelve of sixteen and still printed ALL DEPLOYED. A name missing from a list looks
# exactly like a name that was never supposed to be in it, so nothing could report
# the four citizens left behind on the old build.
#
# Membership is a question for the citizen rather than a second list to maintain.
# A file test would not settle it -- the legacy pinned bots all carry a speak.py on
# disk and would pass one -- but their citizen.service EXECUTES their own player, so
# asking what the service runs excludes them, and excludes the next one added too.
resolve_all() {
  local vm slot ex out=""
  for vm in $(lxc list --format csv -c n,s | awk -F, '$2 == "RUNNING" { print $1 }'); do
    # MATCH the prefix, never merely strip it. run_code's throwaway eol-exec-* sandboxes
    # come and go in this same list, and `sed s/^citizen-vm-//` passes them through
    # untouched -- into an `lxc info citizen-vm-eol-exec-...` that fails and aborts the
    # run halfway through the fleet, on whichever deploys happen to overlap a sandbox.
    case "$vm" in
      citizen-vm-base) continue ;;   # the image the citizens are cut from, never one of them
      citizen-vm-*)    slot="${vm#citizen-vm-}" ;;
      *)               continue ;;
    esac
    ex="$(lxc exec "$vm" -- systemctl show -p ExecStart --value citizen.service 2>/dev/null || true)"
    # An absent or unreadable unit answers EMPTY and exits 0, so silence here is not a
    # "no" -- it is a citizen we failed to read, and quietly dropping it is precisely
    # the bug this replaced. Refuse the whole run instead of shipping a short roster.
    [ -n "$ex" ] || { echo "!! cannot read citizen.service on ${vm}" >&2; return 1; }
    case "$ex" in
      *speak.py*) out="${out} ${slot}" ;;
      *)          echo "    skipping ${slot}: its service runs its own player, not speak.py" >&2 ;;
    esac
  done
  echo "${out# }"
}

[ $# -gt 0 ] || { echo "usage: $0 <slot> [<slot> ...] | --all" >&2; exit 2; }
if [ "${1:-}" = "--all" ]; then
  echo "=== resolving the roster from the running VMs"
  ALL="$(resolve_all)" || exit 1
  [ -n "$ALL" ] || { echo "!! no running citizen runs speak.py" >&2; exit 1; }
  set -- $ALL
  # Printed BEFORE anything is pushed, and counted, so a citizen missing from the
  # house is something you can see here rather than infer from a later silence.
  echo "=== roster ($# citizens): $*"
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
python3 -m py_compile "$SRC/speak.py" || { echo "!! speak.py does not compile"; exit 1; }
echo "=== deploying speak.py to: $*"

for SLOT in "$@"; do
  VM="citizen-vm-${SLOT}"
  lxc info "$VM" >/dev/null 2>&1 || { echo "!! ${VM} does not exist"; exit 1; }
  "$SRC/wait-for-gap.sh" "$SLOT" || exit 1

  PUSHED=0; OK=0
  restore() {
    [ "$PUSHED" = 1 ] && [ "$OK" != 1 ] || return 0
    echo "!! restoring the build ${VM} had, and starting it"
    lxc exec "$VM" -- sh -c 'test -f /root/house/speak.py.prev \
      && mv /root/house/speak.py.prev /root/house/speak.py' || true
    lxc exec "$VM" -- systemctl start citizen.service || true
  }
  trap restore EXIT

  lxc exec "$VM" -- cp /root/house/speak.py /root/house/speak.py.prev
  PUSHED=1
  lxc file push "$SRC/speak.py" "$VM/root/house/speak.py"
  lxc exec "$VM" -- python3 -c "import sys; sys.path.insert(0, '/root/house'); import speak; \
      raise SystemExit(0 if hasattr(speak, 'main') else 1)" \
    || { echo "!! the pushed speak.py is not usable in ${VM}"; exit 1; }

  lxc exec "$VM" -- systemctl restart citizen.service
  sleep 5
  lxc exec "$VM" -- systemctl is-active citizen.service >/dev/null \
    || { echo "!! ${VM} did not come back up"; exit 1; }
  OK=1
  lxc exec "$VM" -- rm -f /root/house/speak.py.prev
  trap - EXIT
  echo "=== ${SLOT} running the new build"
done
echo "=== ALL DEPLOYED"
