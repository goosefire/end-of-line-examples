#!/bin/bash
# Stand up one citizen in its own LXD virtual machine.
#
#   ./deploy-citizen.sh <slot> <room> <persona> [trait ...]
#
# The README covers running a resident as a process; this is the tier the live
# house runs on, one VM per citizen, because the wall between a resident and the
# host is drawn by the CPU rather than by software (see CITIZENS.md).
#
# Assumes a stopped `citizen-vm-base` that already carries /root/house and a
# /root/eol/minimax.env holding the model key. The key is NEVER passed on a
# command line or written by this script.
set -euo pipefail

SLOT="$1"; ROOM="$2"; PERSONA="$3"; shift 3
TRAITS=("$@")
VM="citizen-vm-${SLOT}"
SRC="$(cd "$(dirname "$0")" && pwd)"
MODEL="${MODEL:-MiniMax-M3}"
GRANT="${GRANT:-move}"

echo "=== ${VM}: room=${ROOM} persona=${PERSONA} traits=${TRAITS[*]:-none}"
echo "    model=${MODEL} grant=${GRANT}"

if lxc info "$VM" >/dev/null 2>&1; then
  echo "!! ${VM} already exists — refusing to clobber it"; exit 1
fi

lxc copy citizen-vm-base "$VM" --instance-only
lxc start "$VM"

# Wait for the guest agent rather than sleeping a guessed amount.
for _ in $(seq 1 40); do lxc exec "$VM" -- true 2>/dev/null && break; sleep 3; done
lxc exec "$VM" -- true || { echo "!! ${VM} never came up"; exit 1; }

lxc file push "$SRC/speak.py" "$VM/root/house/speak.py"
lxc exec "$VM" -- mkdir -p /root/house/personas /root/house/traits /root/house/characters
lxc file push "$SRC/personas/${PERSONA}.txt" "$VM/root/house/personas/${PERSONA}.txt"

# The character file is persona + traits CONCATENATED, because --trait takes one
# path. They stay separate in the repo — they are the reusable parts — and are
# combined here so the combination is explicit rather than a third file someone
# has to keep in step with the other two.
CHAR="/root/house/characters/${SLOT}.txt"
lxc exec "$VM" -- sh -c "cat /root/house/personas/${PERSONA}.txt > ${CHAR}"
for tr in "${TRAITS[@]:-}"; do
  [ -n "$tr" ] || continue
  lxc file push "$SRC/traits/${tr}.txt" "$VM/root/house/traits/${tr}.txt"
  lxc exec "$VM" -- sh -c "printf '\n' >> ${CHAR}; cat /root/house/traits/${tr}.txt >> ${CHAR}"
done

# A fresh citizen must not inherit the base image's journal, or it wakes up
# believing it has already lived somewhere as somebody else.
lxc exec "$VM" -- sh -c "rm -rf /root/eol/journals/* /root/eol/choices/* /root/eol/logs/* 2>/dev/null; true"
lxc exec "$VM" -- test -f /root/eol/minimax.env || { echo "!! ${VM} has no model key"; exit 1; }

lxc exec "$VM" -- sh -c "cat > /etc/systemd/system/citizen.service <<UNIT
[Unit]
Description=End of Line citizen (${VM})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/house
EnvironmentFile=/root/eol/minimax.env
ExecStart=/usr/bin/python3 /root/house/speak.py --room ${ROOM} --slot ${SLOT} \\
  --trait characters/${SLOT}.txt --model ${MODEL} --dir /root/eol \\
  --tools --grant ${GRANT}
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
UNIT"

lxc exec "$VM" -- systemctl daemon-reload
lxc exec "$VM" -- systemctl enable --now citizen
sleep 5
lxc exec "$VM" -- systemctl is-active citizen && echo "=== ${VM} RUNNING"
