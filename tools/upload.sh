#!/usr/bin/env bash
# upload.sh — push python/main.py to the Pico, working around the tx console.
#
# Why this is not a plain `mpremote cp`:
#   main.py boots into the interactive tx console, which sits in input()
#   and catches Ctrl-C by design.  mpremote needs the plain MicroPython
#   REPL to upload files, so `mpremote cp` fails with
#   "could not enter raw repl" (the console swallows the handshake).
#
# This script:
#   1. resolves the board's CDC port by its USB IDs (tools/pico_port.sh,
#      1209:fa51) so another ttyACM device - a PlutoSDR, an ST-Link, a
#      second Pico - cannot be picked by mistake;
#   2. opens that port, probes the REPL state by sending CR (Enter) and
#      watching for the prompt:
#        "tx> "  -> the tx console is up: send `exit` (retried) so it
#                   returns to the plain MicroPython REPL;
#        ">>> "  -> already at the plain REPL: nothing to do;
#      a leftover raw-REPL state (from a previous mpremote session) is
#      exited with Ctrl-B first;
#   3. uploads main.py with `mpremote connect <port> resume fs cp`
#      (resume = no soft reset, so the console does not grab the REPL again);
#   4. resets the board with machine.reset() so the new main.py runs.
#
# Works in all states: console up, plain REPL, raw REPL leftover, or no
# main.py yet (first boot).
#
# Usage: ./upload.sh [--port /dev/ttyACMx] [path-to-main.py]
#        FM_PORT=/dev/ttyACMx ./upload.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PORT_OPT=""
SRC=""
while [ $# -gt 0 ]; do
    case "$1" in
        -p|--port) PORT_OPT="${2:-}"; shift 2 ;;
        -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) SRC="$1"; shift ;;
    esac
done
SRC="${SRC:-$(dirname "${SCRIPT_DIR}")/python/main.py}"

command -v mpremote >/dev/null 2>&1 || { echo "error: mpremote not found (pip install mpremote)"; exit 1; }
[ -f "${SRC}" ] || { echo "error: not found: ${SRC}"; exit 1; }

PORT="$(FM_PORT="${PORT_OPT}" "${SCRIPT_DIR}/pico_port.sh")" || exit 1
echo "==> board port: ${PORT}"

# Use the same Python interpreter mpremote runs on (it is guaranteed to have
# pyserial, which the console-exit step needs).
MPY="$(head -1 "$(command -v mpremote)" | sed 's/^#!//')"

# 1. Probe the REPL state and ask the tx console (if running) to exit.
echo "==> probing the REPL state..."
"${MPY}" - "${PORT}" <<'EOF'
import sys, time
import serial

port = sys.argv[1]
try:
    s = serial.Serial(port, 115200, timeout=0.2)
except PermissionError as e:
    sys.exit(
        "error: cannot open %s: %s\n"
        "       the port is root-owned; install the udev rule (once):\n"
        "         sudo install -m644 tools/99-pico-tx.rules /etc/udev/rules.d/99-pico-tx.rules\n"
        "         sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=tty\n"
        "       then replug the Pico. (quick fix: sudo chmod 666 %s)" % (port, e, port))
except Exception as e:
    sys.exit("error: cannot open %s: %s" % (port, e))

def drain(sec):
    end = time.time() + sec
    out = b""
    while time.time() < end:
        try:
            b = s.read(512)
        except Exception:
            time.sleep(0.1)
            continue
        if b:
            out += b
    return out

buf = drain(0.5)
if b"raw REPL" in buf:
    # Left over from a previous mpremote session: leave raw REPL (Ctrl-B).
    print("   leftover raw-REPL state, exiting it")
    s.write(b"\x02")
    buf += drain(0.3)

# Send CR to force a fresh prompt whatever the state, then look at it.
s.write(b"\r")
buf += drain(0.5)

sent = 0
state = None
for _ in range(5):
    if b">>>" in buf:
        state = "repl"
        break
    if b"tx>" in buf:
        if sent < 3:
            s.write(b"exit\r\n")
            sent += 1
            print("   tx console detected, sending 'exit' (try %d)" % sent)
        buf += drain(1.0)
        if b">>>" in buf:
            state = "repl"
            print("   console exited, back at the plain REPL")
            break
    else:
        buf += drain(1.0)

if state == "repl":
    print("   board is at the plain MicroPython REPL")
else:
    print("   WARNING: could not identify the REPL state; captured: %r" % buf)
    print("   (continuing - mpremote may still recover)")
s.close()
EOF

# 2. Upload without a soft reset (the board is at the plain REPL now).
echo "==> uploading $(basename "${SRC}") to :main.py"
mpremote connect "${PORT}" resume fs cp "${SRC}" :main.py

# 3. Reset so the new main.py runs.  The USB port vanishes mid-reset, which
#    makes mpremote throw an OSError (expected - the board is rebooting).
echo "==> resetting the board (USB port will briefly vanish)..."
mpremote connect "${PORT}" resume exec "import machine; machine.reset()" >/dev/null 2>&1 || true

echo "==> done - the new main.py is running. Open a serial terminal (115200)."
