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
# That argument applies to THIS SCRIPT TOO, which is what the lock below is for. Two
# operators, or one impatient one, and the second run's exit trap restarts the citizen
# inside the first run's window — a live writer, mid-reset, reverting it on its next
# turn. The lock is per slot and held for the life of the run.
#
# ORDER. The archive is taken as soon as no writer is left, BEFORE the seat check and
# before anything is written: the case where a copy matters most is a journal too
# corrupt to parse, and a seat check that reads the journal would have aborted first
# and taken none. The PROCESS gate is what protects the file, and it refuses on
# "cannot tell" as well as on "still there". The SEAT check is advisory by comparison
# — it stops the run on a positive sighting and warns when the arena cannot be read.
#
# WHAT SURVIVES a reset is decided by speak.reset_epoch — identity, the room, and the
# dedupe marks — and is unit-tested there, not here. What this script owns is that the
# old journal is off the VM and byte-identical before anything is written over it, and
# that a failure anywhere never leaves a citizen stopped.
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
STOPPED=0; PUSHED=0; FINISHED=0

# Never leave a citizen down, and never leave it on code this run put there and could
# not finish with. Every exit between the stop and the start comes back through here,
# including the refusals — a stopped citizen holds no seat and plays no match, so an
# operator who has to go and look should find it running, on the build it had before.
#
# The code goes back BEFORE the service comes up: verifying the push only proves the
# module imports, which is a weaker claim than "this build works", and a citizen
# restarted onto a build that failed halfway through a reset is a crash loop under
# Restart=always.
restore() {
  local code=$?
  if [ "$PUSHED" = 1 ] && [ "$FINISHED" != 1 ]; then
    echo "!! restoring the speak.py ${VM} had before this run"
    lxc exec "$VM" -- sh -c 'test -f /root/house/speak.py.prev \
      && mv /root/house/speak.py.prev /root/house/speak.py' \
      || echo "!! could not restore it; look at /root/house/speak.py.prev in ${VM}"
  fi
  if [ "$STOPPED" = 1 ] && [ "$FINISHED" != 1 ]; then
    echo "!! exiting (${code}) with ${VM} stopped — restarting it rather than leaving it down"
    # And CHECK that it took. "systemctl start returned 0" is not "the citizen is
    # running"; a unit that starts and dies satisfies the first and not the second,
    # and this promise is the reason an operator can walk away from a failed run.
    if lxc exec "$VM" -- systemctl start citizen.service \
       && sleep 3 && lxc exec "$VM" -- systemctl is-active citizen.service >/dev/null; then
      echo "   ${VM} is running again"
    else
      echo "!! ${VM} IS STILL DOWN. By hand: lxc exec ${VM} -- systemctl start citizen.service"
    fi
  fi
}
trap restore EXIT

# 0 = a writer is still there, 1 = definitely none, 2 = could not tell.
# Three states on purpose: `pgrep` answers 1 for "no match" and `lxc exec` answers 1
# for "could not run at all", and collapsing those two is exactly the check that says
# "safe" for the wrong reason. Reads the process table directly so a writer started
# by hand, or as `python3 -m speak`, is not missed by a pattern shaped like the unit.
# NO PIPELINE. `printf … | grep -q` looks obvious and is the bug: grep -q exits on the
# first match, printf takes SIGPIPE on a process table bigger than the pipe buffer,
# and under `pipefail` the pipeline reports 141 — so the match that PROVES a writer is
# alive is read as "definitely none", and the journal is replaced underneath it.
# Measured: exit 141 on a 20k-line listing. Bash's own pattern match cannot do that.
speak_running() {
  local out
  out="$(lxc exec "$VM" -- sh -c 'ps -eo args= 2>/dev/null || true')" || return 2
  [ -n "$out" ] || return 2
  case "$out" in
    *speak.py*|*"-m speak"*) return 0 ;;
  esac
  return 1
}

echo "=== ${VM}: reset memory epoch"
echo "    reason:  ${REASON}"
echo "    archive: ${ARCHIVE}/${SLOT}.json"

exec 9>"${TMPDIR:-/tmp}/eol-reset-${SLOT}.lock"
flock -n 9 || { echo "!! another reset is already running for ${SLOT}"; exit 1; }

