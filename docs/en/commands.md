# Console Command Reference

> [中文](../zh/commands.md) · English

All values are in Hz unless noted. Type `help` in the console anytime.

## Common commands

| Command | Description | Example |
|---|---|---|
| `help` | Show the full command help | `help` |
| `ver` | Show version, firmware sha256 and project links | `ver` |
| `status` / `s` | Show all audio/FM parameters | `status` |
| `freq <Hz>` | Set the modulation centre (EFFECTIVE frequency; live inside the current PLL band; UHF targets ride the 3rd/5th harmonic; reboots outside the band) | `freq 433920000` |
| `dev <Hz>` | Set full-scale deviation (effective; 1000..half the PLL range x harmonic) | `dev 12000` |
| `reinit <carrier> <dev> [pin]` | Save new carrier/deviation/pin and reboot (UHF 409/433/440M targets are converted to the 3rd/5th-harmonic fundamental automatically) | `reinit 433920000 12000 21` |
| `pdm <1\|2\|3\|4>` | PDM dither rate in MHz (1 = default; >1 measured worse on hardware; saves and reboots) | `pdm 1` |
| `refdiv <1\|2\|auto>` | PLL reference divider (2 = half PDM step; auto is the default and keeps 1 on parked bands) | `refdiv auto` |
| `pin <21\|23\|24\|25>` | Switch the RF output pin (saves and reboots) | `pin 21` |
| `pwr <2\|4\|8\|12>` | RF drive strength in mA (transmit power); 8/12 give better even-harmonic suppression | `pwr 12` |
| `rf on\|off` | RF output on/off | `rf on` |
| `audio on\|off` | USB audio → FM routing on/off | `audio on` |
| `vol <0-100>` | Volume in percent | `vol 70` |
| `mute on\|off` | Mute (≈ vol 0; audio off ≠ mute) | `mute off` |
| `pre on\|off\|50\|75` | 15kHz band-limit + pre-emphasis (50 or 75µs) + limiter; on = 75µs (default), off = no processing | `pre 50` |
| `sq <0-100>` | Mute weak samples below this % of full scale so faint background noise is not modulated; paused/muted audio parks the carrier on fc and the radio is silent anyway; 0 = off (default) | `sq 5` |
| `led <0\|1\|2\|3>` | LED: 0=off 1=always-on 2=on-while-streaming 3=audio VU | `led 3` |
| `ledpin <0-29>` | Set the plain-LED pin (saves and reboots) | `ledpin 25` |
| `ledpin ws2812 [pin]` | Use a WS2812 NeoPixel (default GPIO16) | `ledpin ws2812` |
| `vbar` | Audio level meter (~50fps): peak bar, squelch threshold (T marker) and clip rate; shows "silent" (slow refresh) while the input is quiet; any key exits | `vbar` |
| `ring` | Ring buffer fill % | `ring` |
| `diag` | Measured ISR/RX rates over 1 s (both ~48000) plus the ring drift counters (underflow/drop) | `diag` |
| `pwm` | PWM/ISR diagnostics | `pwm` |
| `pll` | PLL diagnostics (ready/range/last written freq) | `pll` |
| `sweep [lo hi step]` | Pause audio and sweep the carrier (PLL self-test) | `sweep 87000000 88500000 500000` |
| `cls` | Clear the terminal screen (ANSI) | `cls` |
| `reset` | Delete the saved config and reboot to defaults | `reset` |
| `reboot` | Save the current settings and reboot (same as replugging, keeps config) | `reboot` |
| `exit` | Leave the console back to the REPL | `exit` |

## Transmit modes

All modulation runs in firmware; the console only selects the scheme and feeds
data. Switching to a non-`fm` mode starts the modulation engine automatically.

| Command | Description | Example |
|---|---|---|
| `mode [name]` | Show or select the transmit scheme: `fm`,`tone`,`fsk`,`ook`,`cw`,`chirp`,`psk` | `mode tone` |
| `tx on\|off` | Start/stop the modulation engine (non-FM schemes) | `tx off` |
| `tone <hz> [hz2] [level%]` | Internal DDS tone(s) FM-modulated onto the carrier; `tone off` stops | `tone 1000` |
| `fsk <baud> <shift_hz> <hex> [loop]` | Queue 2-FSK symbols (`0`→-shift, `1`→+shift); append `loop` to repeat | `fsk 1200 4500 55aa0f loop` |
| `ook <baud> <hex> [loop]` | Queue on/off keying symbols (`0`→RF off) | `ook 2000 aaaa loop` |
| `psk <baud> <2\|4> <hex> [loop]` | Queue BPSK (`2`) / QPSK (`4`) symbols | `psk 2400 2 abcd` |
| `cw <text>` | Key `text` as Morse on the carrier (20 wpm, blocking) | `cw CQ CQ DE PICO TX` |
| `chirp <f0> <f1> <ms> [gap] [repeat]` | Linear frequency sweep; `chirp off` stops | `chirp 87900000 88100000 100 20 1` |
| `service [cmd...]` | Console command to run automatically at boot (headless); `service off` clears | `service tone 1000` |
| `console on\|off` | Interactive console on/off; `off` keeps the boot service running (Ctrl-C returns to the REPL) | `console off` |

