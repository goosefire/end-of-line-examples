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

ALL="observe research fabricate lexicon contest gambit herald ledger odds spar sieve assay"
[ $# -gt 0 ] || { echo "usage: $0 <slot> [<slot> ...] | --all" >&2; exit 2; }
[ "${1:-}" = "--all" ] && set -- $ALL

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