lxc info "$VM" >/dev/null 2>&1 || { echo "!! ${VM} does not exist"; exit 1; }
lxc exec "$VM" -- test -f "$JOURNAL" || { echo "!! ${VM} has no journal at ${JOURNAL}"; exit 1; }

# Preflight the code that will do the reset, BEFORE stopping anything. Without --push
# that is the VM's deployed speak.py. WITH --push the host copy is only a smoke test:
# it says the file parses here, not that it runs there, so the real check happens in
# the VM after the push, with the previous file kept to fall back to.
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
[ -n "$MAINPID" ] || {
  echo "!! systemd reported no MainPID at all for citizen.service — REFUSING"; exit 1; }
[ "$MAINPID" = "0" ] || {
  echo "!! systemd still reports citizen.service MainPID=${MAINPID} — REFUSING"; exit 1; }
echo "    no writer left"

# 3. Archive FIRST, and prove the archive. Read through `lxc exec`, never
#    `lxc file pull`: pull hands back a stale copy of a file the citizen rewrites
#    every turn. Nothing between here and the write can cost the operator the journal.
( umask 077 && mkdir -p "$ARCHIVE" )    # a citizen's memory is its own, not the box's
BEFORE="$(lxc exec "$VM" -- sha256sum "$JOURNAL" | awk '{print $1}')"
( umask 077 && lxc exec "$VM" -- cat "$JOURNAL" > "${ARCHIVE}/${SLOT}.json" )
sync "${ARCHIVE}/${SLOT}.json" 2>/dev/null || sync   # a cached copy is not a backup
AFTER="$(sha256sum "${ARCHIVE}/${SLOT}.json" | awk '{print $1}')"
if [ "$BEFORE" != "$AFTER" ]; then
  echo "!! the archive does not match the journal it came from"
  echo "   on the VM: ${BEFORE}"
  echo "   archived:  ${AFTER}"
  echo "   Nothing has been written."
  exit 1
fi
echo "    archived ${AFTER:0:12} ($(wc -c < "${ARCHIVE}/${SLOT}.json") bytes)"

# 4. Push now the service is down and the journal is safely off the box. Doing it
#    while the unit was live meant a Restart=always service could come back on a
#    half-written file. Verified IN THE VM, with the previous file kept, because a
#    build that imports on this host and not in there would otherwise be left
#    crash-looping by the restart in `restore`.
if [ "$PUSH" = 1 ]; then
  echo "--- pushing speak.py"
  lxc exec "$VM" -- cp /root/house/speak.py /root/house/speak.py.prev
  PUSHED=1
  lxc file push "$SRC/speak.py" "$VM/root/house/speak.py"
  # Every name the reset below actually uses. `hasattr(reset_epoch)` alone passes a
  # build that then dies at the write with the new code already in place. The .prev
  # copy stays until this run FINISHES, so any later failure still rolls it back.
  lxc exec "$VM" -- python3 -c "import sys; sys.path.insert(0, '/root/house'); import speak; \
      raise SystemExit(0 if all(hasattr(speak, n) for n in \
          ('reset_epoch', 'FileStore', 'new_journal', 'journal')) else 1)" \
    || { echo "!! the pushed speak.py is not usable in ${VM}"; exit 1; }
  echo "    pushed and verified in the VM"
fi

# 5. The seat. SIGTERM releases it, so a seat still held means the release did not
#    happen. An arena that cannot be READ is a different thing from a seat that can
#    be SEEN: the first is unproven, the second is wrong, and only the second stops
#    the run. Checked against the LAST designation — the seat the citizen currently
#    holds. Every earlier one was released by the move that replaced it, and matching
#    on all of them means a re-issued designation seated by somebody else pins this
#    citizen as unresettable forever.
SEAT_STATE="$(lxc exec "$VM" -- cat "$JOURNAL" | python3 -c '
import json, sys, time, urllib.request

UA = "EndOfLineOperator/1.0 (+https://end-of-line.chat)"
try:
    j = json.load(sys.stdin)
    room = j.get("room")
    held = (j.get("designations") or [None])[-1]
except Exception as e:
    # The journal is unreadable. That is the reset the operator needs MOST, the
    # archive is already taken, and speak.FileStore.get will refuse cleanly below.
    print("unverified (journal unreadable: %s)" % str(e)[:50])
    raise SystemExit
