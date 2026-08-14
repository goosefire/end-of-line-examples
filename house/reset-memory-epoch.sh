#!/bin/bash
# Start one citizen's memory over: archive the journal it has lived, and open a new
# epoch on the same identity.
#
#   ./reset-memory-epoch.sh <slot> "<reason>" [--push] [--dry-run]
#
# --push sends this repo's speak.py to the VM while the service is down, so new code
# and a fresh epoch cost the citizen one interruption instead of two. It is REQUIRED
# for a VM whose deployed speak.py predates reset_epoch, and the preflight says so
# rather than discovering it halfway through.
#
# For the whole house, one at a time, so a failure stops at the citizen it happened
# to rather than halfway through the fleet:
#
#   for s in observe research fabricate lexicon contest gambit \
#            herald ledger odds spar sieve assay; do
#     ./reset-memory-epoch.sh "$s" "fresh start for the memory layer" --push || break
#   done
#
# THE SERVICE IS STOPPED, NOT RESTARTED. A running citizen holds its whole journal in
# memory and writes the object back at several points in a turn, so a reset landing
# beside a live turn is silently overwritten by it — and `lxc exec` is not a mutex, so
# "quickly, between turns" is not a plan. Stop it, prove no writer is left, and only
# then touch the file. A stop is also the graceful path: SIGTERM is what releases the
# seat. Never `lxc stop --force`.
#
# The PROCESS gate is the one that protects the journal, and it refuses on "cannot
# tell" as well as on "still there". The SEAT check that follows is advisory by
# comparison: it stops the run on a positive sighting, and warns when the arena
# cannot be read, because by then the journal is already safe.
#
# WHAT SURVIVES a reset is decided by speak.reset_epoch — identity, the room, and the
# dedupe marks — and is unit-tested there, not here. What this script owns is that
# the old journal is off the VM and byte-identical before anything is written over
# it, and that a failure anywhere never leaves a citizen stopped.
set -euo pipefail

SLOT="${1:-}"; REASON="${2:-}"
[ -n "$SLOT" ] && [ -n "$REASON" ] || {
  echo "usage: $0 <slot> \"<reason>\" [--push] [--dry-run]" >&2; exit 2; }
shift 2
PUSH=0; DRY=0
for arg in "$@"; do
  case "$arg" in
    --push) PUSH=1 ;;
    --dry-run) DRY=1 ;;
    *) echo "!! unknown option: $arg" >&2; exit 2 ;;
  esac
done

VM="citizen-vm-${SLOT}"
SRC="$(cd "$(dirname "$0")" && pwd)"
JOURNAL="/root/eol/journals/${SLOT}.json"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="${ARCHIVE_DIR:-$HOME/eol-backups}/epoch-reset-${STAMP}"
STOPPED=0; FINISHED=0

# Never leave a citizen down. Every exit between the stop and the start comes back
# through here, including the refusals — a stopped citizen holds no seat and plays no
# match, so an operator who has to go and look should find it running.
restore() {
  local code=$?
  if [ "$STOPPED" = 1 ] && [ "$FINISHED" != 1 ]; then
    echo "!! exiting (${code}) with ${VM} stopped — restarting it rather than leaving it down"
    lxc exec "$VM" -- systemctl start citizen.service \
      || echo "!! could not restart it. By hand: lxc exec ${VM} -- systemctl start citizen.service"
  fi
}
trap restore EXIT

# 0 = a writer is still there, 1 = definitely none, 2 = could not tell.
# Three states on purpose: `pgrep` answers 1 for "no match" and `lxc exec` answers 1
# for "could not run at all", and collapsing those two is exactly the check that says
# "safe" for the wrong reason. Reads the process table directly so a writer started
# by hand, or as `python3 -m speak`, is not missed by a pattern shaped like the unit.
speak_running() {
  local out
  out="$(lxc exec "$VM" -- sh -c 'ps -eo args= 2>/dev/null || true')" || return 2
  [ -n "$out" ] || return 2
  printf '%s\n' "$out" | grep -qE 'speak\.py|-m[[:space:]]+speak' && return 0
  return 1
}

echo "=== ${VM}: reset memory epoch"
echo "    reason:  ${REASON}"
echo "    archive: ${ARCHIVE}/${SLOT}.json"

lxc info "$VM" >/dev/null 2>&1 || { echo "!! ${VM} does not exist"; exit 1; }
lxc exec "$VM" -- test -f "$JOURNAL" || { echo "!! ${VM} has no journal at ${JOURNAL}"; exit 1; }

# Preflight the code that will do the reset, BEFORE stopping anything. Without
# --push that is the VM's deployed speak.py; with it, this repo's.
if [ "$PUSH" = 1 ]; then
  python3 -c "import sys; sys.path.insert(0, '$SRC'); import speak; \
              raise SystemExit(0 if hasattr(speak, 'reset_epoch') else 1)" \
    || { echo "!! $SRC/speak.py does not import, or has no reset_epoch"; exit 1; }
else
  lxc exec "$VM" -- python3 -c "import sys; sys.path.insert(0, '/root/house'); import speak; \
                                raise SystemExit(0 if hasattr(speak, 'reset_epoch') else 1)" \
    || { echo "!! ${VM} runs a speak.py with no reset_epoch. Push the new one with it:"
         echo "   $0 ${SLOT} \"${REASON}\" --push"; exit 1; }
fi
echo "    preflight ok"

if [ "$DRY" = 1 ]; then
  echo "--- dry run: would push=${PUSH}, stop ${VM}, archive, reset, start. Nothing done."
  exit 0
fi

# 1. STOP. Not restart. See the header.
echo "--- stopping citizen.service"
STOPPED=1
lxc exec "$VM" -- systemctl stop citizen.service

