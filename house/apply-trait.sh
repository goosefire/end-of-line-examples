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

# Wait for a gap. 40 tries x 45s covers a full 8-attempt word500 at its turn clock.
for i in $(seq 1 40); do
  STATE="$(lxc exec "$VM" -- cat "/root/eol/journals/${SLOT}.json" | python3 -c '
import json, sys, urllib.request
UA = "EndOfLineOperator/1.0 (+https://end-of-line.chat)"
try:
    j = json.load(sys.stdin)
    room = j.get("room"); me = (j.get("designations") or [None])[-1]
except Exception:
    print("unknown"); raise SystemExit
if not room or not me:
    print("free"); raise SystemExit
try:
    r = urllib.request.Request("https://end-of-line.chat/api/v1/rooms/%s?since=1" % room,
                               headers={"User-Agent": UA})
    with urllib.request.urlopen(r, timeout=20) as f:
        d = json.load(f)
    m = d.get("match")
    if not isinstance(m, dict) or m.get("status") != "in_progress":
        print("free"); raise SystemExit
    if me in [p.get("seat_id") for p in (m.get("players") or []) if isinstance(p, dict)]:
        print("in-match %s ply %s" % (m.get("game"), m.get("ply")))
    else:
        print("free")
except SystemExit:
    raise
except Exception as e:
    print("unknown (%s)" % str(e)[:40])
')"
  case "$STATE" in
    free) break ;;
    *) echo "    ${SLOT}: ${STATE} — waiting (${i}/40)"; sleep 45 ;;
  esac
done
[ "${STATE:-}" = "free" ] || { echo "!! ${SLOT} never left a match; not restarting it"; exit 1; }

echo "--- ${SLOT}: appending ${TRAIT} and restarting"
lxc file push "$SRC/traits/${TRAIT}.txt" "$VM/root/house/traits/${TRAIT}.txt"
lxc exec "$VM" -- sh -c "printf '\n' >> ${CHAR}; cat /root/house/traits/${TRAIT}.txt >> ${CHAR}"
lxc exec "$VM" -- systemctl restart citizen.service
sleep 5
lxc exec "$VM" -- systemctl is-active citizen.service >/dev/null \
  && echo "=== ${SLOT} running with ${TRAIT}" \
  || { echo "!! ${SLOT} did not come back up"; exit 1; }
