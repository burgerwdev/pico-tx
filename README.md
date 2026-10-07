# pico-tx

**Version 0.1.0** — RP2040 general-purpose RF transmitter

[中文](README.zh.md) · English · [Project](https://git.sr.ht/~bytewolf/pico-tx) · based on [rp2040-fm-transmitter](https://git.sr.ht/~bytewolf/rp2040-fm-transmitter)

Turn a Raspberry Pi Pico (RP2040) into a **general-purpose RF transmitter**
built on the [pico-fractional-pll](https://github.com/kaduhi/pico-fractional-pll)
technique (RF square wave on GPIO21, no DAC/analog stage):

- **All modulation runs in firmware** (the 48 kHz PWM ISR); MicroPython is
  only the control layer and never touches the real-time path;
- A **USB sound card** (UAC1, 48kHz/16-bit/stereo) is one of several signal
  sources: whatever the PC plays is FM-modulated onto the carrier;
- Additional transmit schemes selectable from the `tx>` console: internal
  tone (DDS), FSK, OOK, CW (Morse), linear chirp and BPSK/QPSK;
- Carrier configurable across broadcast FM 88-108M, 2m amateur 144-148M and
  UHF 409/433/440M (via the 3rd/5th harmonic of a <=150MHz fundamental).

| mode | source | modulation | typical use |
|------|--------|------------|-------------|
| `fm` | USB audio | FM | play PC audio to an FM radio |
| `tone` | internal DDS | FM | test tone / CTCSS / DTMF |
| `fsk` | symbol FIFO | 2-FSK | RTTY / packet / paging |
| `ook` | symbol FIFO | on/off keying | remote controls |
| `cw` | key line | Morse | beacon / keyed carrier |
| `chirp` | internal | linear sweep | FMCW / sweep testing |
| `psk` | symbol FIFO | BPSK/QPSK | constant-envelope data |

> **⚠️ LEGAL WARNING**: GPIO21 drives a strong RF square wave. **Do NOT attach
> an antenna.** Unlicensed radiation is illegal in most countries. For testing,
> put an FM radio within a few centimetres of the Pico.  On the UHF harmonic
> bands the *fundamental* also radiates: for 409MHz it sits in the AERONAUTICAL
> band (118-137MHz) - a band-pass filter is mandatory before radiating there.
> Digital schemes (FSK/OOK/PSK) have wider spectra than narrowband FM; check
> the harmonics and splatter before any radiated use.

---

## Quick start

1. Flash `firmware/picotx_firmware.uf2` (BOOTSEL drag & drop, or
   `tools/flash.sh`);
2. Upload the console script:
   ```
   tools/upload.sh
   ```
   (This handles the console holding the REPL - a plain `mpremote cp`
   fails with "could not enter raw repl".  Equivalent manual steps: in a
   serial terminal type `exit` at the `tx>` prompt, then
   `mpremote resume fs cp python/main.py :main.py`.)
3. Replug (or press RESET), wait 2-3 s, open a serial terminal (115200) — the
   `tx>` console appears automatically;
4. On the PC select **"pico-tx"** as the audio output and play;
5. Tune an FM radio to **87.9 MHz**.

Transmitting without a PC (`tone`/`fsk`/`cw`/`chirp`/`psk`):

```
mode tone            # internal signal source
tone 1000            # 1 kHz FM test tone
mode cw              # Morse
cw CQ CQ DE PICO TX
```

One-command band presets (save + reboot):

```
band fm    -> 98.0 MHz FM broadcast (parked-carrier silence)
band 2m    -> 145.0 MHz, handheld radio VHF WIDE mode
band 433   -> 433.92 MHz on the 3rd harmonic (UHF WIDE mode)
band 409   -> 409.75 MHz license-free PMR on the 3rd harmonic
```

For a handheld radio use **WIDE (25kHz)** mode and keep the deviation around
12kHz; the console sets `refdiv 2` automatically on every band that keys the RF
off when silent (the half PDM step is what makes the harmonic links sound
clean).  Broadcast FM keeps `refdiv 1` because a parked carrier with refdiv 2
has an audible idle tone.  When there is no audio the
narrowband bands key the RF output off (PTT-style) so the handheld squelch
closes instead of hearing an off-tune parked carrier - use `silence park` to
restore the broadcast behaviour.  For a serial session that survives reboots:
`tools/serial.sh` (tio auto-reconnect; a udev rule pins `/dev/pico`).

See [docs/en/usage.md](docs/en/usage.md) and
[docs/en/commands.md](docs/en/commands.md).

## Repository layout

```
release/
├── README.md / README.zh.md   # English (default) + Chinese
├── LICENSE                    # MIT + third-party licence notes
├── build.sh                   # one-shot build (clone MicroPython + patch + build)
├── patches/
│   └── micropython-tx.patch   # all our changes on top of MicroPython v1.29.0
├── firmware/
│   ├── picotx_firmware.uf2   # prebuilt firmware
│   └── sha256.txt
├── python/
│   └── main.py                # console script (copy to the board)
├── tools/
│   ├── flash.sh               # picotool helper
│   ├── upload.sh              # upload python/main.py (console-aware)
│   ├── serial.sh              # serial terminal (auto-reconnect)
│   ├── pico_port.sh           # resolve the board's USB CDC port
│   ├── pll_range.py           # PLL range / PDM-step enumeration
│   ├── audio_quality.py       # host-side fixed-point audio-chain model
│   ├── design_filters.py      # Chebyshev design + Q15 verification
│   └── make_test_audio.py     # test signal generator
└── docs/
    ├── README.md              # documentation index (both languages)
    ├── en/                    # English docs
    │   ├── usage.md           # usage guide
    │   ├── commands.md        # console command reference
    │   ├── technical.md       # technical architecture
    │   ├── audio-quality.md   # audio-quality layer audit
    │   └── troubleshooting.md # troubleshooting
    └── zh/                    # 中文文档
        ├── usage.md           # 使用说明
        ├── commands.md        # 指令使用指南
        ├── technical.md       # 技术文档
        ├── audio-quality.md   # 音质分层审计
        └── troubleshooting.md # 故障排查
```

## Build from source

```bash
git clone git@git.sr.ht:~bytewolf/pico-tx
cd pico-tx
./build.sh            # default MicroPython v1.29.0
# or ./build.sh <tag-or-commit>
```
The script clones a clean MicroPython, applies the patch, fetches submodules
and builds; the artifact lands in `firmware/`. It is fully automated — one
command produces the UF2 and its `sha256.txt`. Details in
[docs/en/technical.md](docs/en/technical.md).

**Prerequisites (Ubuntu/Debian)** — the host needs the RP2040 cross-toolchain
before running `build.sh`:

```bash
sudo apt install -y build-essential git cmake \
    gcc-arm-none-eabi libnewlib-arm-none-eabi \
    libstdc++-arm-none-eabi-newlib python3
```

`mpremote` (upload `main.py`) and `picotool` (`tools/flash.sh`) are optional
and only needed for uploading/flashing. See
[docs/en/technical.md](docs/en/technical.md) for details.

## Documents

| Doc | Contents |
|-----|----------|
| [docs/en/usage.md](docs/en/usage.md) | flashing, connecting, playing, LED |
| [docs/en/commands.md](docs/en/commands.md) | full `fm>` command reference |
| [docs/en/technical.md](docs/en/technical.md) | architecture: USB audio → ring buffer → 48kHz modulation → PLL-PDM |
| [docs/en/troubleshooting.md](docs/en/troubleshooting.md) | common issues (Windows drivers, no signal, shell) |

Chinese versions live under [docs/zh/](docs/zh/); see the
[docs index](docs/README.md).

## Credits & licences

- [MicroPython](https://github.com/micropython/micropython) (MIT) — our changes
  are shipped as a patch;
- [pico-fractional-pll](https://github.com/kaduhi/pico-fractional-pll)
  (BSD-3-Clause, Kazuhisa Terasaki) — **its core source is incorporated into
  our patch** (`ports/rp2/tx/pico_fractional_pll.c/.h`); keep the
  attribution; it is not a separate build dependency;
- [kaduhi/pico-playground (fm_transmitter branch)](https://github.com/kaduhi/pico-playground)
  — **reference only** (UAC1 descriptors and FM modulation ideas); no code
  from it is included;
- This project: see [LICENSE](LICENSE).

## Dependencies & build sources

The build depends only on **MicroPython** (with its own submodules
pico-sdk/tinyusb/mbedtls). `build.sh` tries these sources in order:
1. `https://git.sr.ht/~bytewolf/micropython` (maintainer's fork, for
   availability);
2. `https://github.com/micropython/micropython` (upstream).

> On "why not a git submodule": for this project the **patch + clone script**
> fits better — MicroPython is large with its own nested submodules, so a
> top-level submodule still needs recursive init; a patch keeps "what we
> changed" visible, applies to any version/fork, and does not pin the project
> to one fork's maintenance cadence. If you prefer submodules, apply the patch
> inside your fork and reference it with `git clone --recurse-submodules`;
> build.sh already skips the patch when it is already applied.