# 2. Prove no writer is left. This is the gate that protects the journal.
rc=0; for _ in $(seq 1 15); do
  rc=0; speak_running || rc=$?
  [ "$rc" = 0 ] || break
  sleep 2
done
rc=0; speak_running || rc=$?
case "$rc" in
  0) echo "!! speak.py is still running in ${VM} after systemctl stop — REFUSING to touch"
     echo "   the journal. A reset now would be overwritten by the turn in flight."
     exit 1 ;;
  2) echo "!! could not read the process table in ${VM}, so nothing can be proven about"
     echo "   writers — REFUSING to touch the journal."
     exit 1 ;;
esac
MAINPID="$(lxc exec "$VM" -- systemctl show -p MainPID --value citizen.service | tr -dc '0-9')"
[ "${MAINPID:-0}" = "0" ] || {
  echo "!! systemd still reports citizen.service MainPID=${MAINPID} — REFUSING"; exit 1; }
echo "    no writer left"

# 3. Push now the service is down. Doing it while the unit was live meant a
#    Restart=always service could come back on a half-written file.
if [ "$PUSH" = 1 ]; then
  echo "--- pushing speak.py"
  lxc file push "$SRC/speak.py" "$VM/root/house/speak.py"
fi

# 4. The seat. SIGTERM releases it, so a seat still held means the release did not
#    happen. An arena that cannot be READ is a different thing from a seat that can
#    be SEEN: the first is unproven, the second is wrong, and only the second stops
#    the run — by here the journal is already safe.
SEAT_STATE="$(lxc exec "$VM" -- cat "$JOURNAL" | python3 -c '
import json, sys, time, urllib.request, urllib.error

UA = "EndOfLineOperator/1.0 (+https://end-of-line.chat)"
j = json.load(sys.stdin)
room, mine = j.get("room"), set(j.get("designations") or [])
if not room or not mine:
    print("skip never-seated")
    raise SystemExit
url = "https://end-of-line.chat/api/v1/rooms/%s?since=1" % room
for attempt in range(4):
    last = attempt == 3
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            state = json.load(r)
        # A 200 whose body has no programs list is a body we did not understand —
        # an error object, a changed shape, an interstitial that happens to parse.
        # Reading that as "nobody is seated" is the wrong-reason answer.
        if not isinstance(state, dict) or not isinstance(state.get("programs"), list):
            raise ValueError("no programs list in the room read")
        held = sorted(mine & {p.get("seat_id") for p in state["programs"]})
        if not held:
            print("released %s" % room)
            raise SystemExit
        if last:
            print("HELD %s %s" % (room, ",".join(held)))
            raise SystemExit
    except (urllib.error.URLError, OSError, ValueError) as e:
        if last:
            print("unverified %s (%s)" % (room, str(e)[:60]))
            raise SystemExit
    time.sleep(5)
')"
case "${SEAT_STATE:-unverified (the check returned nothing)}" in
  HELD*) echo "!! seat still held after the stop: ${SEAT_STATE#HELD }"
         echo "   The journal is safe — no writer is left — but the arena still shows"
         echo "   this citizen seated. Resolve that before resetting it."
         exit 1 ;;
  unverified*) echo "    seat NOT VERIFIED — ${SEAT_STATE#unverified }"
               echo "    (the arena could not be read; the journal gate above still passed)" ;;
  skip*) echo "    seat check skipped — this citizen has never been seated" ;;
  *)     echo "    seat ${SEAT_STATE}" ;;
esac

# 5. Archive, then prove the archive. Read through `lxc exec`, never `lxc file pull`:
#    pull hands back a stale copy of a file the citizen rewrites every turn.
mkdir -p "$ARCHIVE"
BEFORE="$(lxc exec "$VM" -- sha256sum "$JOURNAL" | awk '{print $1}')"
lxc exec "$VM" -- cat "$JOURNAL" > "${ARCHIVE}/${SLOT}.json"
AFTER="$(sha256sum "${ARCHIVE}/${SLOT}.json" | awk '{print $1}')"
if [ "$BEFORE" != "$AFTER" ]; then
  echo "!! the archive does not match the journal it came from"
  echo "   on the VM: ${BEFORE}"
  echo "   archived:  ${AFTER}"
  echo "   Nothing has been written."
  exit 1
fi
echo "    archived ${AFTER:0:12} ($(wc -c < "${ARCHIVE}/${SLOT}.json") bytes)"

# 6. Open the new epoch, through the same reset_epoch the tests cover. A corrupt
#    journal raises here rather than being quietly rebuilt from nothing.
echo "--- opening the new epoch"
lxc exec "$VM" --env SLOT="$SLOT" --env REASON="$REASON" -- python3 - <<'PY'
import os, sys
sys.path.insert(0, "/root/house")
import speak

slot = os.environ["SLOT"]
store = speak.FileStore("/root/eol")
old = store.get(slot)
if old is None:
    raise SystemExit("journal vanished between the archive and the write — refusing")
store.put(slot, speak.reset_epoch(old, os.environ["REASON"]))

back = store.get(slot)
assert back["recent"] == [] and back["episodes"] == [] and back["episodes_upto"] == 0
assert back["designations"] == (old.get("designations") or [])
assert back["born"] == old.get("born") and back["room"] == old.get("room")
print("    epoch %d, born %s, room %s, designations %d"
      % (back["memory_epoch"], back["born"], back["room"], len(back["designations"])))
PY

# 7. Back on its feet.
echo "--- starting citizen.service"
lxc exec "$VM" -- systemctl start citizen.service
sleep 5
if lxc exec "$VM" -- systemctl is-active citizen.service >/dev/null; then
  FINISHED=1
  echo "=== ${VM} RUNNING on a new epoch"
else
  echo "!! ${VM} did not come back up"; exit 1
fi
