#!/usr/bin/env sh
# pico_port.sh — print the RP2040 FM transmitter's USB CDC port.
#
# Why not "the first /dev/ttyACM*": any other attached device (a PlutoSDR's
# Linux serial console, an ST-Link, a second Pico) would be picked and the
# upload would fail with "could not enter raw repl" or a stray login prompt.
#
# Note the USB IDs below belong to the *firmware*, not to a board model:
# MICROPY_HW_USB_VID/PID = 0x1209/0xFA51 lives in
# ports/rp2/boards/RPI_PICO_TX/mpconfigboard.h, so a Pico, a Pico W, an
# RP2040-Zero or a clone running this firmware all enumerate as 1209:fa51.
#
# Resolution order:
#   1. $FM_PORT                              explicit override
#   2. VID:PID == 1209:fa51                  this firmware (any RP2040 board)
#   3. generic MicroPython RP2040            board not yet flashed with this
#      (2e8a:0005, or a "MicroPython"        firmware (stock MicroPython,
#       manufacturer/product string)        recovery, another build)
#   4. /dev/pico                             udev symlink
#   5. /dev/serial/by-id/*RP2040*            string fallback
#
# Tiers 2 and 3 must be unambiguous: if several devices match, the candidates
# are listed and the caller is asked to pass $FM_PORT (never guess).
# A tier-3 match prints a note to stderr, since it is not this firmware.
#
# Self-check (no hardware):  tools/pico_port.sh --self-test
set -eu

# Overridable for the self-test:
#   PICO_SYSFS        sysfs tty class directory
#   PICO_NO_FALLBACK  set to skip /dev/pico and /dev/serial/by-id
SYS_TTY="${PICO_SYSFS:-/sys/class/tty}"

# Echo every ttyACM/ttyUSB device matching tier $1 ("exact" or "generic").
collect() {
    for d in "${SYS_TTY}"/ttyACM* "${SYS_TTY}"/ttyUSB*; do
        [ -e "${d}" ] || continue
        vid="$(cat "${d}/device/../idVendor" 2>/dev/null || true)"
        pid="$(cat "${d}/device/../idProduct" 2>/dev/null || true)"
        mfg="$(cat "${d}/device/../manufacturer" 2>/dev/null || true)"
        prod="$(cat "${d}/device/../product" 2>/dev/null || true)"
        case "$1" in
            exact)
                [ "${vid}:${pid}" = "1209:fa51" ] && echo "/dev/$(basename "${d}")"
                ;;
            generic)
                if [ "${vid}:${pid}" = "2e8a:0005" ] || \
                   printf '%s %s' "${mfg}" "${prod}" | grep -qi "micropython"; then
                    echo "/dev/$(basename "${d}")"
                fi
                ;;
        esac
    done
    return 0
}

unit() {
    echo "$1" | sed '/^$/d'
}

list_candidates() {
    for d in "${SYS_TTY}"/ttyACM* "${SYS_TTY}"/ttyUSB*; do
        [ -e "${d}" ] || continue
        vid="$(cat "${d}/device/../idVendor" 2>/dev/null || true)"
        pid="$(cat "${d}/device/../idProduct" 2>/dev/null || true)"
        mfg="$(cat "${d}/device/../manufacturer" 2>/dev/null || true)"
        prod="$(cat "${d}/device/../product" 2>/dev/null || true)"
        echo "         /dev/$(basename "${d}")  ${vid}:${pid}  ${mfg} ${prod}"
    done
}

fail() {
    {
        echo "error: $1"
        echo "       candidates seen:"
        list_candidates
        echo "       pass one explicitly:  FM_PORT=/dev/ttyACM1 $0"
        echo "       (in BOOTSEL mode there is no CDC port at all)"
    } >&2
    return 1
}

