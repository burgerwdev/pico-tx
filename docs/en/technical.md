# Technical Architecture

> [中文](../zh/technical.md) · English

## System architecture

pico-tx separates **signal source**, **modulation scheme** and **channel**.
All three meet in one place: the 48 kHz PWM-wrap ISR, which is the only
real-time code. MicroPython only configures it and feeds data.

```
  source                     scheme (ISR dispatch)          channel
  ───────                    ────────────────────          ────────
  USB UAC1 audio  ─┐                                         carrier ± deviation
    stereo→mono,   │                                         harmonic (x1/3/5)
    volume/mute,   │
    15 kHz LP +    ├─►  switch (s_scheme):
    pre-emphasis,  │      FM    : f = carrier + sample·dev/32768
    limiter        │      TONE  : DDS sine(s) -> f
  ─────────────────┘      FSK   : symbol -> carrier ± shift
  internal DDS tone       OOK   : symbol -> RF enable on/off
  symbol FIFO (FSK/OOK/   CW    : key   -> RF enable on/off
    PSK), 256 entries     CHIRP : f = f0 + (f1-f0)·t/dur
  manual key line         PSK   : phase accumulator -> f
                          ─────────────┬───────────────
                                       │
  ring buffer (FM only) ──────────────┤
                                       ▼
  PWM slice 7 wrap IRQ @ exactly 48 kHz (48 MHz/1000, top NVIC priority)
    -> pico_fractional_pll_set_freq_u32(freq)  and/or  enable_output(key)
    -> Core1 1 MHz PDM dithers PLL_SYS fbdiv_int
    -> CLK_GPOUT0 -> GPIO21 RF square wave
```

FM audio path detail:

```
PC (48 kHz stereo 16-bit PCM)
  → USB UAC1 audio RX callback (TinyUSB / tud_task soft IRQ)
  → stereo→mono mix + volume + mute
  → 15 kHz band-limit + 75 µs pre-emphasis + limiter (fixed-point, no FPU)
  → SPSC ring buffer (4096 samples)
  → PWM slice 7 wrap IRQ @ 48 kHz
  → pico_fractional_pll_set_freq_u32(carrier + sample·dev/32768)
  → CLK_GPOUT0 → GPIO21
```

- The hard-real-time path never passes through Python; MicroPython only
  configures and controls it.
- clk_sys is fixed at **48 MHz** (a hard requirement of pico-fractional-pll:
  clk_sys from PLL_USB, PLL_SYS dedicated to the RF output).
- Only **fm** consumes the USB ring buffer; the other schemes synthesise their
  waveform in the ISR and never touch the ring.

## Transmit schemes

| scheme | ISR action | source | notes |
|---|---|---|---|
| `fm` | freq = carrier + sample·dev/32768 | USB ring | original behaviour; silence gate / pre-emphasis / squelch apply here only |
| `tone` | DDS sine(s) → freq | internal (up to 2 tones) | 256-entry Q14 LUT, linear interpolation; test tone / CTCSS / DTMF |
| `fsk` | freq = carrier ± shift | symbol FIFO | shift from `fsk_config`, `0`→-shift `1`→+shift |
| `ook` | RF enable = bit | symbol FIFO | `0`→RF off |
| `cw` | RF enable = key | `key(bool)` | Morse timing driven from Python (20 wpm) |
| `chirp` | freq = linear ramp | internal | `f0..f1` over `duration_ms`, optional gap + repeat |
| `psk` | freq = Δphase/tsym | symbol FIFO | order 2 (BPSK) / 4 (QPSK); phase lands on the constellation point |

A shared symbol FIFO (256 entries) feeds FSK/OOK/PSK; the symbol clock is a
counter in the ISR (`sps = 48000/baud`).  Because the PLL only accepts
frequency, the reachable modulation space is constant-envelope
frequency/phase modulation plus on/off keying with a coarse (±2/4/8/12 mA)
drive-strength amplitude - there is no linear AM/SSB path.

## Key engineering points

### 1. USB audio (UAC1)

- `CFG_TUD_AUDIO` is enabled in the built-in TinyUSB config; the configuration
  descriptor carries a UAC1 block (IAD + AC + AS alt0/alt1, 48 kHz stereo
  16-bit, ISO OUT EP 0x03, synchronous);
- Volume/mute control requests are handled in `tud_audio_*_req_entity_cb`;
- The audio callbacks must be compiled in: `#include "tusb.h"` has to come
  before `#if CFG_TUD_AUDIO`.

