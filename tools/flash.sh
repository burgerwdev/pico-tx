#!/usr/bin/env bash
# flash.sh — flash the firmware with picotool (or just drag-and-drop the UF2).
# Usage: ./flash.sh [path-to.uf2]   (default: firmware/rp2040pico_fm_firmware.uf2)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UF2="${1:-$(dirname "${SCRIPT_DIR}")/firmware/rp2040pico_fm_firmware.uf2}"

if ! command -v picotool >/dev/null 2>&1; then
    echo "picotool not found. Alternative: hold BOOTSEL while plugging in,"
    echo "then copy ${UF2} to the RPI-RP2 mass-storage drive."
    exit 1
fi
picotool load -x "${UF2}"
echo "flashed: ${UF2}"