Notes:
- `fsk`/`ook`/`psk` use a 256-symbol firmware FIFO; symbols beyond that are
  rejected (`send` returns how many were accepted). Choose `baud` so the
  symbol rate fits the PLL loop bandwidth (a few kbaud for wideband FM).
- `shift_hz` must fit inside the PLL window set by `init(carrier ± deviation)`.
- `chirp` frequencies are absolute Hz and must also lie inside that window.
- `cw` keys the carrier off/on; the radio hears Morse only if it is tuned to
  the carrier (use a narrow FM/AM receiver or an SDR).
- `service` runs one command after boot, e.g. `service mode cw` or
  `service tone 1000`. Combine with `console off` for a headless transmitter.

## Notes

- **`freq` vs PLL range**: fine-tunes live only inside the current PLL range
  (shown by `status`); use `reinit` to switch bands.
- **`power`**: the RP2040 GPIO is rail-to-rail; a high-impedance probe sees no
  amplitude change. Use a real 50Ω load (SA 50Ω input + attenuator) to see a
  10 dB+ difference.
- **`audio off` vs `mute`**: both silence the radio immediately (the carrier
  parks on fc, no modulation). `audio off` stops the USB-audio -> FM routing
  and is reported as "muted" to the host (Windows shows the mute state);
  `mute` keeps the routing but silences the stream. `vol 0` is another way to
  get a quiet carrier.
- **`pre`**: on = 75µs (US/EU broadcast), `50` = 50µs (China/Japan). If your
  radio's de-emphasis is 50µs, `pre 50` reduces the high-frequency hiss.
- **UHF targets (409/433/440MHz)**: the RP2040 PLL can only drive a
  fundamental up to ~150MHz, so a UHF target is transmitted on the 3rd (or
  5th) odd harmonic of a lower fundamental — `reinit 433920000 12000 21`
  actually runs the PLL at 144.640MHz and the radio hears 433.920MHz (dev is
  also scaled xN).  `status` shows both.  **Legal/safety**: for the 409MHz
  band the fundamental sits at 136.6MHz inside the AERONAUTICAL band
  (118-137MHz) — the console warns; suppress it with a band-pass filter.
- **`pdm` / `refdiv`**: persisted and applied at the next reboot (a live
  PLL re-init without rebooting deadlocks the board - tested).  Hardware
  results: pdm >1MHz is WORSE (systick latency / PLL write timing).
  `refdiv 2` halves the PDM dither step (`ref/div`) on every band, but it
  must not be used where the carrier is parked (broadcast FM): the parked PDM
  pattern changes and the silent carrier gets an audible idle tone.  Hence the
  default `refdiv auto`: gated bands take 2, parked bands take 1.
  Use `tools/serial.sh` (tio auto-reconnect) so reboots do not kill your
  serial session.
- **FM silence while the host keeps streaming**: `mute on` / `audio off`
  park the unmodulated carrier on fc, but USB packet reception keeps running
  and its on-die digital activity couples faint noise into the parked carrier
  (audible only in the radio's quieting state; masked while music modulates
  at 75kHz deviation).  This is inherent to the square-wave PDM transmitter
  and was identical in the original firmware - the RX path drops the frames
  (no DSP) but the USB hardware must still receive them.  For perfect FM
  silence, pause the host playback (or `rf off`).  On narrowband bands the
  silence gate avoids this entirely by keying the RF off.
- **`sq` (weak-sample mute)**: samples below the threshold are zeroed in the
  RX path, so faint stream noise is not modulated. Note that pausing or
  muting already parks the carrier exactly on fc (no modulation), which makes
  a radio tuned to fc silent — the off-tune "silence gate" from earlier
  versions was removed because a CW at the IF band edge can make receiver
  AGC/quieting cycle audibly. Watch the T marker in `vbar`.
- **`vbar` clips**: the per-second clip rate shows how often the limiter is
  engaged; a high rate means the PC volume is too high (lower it, or `vol`).
- **`reset`** deletes `/tx_cfg.json` and reboots to defaults (75µs pre-emphasis,
  squelch off). **`reboot`** persists the current settings first and reboots
  without deleting them — use it when a change only takes effect after a
  replug (e.g. after `ledpin`/`pin`-style reconfigurations you have already
  saved).
- Settings persist in `/tx_cfg.json` on the board's filesystem and are restored
  at boot.