### 2. 48 kHz sample clock

- PWM slice 7 (TOP=999, div 1.0) provides an exact jitter-free 48 kHz timebase
  (the µs timer cannot do exact 48 kHz);
- **PWM_IRQ_WRAP must run at the top NVIC priority (0)**: at the default
  priority it was starved by the USB IRQ (only ~26 kHz, audio slowed +
  distorted);
- **No 64-bit division in the ISR**: `acc = (freq−low)·k >> 32` with
  `k = 2⁶⁴/Δf` precomputed at init (M0+ has no FPU; division is very slow).

### 3. Core1 PDM and clocks

- The library's Core1 loop dithers the PLL feedback divider at 1 MHz (PDM) and
  requires clk_sys = 48 MHz;
- MicroPython's linker gives SCRATCH_X 0 bytes, so the library's
  `multicore_launch_core1` (which needs `.stack1`) panics — we use
  `multicore_launch_core1_with_stack` with our own 2 KB stack in plain RAM;
- Do not use `_thread` together with `pico_tx` (both need Core1).

## Narrowband FM reception (handheld radios)

The fractional PLL dithers fbdiv_int between two adjacent values at 1 MHz; the
PLL's analog loop filter averages the pattern, but imperfectly.  The residual
frequency ripple is what sets the usable SNR:

- Broadcast FM (87.9MHz, 75kHz deviation, 230kHz IF): the ripple stays inside
  the IF passband and is rejected by the 15kHz audio LPF -> clean.
- The ripple scales with the PDM step (12MHz/total_div at the fundamental).
  REFDIV=2 halves the step (6MHz per fbdiv LSB), which also lowers the PLL
  loop bandwidth (PFD 12->6MHz) so the 1MHz dither is averaged more strongly.
  This is the key to narrowband reception.

**Verified recipe (hardware-tested, handheld radio in WIDE/25kHz mode):**

| band | config | result |
|---|---|---|
| 2m 144-148MHz (fundamental) | `reinit 145000000 12000 21` | clear, ~10dB SNR |
| 433.92MHz (3rd harmonic of 144.64MHz) | `reinit 433920000 12500 21` + `refdiv 2` | **clear** |
| 409.75MHz (3rd harmonic of 136.58MHz) | `reinit 409750000 12500 21` + `refdiv 2` | clear, slightly noisier |

A UHF target (above the ~150MHz fundamental ceiling) is carried on the 3rd
odd harmonic of a lower fundamental: the PLL runs on target/3 and the radio
hears target (deviation is scaled x3 too).  REFDIV=2 halves the fundamental
PDM step to ~0.6MHz (vs 1.2MHz at refdiv 1), which is why the UHF harmonic
links sound cleaner than the 2m fundamental at refdiv 1.

- `pdm >1MHz` was hardware-tested WORSE (M0+ systick latency and PLL write
  timing break down) - keep 1.
- `refdiv 2` is neutral on 2m but essential for the UHF harmonic recipe.
- A live PLL re-init without reboot (deinit/init) DEADLOCKS the board -
  pdm/refdiv persist and apply at the next boot.
- The emission is a few kHz off nominal (crystal tolerance x the harmonic);
  tune the radio to the actual signal.
- Legal/safety: the 409MHz fundamental (136.6MHz) is inside the AERONAUTICAL
  band (118-137MHz) and radiates stronger than the 409MHz signal - suppress
  it with a band-pass filter for any sustained use.  433MHz's fundamental is
  in the 2m amateur band.

### PLL reference divider (refdiv) policy

The instantaneous frequency hops between two adjacent feedback-divider values
on the 1 MHz PDM tick with `step = ref/div`; that step is the hard floor of
the residual RF ripple.  `refdiv 2` lowers the reference from 12 MHz to
6 MHz and **halves the step on every band** (`tools/pll_range.py bands`):

| Band | Frequency | step at refdiv 1 | step at refdiv 2 |
|---|---|---|---|
| 160m | 1.840 MHz | unreachable | **7 kHz** |
| 80m | 3.573 MHz | unreachable | **16 kHz** |
| 40m | 7.074 MHz | 80 kHz | **30 kHz** |
| 30m | 10.136 MHz | 80 kHz | **40 kHz** |
| 20m | 14.074 MHz | 120 kHz | **60 kHz** |
| 10m | 28.074 MHz | 240 kHz | **120 kHz** |
| 2m | 145.000 MHz | 1.2 MHz | **600 kHz** |
| 433.92 MHz (x3) | 144.640 MHz | 1.2 MHz | **600 kHz** |
| FM broadcast | 87.900 MHz | 750 kHz | 375 kHz (but see below) |