find_port() {
    if [ -n "${FM_PORT:-}" ]; then
        echo "${FM_PORT}"
        return 0
    fi

    exact="$(unit "$(collect exact)")"
    if [ -n "${exact}" ]; then
        count="$(echo "${exact}" | wc -l)"
        [ "${count}" -eq 1 ] && { echo "${exact}"; return 0; }
        fail "several boards running this firmware are attached." || return 1
    fi

    generic="$(unit "$(collect generic)")"
    if [ -n "${generic}" ]; then
        count="$(echo "${generic}" | wc -l)"
        if [ "${count}" -eq 1 ]; then
            echo "note: ${generic} is not running this firmware (generic" >&2
            echo "      MicroPython RP2040) - using it anyway." >&2
            echo "${generic}"
            return 0
        fi
        fail "several MicroPython RP2040 devices are attached." || return 1
    fi

    if [ -z "${PICO_NO_FALLBACK:-}" ]; then
        [ -e /dev/pico ] && { echo /dev/pico; return 0; }
        for d in /dev/serial/by-id/*RP2040*; do
            [ -e "${d}" ] && { echo "${d}"; return 0; }
        done
    fi

    fail "RP2040 MicroPython board not found (USB 1209:fa51 or 2e8a:0005)."
}

self_test() {
    tmp="$(mktemp -d)"
    trap 'rm -rf "${tmp}"' EXIT

    add() {   # add <ttyACMx> <vid> <pid> <mfg> <product>
        mkdir -p "${tmp}/$1/device"
        printf '%s\n' "$2" > "${tmp}/$1/idVendor"
        printf '%s\n' "$3" > "${tmp}/$1/idProduct"
        printf '%s\n' "$4" > "${tmp}/$1/manufacturer"
        printf '%s\n' "$5" > "${tmp}/$1/product"
    }

    check() {   # check <expected|-> <label> ...
        got="$(env -u FM_PORT PICO_SYSFS="${tmp}" PICO_NO_FALLBACK=1 \
              "$0" 2>/dev/null || true)"
        if [ "$1" = "-" ]; then
            [ -z "${got}" ] || { echo "FAIL ($2): expected none, got '${got}'" >&2; exit 1; }
        else
            [ "${got}" = "$1" ] || { echo "FAIL ($2): expected $1, got '${got}'" >&2; exit 1; }
        fi
    }

    # a PlutoSDR console plus our board on a different tty number
    add ttyACM0 0456 b673 "Analog Devices Inc." "PlutoSDR (ADALM-PLUTO)"
    add ttyACM1 1209 fa51 "BurgerW" "pico-tx"
    check /dev/ttyACM1 "picks 1209:fa51 among candidates"

    # the same, but the board is an RP2040-Zero: identical firmware IDs
    add ttyACM1 1209 fa51 "BurgerW" "pico-tx"
    check /dev/ttyACM1 "board model does not matter (IDs come from firmware)"

    # board not flashed with this firmware yet: stock MicroPython
    rm -rf "${tmp}/ttyACM1"
    add ttyACM1 2e8a 0005 "MicroPython" "Board in FS mode"
    check /dev/ttyACM1 "falls back to a generic MicroPython RP2040"

    # two boards running this firmware -> never guess
    add ttyACM1 1209 fa51 "BurgerW" "pico-tx"
    add ttyACM2 1209 fa51 "BurgerW" "pico-tx"
    check - "ambiguous matches are not guessed"

    # explicit override wins, and absence fails
    got="$(PICO_SYSFS="${tmp}" PICO_NO_FALLBACK=1 FM_PORT=/dev/ttyACM9 "$0")"
    [ "${got}" = "/dev/ttyACM9" ] || { echo "FAIL: FM_PORT ignored" >&2; exit 1; }
    rm -rf "${tmp}/ttyACM1" "${tmp}/ttyACM2"
    env -u FM_PORT PICO_SYSFS="${tmp}" PICO_NO_FALLBACK=1 "$0" >/dev/null 2>&1 \
        && { echo "FAIL: expected non-zero exit when absent" >&2; exit 1; }

    echo "pico_port.sh self-test: OK (firmware IDs, generic fallback,"
    echo "                        ambiguity, FM_PORT override, absent)"
}

case "${1:-}" in
    --self-test) self_test ;;
    *) find_port ;;
esac
