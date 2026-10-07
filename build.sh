#!/usr/bin/env bash
# ============================================================================
# build.sh — one-shot builder for the RP2040 Pico FM Transmitter firmware.
# No CI needed: clones a clean MicroPython, applies our patch, builds the UF2.
#
# MicroPython source order (first reachable wins):
#   1. https://git.sr.ht/~bytewolf/micropython   (maintainer's fork)
#   2. https://github.com/micropython/micropython (upstream)
#
# Usage:
#   ./build.sh                 # default ref: v1.29.0
#   ./build.sh v1.29.0         # a tag or branch
#   ./build.sh <commit-sha>    # an exact commit (clones then checks out)
#
# Output: firmware/rp2040pico_fm_firmware.uf2 (+ sha256.txt)
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MPY_REF="${1:-v1.29.0}"
WORK="${SCRIPT_DIR}/build_tmp"
PATCH="${SCRIPT_DIR}/patches/micropython-fm.patch"
OUT_DIR="${SCRIPT_DIR}/firmware"

MPY_REPOS=(
    "https://git.sr.ht/~bytewolf/micropython"
    "https://github.com/micropython/micropython"
)

echo "==> Building RPI_PICO_FM firmware, MicroPython ref: ${MPY_REF}"

rm -rf "${WORK}"
mkdir -p "${WORK}"
cd "${WORK}"

# 1+2. Clone MicroPython from the first source whose checkout accepts our
# patch (or already contains it).  A commit SHA cannot be used with --branch,
# so fall back to cloning the default branch and checking the commit out.
SOURCE=""
patch_ready() {
    # returns 0 if the patch applies cleanly, 1 if it is already applied
    if git -C micropython apply --check "${PATCH}" >/dev/null 2>&1; then
        return 0
    fi
    if git -C micropython apply --reverse --check "${PATCH}" >/dev/null 2>&1; then
        return 1
    fi
    return 2
}
for repo in "${MPY_REPOS[@]}"; do
    echo "    trying ${repo} ..."
    rm -rf micropython
    if ! git clone --quiet --depth 1 --branch "${MPY_REF}" "${repo}" micropython 2>/dev/null \
        && ! { git clone --quiet "${repo}" micropython 2>/dev/null \
               && git -C micropython checkout --quiet "${MPY_REF}" 2>/dev/null; }; then
        continue
    fi
    case "$(patch_ready; echo $?)" in
        0)
            git -C micropython apply "${PATCH}"
            echo "    patch applied"
            SOURCE="${repo}"
            break
            ;;
        1)
            echo "    patch already applied (skipped)"
            SOURCE="${repo}"
            break
            ;;
        *)
            echo "    patch does not apply to this checkout - trying next source"
            ;;
    esac
done
if [ -z "${SOURCE}" ]; then
    echo "ERROR: no MicroPython source matched ref ${MPY_REF} and the patch."
    exit 1
fi
echo "    source: ${SOURCE}"

# 3. Fetch the rp2 submodules (pico-sdk, tinyusb, mbedtls, ...).
make -C micropython/ports/rp2 submodules

# 4. Build.
make -C micropython/ports/rp2 BOARD=RPI_PICO_FM -j"$(nproc)"

# 5. Collect the artifact.
mkdir -p "${OUT_DIR}"
cp micropython/ports/rp2/build-RPI_PICO_FM/firmware.uf2 \
    "${OUT_DIR}/rp2040pico_fm_firmware.uf2"
VERSION="$(cat "${SCRIPT_DIR}/VERSION" 2>/dev/null || echo unknown)"
FW_HASH="$(sha256sum "${OUT_DIR}/rp2040pico_fm_firmware.uf2" | awk '{print $1}')"
{
    echo "# RP2040 FM Transmitter - v${VERSION}"
    echo "# Prebuilt firmware for MicroPython v1.29.0 (patch micropython-fm.patch)."
    echo "# Verify from the repo root:  sha256sum -c firmware/sha256.txt"
    echo "${FW_HASH}  firmware/rp2040pico_fm_firmware.uf2"
} > "${OUT_DIR}/sha256.txt"

# 6. Keep the console's FW_SHA256 in sync with the built firmware (the
#    console displays it via `ver` so a mismatch is instantly visible).
sed -i "s/^FW_SHA256 = \".*\"/FW_SHA256 = \"${FW_HASH}\"/" "${SCRIPT_DIR}/python/main.py"
echo "==> main.py FW_SHA256 synced: ${FW_HASH}"

echo
echo "==> Done: ${OUT_DIR}/rp2040pico_fm_firmware.uf2"
cat "${OUT_DIR}/sha256.txt"
