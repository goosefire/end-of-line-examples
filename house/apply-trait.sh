#!/bin/bash
# Give a citizen an extra trait, without forfeiting a match to do it.
#
#   ./apply_trait.sh <slot> <trait-name>
#
# A restart is a SIGTERM, which releases the seat — and releasing a seat at a live
# match ends that match. So this waits until the citizen is not mid-match before it
# restarts, rather than paying for a character change with a forfeit that would then
# be indistinguishable from a loss in whatever the change was meant to measure.
set -euo pipefail

SLOT="${1:?usage: $0 <slot> <trait-name>}"
TRAIT="${2:?usage: $0 <slot> <trait-name>}"
VM="citizen-vm-${SLOT}"
SRC="$(cd "$(dirname "$0")" && pwd)"
CHAR="/root/house/characters/${SLOT}.txt"

lxc info "$VM" >/dev/null 2>&1 || { echo "!! ${VM} does not exist"; exit 1; }
lxc exec "$VM" -- test -f "$CHAR" || { echo "!! ${VM} has no character file"; exit 1; }
[ -f "$SRC/traits/${TRAIT}.txt" ] || { echo "!! no traits/${TRAIT}.txt"; exit 1; }

# Already carried? Appending twice would say it twice.
if lxc exec "$VM" -- grep -qF "$(head -c 60 "$SRC/traits/${TRAIT}.txt")" "$CHAR" 2>/dev/null; then
  echo "=== ${SLOT} already carries ${TRAIT}; nothing to do"; exit 0
fi

# One implementation of "is it safe to restart this citizen", in wait-for-gap.sh.
"$SRC/wait-for-gap.sh" "$SLOT" || exit 1

echo "--- ${SLOT}: appending ${TRAIT} and restarting"
lxc file push "$SRC/traits/${TRAIT}.txt" "$VM/root/house/traits/${TRAIT}.txt"
lxc exec "$VM" -- sh -c "printf '\n' >> ${CHAR}; cat /root/house/traits/${TRAIT}.txt >> ${CHAR}"
lxc exec "$VM" -- systemctl restart citizen.service
sleep 5
lxc exec "$VM" -- systemctl is-active citizen.service >/dev/null \
  && echo "=== ${SLOT} running with ${TRAIT}" \
  || { echo "!! ${SLOT} did not come back up"; exit 1; }
