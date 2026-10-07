# Usage Guide

> [中文](../zh/usage.md) · English

## Flashing

1. Hold **BOOTSEL** while plugging the Pico into USB — an `RPI-RP2` drive
   appears.
2. Drag `firmware/rp2040pico_fm_firmware.uf2` onto it (auto-flash & reboot),
   or run `tools/flash.sh` (needs picotool).
3. Optional check (from the repo root): `sha256sum -c firmware/sha256.txt`.

## Connecting

- The device enumerates as a **serial port** (MicroPython REPL / `fm>` console)
  plus a **USB audio device** ("RP2040 RF Transmitter"; shown as
  RP2040 RF Transmitter / BurgerW).
- Recommended: `tools/serial.sh` (tio) — it auto-reconnects when the board
  reboots (band switches / presets do reboot), and a udev rule can pin the
  device name to `/dev/pico` so the ttyACM number never drifts.
- Manual: `screen /dev/ttyACM0 115200`; Windows: PuTTY (COM port, 115200);
  Thonny also works.
- Upload the console script once: `tools/upload.sh` (a plain `mpremote cp`
  fails while the `fm>` console holds the REPL).
- Afterwards, replug (or press RESET), wait 2-3 s, open the serial terminal —
  the `fm>` console appears automatically.

## Playing

1. In the OS sound settings, select **RP2040 RF Transmitter** as the output.
2. Play something.
3. Put an FM radio next to the Pico, tuned to **87.9 MHz**.

> Tip: 75µs pre-emphasis is on by default (matches the radio's de-emphasis).
> If it sounds distorted, try `pre off`, `dev 40000`, or lower the PC volume.

## Band presets & handheld radios

```
band fm    98.0 MHz    FM broadcast (parked-carrier silence, 15kHz audio)
band 2m    145.0 MHz   2m amateur — handheld VHF, WIDE mode
band 409   409.75 MHz  PRC license-free PMR, 3rd harmonic (filter needed*)
band 433   433.92 MHz  ISM/ham, 3rd harmonic — handheld UHF, WIDE mode
band 446   446.00625 MHz  PMR446 ch1, 3rd harmonic (EU)
```

Each preset sets the carrier/deviation, the PLL reference divider (every band
that keys the RF off when silent uses refdiv 2 — the half PDM step that makes
narrowband/harmonic links clean; broadcast FM keeps refdiv 1 because a parked
carrier with refdiv 2 has an audible idle tone), the
silence mode and the NFM voice-band audio automatically, then reboots.
Handheld-radio notes:

- Tune the radio to the **actual** emission (a few kHz off nominal — the
  crystal tolerance is multiplied by the harmonic).  `trim <±Hz>` shifts the
  emission to compensate (e.g. `trim -5000`); `trim 0` clears it.
- On UHF the audio is voice-band shaped (300Hz HP + 3kHz LP) and the
  pre-emphasis should be `pre 300` to match the radio's 300µs de-emphasis.
- When there is no audio the RF output is keyed off (squelch closes ->
  silence).  `silence park` restores broadcast behaviour.
- \* The 409MHz fundamental sits at 136.6MHz inside the AERONAUTICAL band
  (118-137MHz) and radiates stronger than the 409MHz signal — do NOT radiate
  there without a 409MHz band-pass filter suppressing the fundamental.

## FM silence best practice

An unmodulated parked carrier is what silences an FM radio (pause / `mute on`
/ `audio off`).  If the host KEEPS streaming while muted, its USB packet
activity couples a faint buzz into the parked carrier (inherent to the
chip — same on the original firmware).  For perfect FM silence, pause the
host playback.  Narrowband bands are unaffected (RF keyed off).

## LED

- Default (plain-LED boards) `led 3`: audio-level VU; `led 0/1/2`: off /
  always-on / on-while-streaming.
- Boards without an LED on GPIO25 (e.g. RP2040-Zero's WS2812 on GPIO16):
  `ledpin ws2812` enables NeoPixel mode (`led 3` = green→yellow→red VU), or
  attach a plain LED and set its pin with `ledpin <gpio>`.

## Changing band

One-command presets: `band fm` / `band 2m` / `band 409` / `band 433` /
`band 446` (see above).  Custom: `reinit 98000000 50000` (carrier /
deviation, optional 3rd arg = RF pin) saves and reboots; `freq <Hz>`
fine-tunes live inside the current PLL band, and reboots to the target
carrier when outside it.  `trim <±Hz>` compensates the crystal offset.
