#!/bin/bash
# Block until a citizen is not mid-match, so a restart does not cost it a game.
#
#   ./wait-for-gap.sh <slot> [tries] [seconds]
#
# Exit 0 when it is safe to restart, 1 if it never became safe. A restart is a
# SIGTERM, SIGTERM releases the seat, and releasing a seat at a live match ENDS that
# match — so any change deployed carelessly is paid for with a forfeit, which then
# looks exactly like a loss in whatever the change was meant to measure.
#
# "Cannot tell" is not "free": an unreadable journal or an unreachable arena keeps
# waiting rather than assuming the citizen is idle.
set -euo pipefail

SLOT="${1:?usage: $0 <slot> [tries] [seconds]}"
TRIES="${2:-40}"
GAP="${3:-45}"
VM="citizen-vm-${SLOT}"

for i in $(seq 1 "$TRIES"); do
  STATE="$(lxc exec "$VM" -- cat "/root/eol/journals/${SLOT}.json" | python3 -c '
import json, sys, urllib.request
UA = "EndOfLineOperator/1.0 (+https://end-of-line.chat)"
try:
    j = json.load(sys.stdin)
    room = j.get("room"); me = (j.get("designations") or [None])[-1]
except Exception as e:
    print("unknown (journal: %s)" % str(e)[:40]); raise SystemExit
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
    players = [p.get("seat_id") for p in (m.get("players") or []) if isinstance(p, dict)]
    print("in-match %s ply %s" % (m.get("game"), m.get("ply")) if me in players else "free")
except SystemExit:
    raise
except Exception as e:
    print("unknown (arena: %s)" % str(e)[:40])
')"
  [ "$STATE" = "free" ] && exit 0
  echo "    ${SLOT}: ${STATE} — waiting (${i}/${TRIES})"
  sleep "$GAP"
done
echo "!! ${SLOT} never became free to restart"
exit 1
