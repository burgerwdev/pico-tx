#!/usr/bin/env bash
# serial.sh — serial terminal that survives Pico reboots.
#
# Every reinit/pdm/refdiv... that reboots the board makes the USB CDC port
# vanish and re-enumerate (often as a NEW /dev/ttyACM number), which kills
# screen and leaves the terminal in a mess.  This script:
#   - uses tio --auto-reconnect (preferred): it reopens the port when the
#     device comes back, so the console prompt survives reboots;
#   - falls back to a screen re-attach loop (Ctrl-A \ quits screen).
#
# Recommended: pin a stable device name with a udev rule so the ttyACM
# number never matters:
#
#   # /etc/udev/rules.d/99-pico.rules
#   SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="fa50", \
#       SYMLINK+="pico"
#   # then:
#   sudo udevadm control --reload && sudo udevadm trigger
#
# Install tio (auto-reconnect):  sudo pacman -S tio
#
# Usage: ./serial.sh [device]    (default: the board's CDC port, resolved by
#                                 tools/pico_port.sh from USB IDs 1209:fa50,
#                                 falling back to /dev/pico)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-}"
if [ -z "${PORT}" ]; then
    PORT="$("${SCRIPT_DIR}/pico_port.sh")" || exit 1
fi
[ -n "${PORT}" ] || { echo "error: no serial port; is the Pico plugged in?" >&2; exit 1; }

if command -v tio >/dev/null 2>&1; then
    echo "==> tio ${PORT} @115200 (auto-reconnects on Pico reboots; Ctrl-T Q to quit)"
    # tio reconnects automatically when the device reappears (unless
    # --no-reconnect).  Without the udev symlink, add -a new so it re-picks
    # the next /dev/ttyACM* when the number changes.
    exec tio -b 115200 "${PORT}"
fi

echo "==> tio not found - using a screen re-attach loop."
echo "    For auto-reconnect install tio:  sudo pacman -S tio"
echo "    Quit screen with Ctrl-A \\ (it will just re-attach otherwise)."
while true; do
    if [ -e "${PORT}" ]; then
        screen "${PORT}" 115200 || true
        stty sane 2>/dev/null || true   # restore the terminal if screen died abruptly
        sleep 1
    else
        echo "==> device ${PORT} not present; waiting for it to come back..."
        while [ ! -e "${PORT}" ]; do sleep 1; done
        echo "==> device back, reconnecting..."
    fi
done
