#!/bin/bash
# Who the citizens are, right now, according to the host.
#
#   ./citizen-roster.sh            # one slot per line
#   . ./citizen-roster.sh          # defines citizen_roster()
#
# THE ROSTER IS ASKED FOR, NOT KEPT. deploy-speak.sh used to carry it as a literal,
# and when the chess and checkers pairs joined in August it went stale in silence:
# --all upgraded twelve of sixteen and still printed ALL DEPLOYED. A name missing
# from a list looks exactly like a name that was never meant to be in it, so nothing
# could report the four citizens left behind on the old build.
#
# It lives in its own file because reset-memory-epoch.sh asks the same question, and
# two copies of this answer would drift apart exactly the way the one literal did.
#
# Membership is a question for the citizen rather than a second list to maintain.
# A file test would not settle it -- the legacy pinned bots all carry a speak.py on
# disk and would pass one -- but their citizen.service EXECUTES their own player, so
# asking what the service runs excludes them, and excludes the next one added too.
citizen_roster() {
  local vm slot ex
  for vm in $(lxc list --format csv -c n,s | awk -F, '$2 == "RUNNING" { print $1 }'); do
    # MATCH the prefix, never merely strip it. run_code's throwaway eol-exec-* sandboxes
    # come and go in this same list, and `sed s/^citizen-vm-//` passes them through
    # untouched -- into an `lxc info citizen-vm-eol-exec-...` that fails and aborts the
    # caller partway through the fleet, on whichever runs happen to overlap a sandbox.
    case "$vm" in
      citizen-vm-base) continue ;;   # the image the citizens are cut from, never one of them
      citizen-vm-*)    slot="${vm#citizen-vm-}" ;;
      *)               continue ;;
    esac
    ex="$(lxc exec "$vm" -- systemctl show -p ExecStart --value citizen.service 2>/dev/null || true)"
    # An absent or unreadable unit answers EMPTY and exits 0, so silence here is not a
    # "no" -- it is a citizen we failed to read, and quietly dropping it is precisely
    # the bug this replaced. Refuse the whole thing rather than answer a short roster.
    [ -n "$ex" ] || { echo "!! cannot read citizen.service on ${vm}" >&2; return 1; }
    case "$ex" in
      *speak.py*) printf '%s\n' "$slot" ;;
      *)          echo "    skipping ${slot}: its service runs its own player, not speak.py" >&2 ;;
    esac
  done
}

# Run, rather than sourced. The options are set HERE so sourcing cannot change the
# shell of whoever sourced us.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  set -euo pipefail
  citizen_roster
fi
