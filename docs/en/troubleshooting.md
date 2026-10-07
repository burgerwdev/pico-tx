# Troubleshooting

> [中文](../zh/troubleshooting.md) · English

## Windows: device with an exclamation mark / no sound card

**Cause**: Windows caches driver profiles per VID/PID. The stock MicroPython
PID (0x2E8A:0x0005, CDC-only) conflicts with the CDC+Audio composite — this
firmware uses a unique identity (0x1209:0xFA50). If leftover devices appear:

1. Device Manager → View → Show hidden devices;
2. Uninstall the error devices ("delete driver software");
3. Re-flash and replug.

## No RF signal

1. Confirm the `fm>` console is up; `status` shows `PLL ready: yes` and
   `RF output: ON`;
2. `diag`: ISR/RX should both be ~48000/s; if the ISR is ~26000/s, the sample
   IRQ was starved — flash a build with the top-priority IRQ fix;
3. `sweep 87000000 88500000 500000` — the SA signal should move (PLL/Core1
   self-test);
4. Probe **GPIO21** directly (a high-Z probe sees ~87.9 MHz + harmonics);
5. Put the radio next to the Pico, tuned to 87.9 MHz (dial error of ±100 kHz
   is common).

## Console does not appear

1. Make sure `main.py` is at the root of the board filesystem
   (`import os; os.listdir('/')` in the REPL);
2. Use the latest `main.py` (old versions were killed by Thonny's Ctrl-C);
3. After replug wait 2–3 s before connecting (USB enumeration + banner wait);
4. `screen`/PuTTY/minicom are more reliable than Thonny; don't press Run in
   Thonny after the console is up.

## Bad audio (distortion / wrong speed / dropouts)

- Run `diag` — the ISR must be ≈48000/s (lower = starved IRQ, update firmware);
- A/B with `pre off`; try `dev 40000`;
- Keep the PC volume at 60–80% to avoid constant limiting;
- clicks after a long listen: the USB host clock and the PWM sample clock are
  not the same source.  This firmware now repeats the previous sample on an
  underflow and counts it — watch the Underflows/Drops deltas in `diag` (a
  delta of 0 means the two crystals happen to match).  A growing count means a
  ppm-level offset: the audible click is gone, but removing the timing error
  entirely needs an ASRC or a UAC1 feedback endpoint.

## Upload tool picks the wrong serial port

With several ttyACM devices attached (e.g. the ADALM-Pluto's own serial
console) `tools/upload.sh` / `tools/serial.sh` do not take "the first port";
they resolve it by USB ID:

1. `1209:fa50` — **this firmware's own ID**, set in
   `ports/rp2/boards/RPI_PICO_TX/mpconfigboard.h` (`MICROPY_HW_USB_VID/PID`).
   It is independent of the board model: a Pico, Pico W, RP2040-Zero or a
   clone running this firmware all enumerate with it.
2. generic MicroPython RP2040 (`2e8a:0005`, or a description containing
   MicroPython) — for a board **not yet flashed** with this firmware; a note
   is printed to stderr.
3. the `/dev/pico` udev symlink, then `/dev/serial/by-id/*RP2040*`.

If tiers 1/2 match more than one device the tool does **not** guess: it lists
the candidates and asks for an explicit port:

```bash
FM_PORT=/dev/ttyACM1 tools/upload.sh
./tools/upload.sh --port /dev/ttyACM1
./tools/serial.sh /dev/ttyACM1
tools/pico_port.sh                 # print the resolved port, or list candidates
tools/pico_port.sh --self-test     # hardware-free self-check
```

`could not enter raw repl` with a login prompt or garbage captured means the
wrong port was picked (check with `tools/pico_port.sh`); note there is no CDC
port at all in BOOTSEL mode.

## Common mistakes

- **Antenna on GPIO21**: illegal and noisy — never;
- **`audio off` as mute**: it freezes the output frequency; use `mute`;
- **`freq` outside the current PLL range**: reboots to that carrier (like
  reinit), not an error;
- **`power` measured with a high-Z probe**: shows no difference; use a 50Ω load.