**refdiv 2 must not be used where the carrier is parked, though.**  Broadcast
FM (87.5–108 MHz) parks the unmodulated carrier on fc when silent; with
refdiv 2 the parked PDM pattern changes and puts an audible idle tone on the
silent carrier (hardware-observed, the v0.22.x FM silence regression).  The
rule is therefore:

```
refdiv_for(target) = 2 if (the band keys the RF off when silent) else 1
```

ie broadcast FM takes 1; everything else (2m, UHF harmonics, narrowband HF)
takes 2.  The console gained `refdiv <1|2|auto>` (default auto); an explicit
1/2 already in the config wins.  Because refdiv 2 narrows the reachable PLL
window, boot-time `init` failures fall back to refdiv 1 automatically (a
failed `init` never launches core1, so the retry is safe - unlike the
"deinit/init while running deadlocks" case) and the 1 is persisted so the
probe does not repeat on every boot.

`status` now shows the actual step (the width of the window `pico_tx.range()`
returns is exactly one feedback-divider step).  In addition,
`tools/pll_range.py minstep` verifies that the solution the C
`calculate_pll_divider()` returns **already has the smallest step within its
pass** (559 HF windows + 129 UHF windows, 0 counterexamples), so the PLL
search itself needs no change.

### 5. Diagnostics

- `diag`: ISR ticks, RX frames, and the ring drift counters (underflow = host
  clock slower, repeats the last sample on an empty ring; drop = host clock
  faster, ring full);
- `pwm`: clk_sys, PWM registers, ISR cost (2–6 µs is normal);
- `pll`: PLL ready / actual output range / last written frequency;
- `ring`: ring fill % (near 0% when balanced; 99% means the consumer stalled).

## How the build works

`build.sh` clones a clean MicroPython (tries the maintainer's fork
`git.sr.ht/~bytewolf/micropython` first, then upstream) → applies
`patches/micropython-tx.patch` (skips if already applied) → `make submodules`
→ `make BOARD=RPI_PICO_TX`. The patch contains:
- `ports/rp2/boards/RPI_PICO_TX/` (new board: 48 MHz clock, USB_AUDIO, FM macros,
  unique VID/PID 0x1209:0xFA50, USB strings);
- `ports/rp2/tx/` (pico_tx user C module: **the bundled
  pico-fractional-pll source** (BSD-3-Clause, Kazuhisa Terasaki) + modulator +
  MicroPython module);
- `ports/rp2/main.c` (48 MHz boot), `modmachine.c` (disable machine.freq setter);
- `shared/tinyusb/` (UAC1 descriptors and config).

[kaduhi/pico-playground (fm_transmitter branch)](https://github.com/kaduhi/pico-playground)
is reference only (UAC1 descriptor layout and FM modulation ideas); no code
from it is included.

### Build prerequisites (Ubuntu/Debian)

`build.sh` is a single automated command, but the host must have the RP2040
cross-toolchain installed. On Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y \
    build-essential git \
    cmake \
    gcc-arm-none-eabi \
    libnewlib-arm-none-eabi \
    libstdc++-arm-none-eabi-newlib \
    python3
```

- `build-essential` — `make`, `gcc` and friends (drives the build);
- `git` — `build.sh` clones MicroPython (required, no tarball fallback);
- `cmake` — the pico-sdk build system;
- `gcc-arm-none-eabi` — the ARM Cortex-M0+ cross compiler (>= 10 is fine);
- `libnewlib-arm-none-eabi` + `libstdc++-arm-none-eabi-newlib` — newlib C
  library / C++ runtime for the target, required by the pico-sdk;
- `python3` — used by MicroPython's build scripts.

Optional, only for uploading/flashing (not needed by `build.sh` itself):
`mpremote` (`pip install mpremote`, upload `main.py`) and `picotool`
(`tools/flash.sh`). Check the toolchain with
`arm-none-eabi-gcc --version` (expect 10.3.x or newer).

## Regenerating the patch

```bash
cd micropython
git add -A
git diff --cached > ../release/patches/micropython-tx.patch
git reset -q
```