if not room or not held:
    print("skip never-seated")
    raise SystemExit
url = "https://end-of-line.chat/api/v1/rooms/%s?since=1" % room
for attempt in range(4):
    last = attempt == 3
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            state = json.load(r)
        # A 200 whose body has no programs LIST is a body we did not understand — an
        # error object, a changed shape, an interstitial that happens to parse.
        # Reading that as "nobody is seated" is the wrong-reason answer.
        if not isinstance(state, dict) or not isinstance(state.get("programs"), list):
            raise ValueError("no programs list in the room read")
        seated = {p.get("seat_id") for p in state["programs"] if isinstance(p, dict)}
        if held not in seated:
            print("released %s" % room)
            raise SystemExit
        if last:
            print("HELD %s %s" % (room, held))
            raise SystemExit
    except SystemExit:
        raise
    except Exception as e:
        # Deliberately broad: every failure here is "could not tell", and the one
        # thing this must never do is turn a shape it did not expect into "released".
        if last:
            print("unverified %s (%s)" % (room, str(e)[:60]))
            raise SystemExit
    time.sleep(5)
')"
case "${SEAT_STATE:-unverified (the check returned nothing)}" in
  HELD*) echo "!! seat still held after the stop: ${SEAT_STATE#HELD }"
         echo "   The journal is safe and archived, but the arena still shows this"
         echo "   citizen seated. Resolve that before resetting it."
         exit 1 ;;
  unverified*) echo "    seat NOT VERIFIED — ${SEAT_STATE#unverified }"
               echo "    (the journal gate above still passed; the archive is taken)" ;;
  skip*) echo "    seat check skipped — this citizen has never been seated" ;;
  *)     echo "    seat ${SEAT_STATE}" ;;
esac

# 6. Open the new epoch, through the same reset_epoch the tests cover. The writer gate
#    is asked once more first: minutes of arena reads have passed since it was proven,
#    and this is the one step whose whole safety argument is that nothing else holds
#    the journal.
rc=0; speak_running || rc=$?
[ "$rc" = 1 ] || { echo "!! a writer appeared before the write (state ${rc}) — REFUSING"; exit 1; }
echo "--- opening the new epoch"
lxc exec "$VM" --env SLOT="$SLOT" --env REASON="$REASON" -- python3 - <<'PY'
import os, sys
sys.path.insert(0, "/root/house")
import speak

slot = os.environ["SLOT"]
store = speak.FileStore("/root/eol")
old = store.get(slot)          # SystemExits with its own message on a corrupt journal
if old is None:
    raise SystemExit("journal vanished between the archive and the write — refusing")
fresh = speak.reset_epoch(old, os.environ["REASON"])

# Checked BEFORE the write, and with `if`, not `assert`: a check after store.put is a
# check made too late, and `python3 -O` deletes an assert without deleting the write
# it was guarding. reset_epoch coerces rather than copies, so a journal whose
# designations were a string comes back as a sound list — compare against what it
# produced, and report what was dropped instead of pretending nothing was.
bad = [k for k, v in (("recent", []), ("episodes", []), ("episodes_upto", 0)) if fresh[k] != v]
if bad or not isinstance(fresh.get("memory_epoch"), int):
    raise SystemExit("reset_epoch produced an unusable journal (%s) — refusing to write" % bad)
kept, had = len(fresh["designations"]), len(old.get("designations") or [])
store.put(slot, fresh)

back = store.get(slot)
if back["recent"] or back["episodes"] or back["episodes_upto"]:
    raise SystemExit("the journal did not read back as reset — look at it before starting")
print("    epoch %d, born %s, room %s, designations %d%s"
      % (back["memory_epoch"], back["born"], back["room"], kept,
         "" if kept == had else " (%d dropped as malformed)" % (had - kept)))
PY

# 7. Back on its feet.
echo "--- starting citizen.service"
lxc exec "$VM" -- systemctl start citizen.service
sleep 5
if lxc exec "$VM" -- systemctl is-active citizen.service >/dev/null; then
  FINISHED=1
  lxc exec "$VM" -- rm -f /root/house/speak.py.prev
  echo "=== ${VM} RUNNING on a new epoch"
else
  echo "!! ${VM} did not come back up"; exit 1
fi
