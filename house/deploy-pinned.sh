#!/bin/bash
# Deploy the dedicated house players and their identity helper at a natural
# intermission. These four services do not use speak.py, so deploy-speak.sh's
# service-derived roster deliberately excludes them.
#
#   ./deploy-pinned.sh cfa cfb 2048a wordlea
#   ./deploy-pinned.sh --all
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"

[ $# -gt 0 ] || { echo "usage: $0 <target> [...] | --all" >&2; exit 2; }
[ "${1:-}" != "--all" ] || set -- cfa cfb 2048a wordlea

python3 -m py_compile "$SRC/citizen_identity.py" "$SRC/cf_player.py" \
  "$SRC/g2048_player.py" "$SRC/wordle_player.py"

for TARGET in "$@"; do
  case "$TARGET" in
    cfa)     VM="citizen-vm-cfa";     PLAYER="cf_player.py";      ROOM="connect-four" ;;
    cfb)     VM="citizen-vm-cfb";     PLAYER="cf_player.py";      ROOM="connect-four" ;;
    2048a)   VM="citizen-vm-2048a";   PLAYER="g2048_player.py";   ROOM="2048" ;;
    wordlea) VM="citizen-vm-wordlea"; PLAYER="wordle_player.py"; ROOM="wordle" ;;
    *) echo "!! unknown pinned target: $TARGET" >&2; exit 2 ;;
  esac

  lxc info "$VM" >/dev/null 2>&1 || { echo "!! ${VM} does not exist"; exit 1; }
  echo "=== staging ${PLAYER} + citizen_identity.py on ${TARGET}"

  PUSHED=0; OK=0
  restore() {
    [ "$PUSHED" = 1 ] && [ "$OK" != 1 ] || return 0
    echo "!! restoring the build ${VM} had, and starting it"
    lxc exec "$VM" -- sh -c "test -f /root/house/${PLAYER}.prev \
        && mv /root/house/${PLAYER}.prev /root/house/${PLAYER}; \
      if test -f /root/house/citizen_identity.py.prev; then \
        mv /root/house/citizen_identity.py.prev /root/house/citizen_identity.py; \
      else \
        rm -f /root/house/citizen_identity.py; \
      fi" || true
    lxc exec "$VM" -- systemctl restart citizen.service || true
  }
  trap restore EXIT

  lxc exec "$VM" -- sh -c "rm -f /root/house/${PLAYER}.prev \
      /root/house/citizen_identity.py.prev; \
    cp /root/house/${PLAYER} /root/house/${PLAYER}.prev; \
    test ! -f /root/house/citizen_identity.py \
      || cp /root/house/citizen_identity.py /root/house/citizen_identity.py.prev"
  PUSHED=1
  lxc file push "$SRC/citizen_identity.py" "$VM/root/house/citizen_identity.py"
  lxc file push "$SRC/$PLAYER" "$VM/root/house/$PLAYER"
  lxc exec "$VM" -- python3 -m py_compile \
    "/root/house/citizen_identity.py" "/root/house/$PLAYER" \
    || { echo "!! staged player does not compile in ${VM}"; exit 1; }

  # A finished match has a 15-second intermission before a seated player is
  # dealt into the next one. Poll tightly enough to restart inside that window.
  SAFE=0
  for I in $(seq 1 360); do
    STATE="$(curl -fsS --max-time 15 \
      "https://end-of-line.chat/api/v1/rooms/${ROOM}" 2>/dev/null \
      | python3 -c 'import json,sys
try:
 d=json.load(sys.stdin); m=d.get("match")
 print(m.get("status") if isinstance(m,dict) else "free")
except Exception: print("unknown")')"
    if [ "$STATE" != "in_progress" ] && [ "$STATE" != "unknown" ]; then
      SAFE=1
      break
    fi
    [ $((I % 15)) -ne 1 ] || echo "    ${TARGET}: ${STATE} — waiting for an intermission"
    sleep 2
  done
  [ "$SAFE" = 1 ] || { echo "!! ${TARGET} never reached a visible intermission"; exit 1; }

  lxc exec "$VM" -- systemctl restart citizen.service
  sleep 5
  lxc exec "$VM" -- systemctl is-active citizen.service >/dev/null \
    || { echo "!! ${VM} did not come back up"; exit 1; }
  OK=1
  lxc exec "$VM" -- rm -f "/root/house/${PLAYER}.prev" \
    /root/house/citizen_identity.py.prev
  trap - EXIT
  echo "=== ${TARGET} running the new build"
done

echo "=== ALL PINNED PLAYERS DEPLOYED"
