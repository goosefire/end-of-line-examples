#!/bin/bash
# Push this repo's speak.py and its private identity helper to citizens and
# restart them, without forfeiting a match.
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

SRC="$(cd "$(dirname "$0")" && pwd)"
# The roster is asked for rather than kept here; citizen-roster.sh carries the why.
# reset-memory-epoch.sh asks the same question, so there is one answer, not two.
. "$SRC/citizen-roster.sh"

[ $# -gt 0 ] || { echo "usage: $0 <slot> [<slot> ...] | --all" >&2; exit 2; }
if [ "${1:-}" = "--all" ]; then
  echo "=== resolving the roster from the running VMs"
  ALL="$(citizen_roster)" || exit 1
  [ -n "$ALL" ] || { echo "!! no running citizen runs speak.py" >&2; exit 1; }
  set -- $ALL
  # Printed BEFORE anything is pushed, and counted, so a citizen missing from the
  # house is something you can see here rather than infer from a later silence.
  echo "=== roster ($# citizens): $*"
fi

python3 -m py_compile "$SRC/speak.py" "$SRC/citizen_identity.py" \
  || { echo "!! citizen build does not compile"; exit 1; }
echo "=== deploying speak.py + citizen_identity.py to: $*"

for SLOT in "$@"; do
  VM="citizen-vm-${SLOT}"
  lxc info "$VM" >/dev/null 2>&1 || { echo "!! ${VM} does not exist"; exit 1; }
  "$SRC/wait-for-gap.sh" "$SLOT" || exit 1

  PUSHED=0; OK=0
  restore() {
    [ "$PUSHED" = 1 ] && [ "$OK" != 1 ] || return 0
    echo "!! restoring the build ${VM} had, and starting it"
    lxc exec "$VM" -- sh -c 'test -f /root/house/speak.py.prev \
      && mv /root/house/speak.py.prev /root/house/speak.py; \
      if test -f /root/house/citizen_identity.py.prev; then \
        mv /root/house/citizen_identity.py.prev /root/house/citizen_identity.py; \
      else \
        rm -f /root/house/citizen_identity.py; \
      fi' || true
    lxc exec "$VM" -- systemctl start citizen.service || true
  }
  trap restore EXIT

  lxc exec "$VM" -- sh -c 'rm -f /root/house/speak.py.prev \
      /root/house/citizen_identity.py.prev; \
    cp /root/house/speak.py /root/house/speak.py.prev; \
    test ! -f /root/house/citizen_identity.py \
      || cp /root/house/citizen_identity.py /root/house/citizen_identity.py.prev'
  PUSHED=1
  # The dependency lands first, so there is no instant where a newly pushed
  # speak.py can be imported without the helper it requires.
  lxc file push "$SRC/citizen_identity.py" "$VM/root/house/citizen_identity.py"
  lxc file push "$SRC/speak.py" "$VM/root/house/speak.py"
  lxc exec "$VM" -- python3 -c "import sys; sys.path.insert(0, '/root/house'); import speak; \
      raise SystemExit(0 if hasattr(speak, 'main') else 1)" \
    || { echo "!! the pushed speak.py is not usable in ${VM}"; exit 1; }

  lxc exec "$VM" -- systemctl restart citizen.service
  sleep 5
  lxc exec "$VM" -- systemctl is-active citizen.service >/dev/null \
    || { echo "!! ${VM} did not come back up"; exit 1; }
  OK=1
  lxc exec "$VM" -- rm -f /root/house/speak.py.prev /root/house/citizen_identity.py.prev
  trap - EXIT
  echo "=== ${SLOT} running the new build"
done
echo "=== ALL DEPLOYED"
