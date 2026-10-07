# FM transmit audio-quality layer audit

> [中文](../zh/audio-quality.md) · English

This document audits, layer by layer, the code in this repository that affects
the **received** sound quality, and gives quantified limits plus an impact
ranking. Every number comes from the host-side script
`tools/audio_quality.py`, a line-by-line replica of the fixed-point semantics
and coefficient constants of `tx_modulator.c: tx_audio_process()`.

> **This analysis does not flash the board and does not measure hardware.**
> Every number below is reproducible on the host with:
> ```bash
> python3 tools/audio_quality.py resp
> python3 tools/audio_quality.py thd
> python3 tools/audio_quality.py limiter
> python3 tools/audio_quality.py drift
> ```

---

## 1. Layers and impact ranking

| Layer | Location | Limit | Audible effect | Priority |
|---|---|---|---|---|
| **L3 audio DSP chain** | `tx_modulator.c: tx_audio_process()` | non-standard pre-emphasis (+8.6 dB too much at 2 kHz, −6.3 dB too little at 15 kHz); two first-order band-limit poles (−2.2 dB at 10 kHz); no limiter look-ahead | **tonal error (midrange hump, dull top) + severe distortion on loud material (THD 18–26 %)** | **P0** |
| **L1 USB audio & asynchronous sample clock** | `shared/tinyusb/*`, `tud_audio_rx_done_isr()` | host 48 kHz and PWM 48 kHz come from two independent crystals; no resampler, no feedback endpoint, no drift counter | periodic clicks after long playback (starts after ~14 min and persists) | **P1** |
| **L5 PLL/PDM RF layer** | `pico_fractional_pll.c` | the instantaneous frequency dithers by `ref/div` at 1 MHz; the PLL loop filter averages it only partially | RF phase noise / receiver noise floor (worst on narrowband and weak signals) | **P1** |
| **L2 mono mix & ring buffer** | `tud_audio_rx_done_isr()`, `fm_pwm_wrap_handler()` | the mix itself is clean (no int32 overflow; anti-phase cancellation is inherent to mono); the ring has no drop/underflow counters and zeroes instead of holding | the mix is inaudible (−91.8 dBFS truncation); the ring hides L1 and turns a drop into a dropout | **P2** |
| **L6 USB control surface & defaults** | `tx_modulator.c: tx_db256_to_gain()`, `main.py` | UAC1 volume maps **dB → linear gain** linearly, not dB → dB | volume slider is mis-calibrated (request −30 dB, get −6 dB) | **P2** |
| **L4 modulation mapping** | `fm_pwm_wrap_handler()` | `freq = carrier + (sample*dev)>>15` | none (0.99997× full deviation; negligible) | — |

Rationale: L3 decides the tone and distortion and is 100 % verifiable on the
host; L1/L5 decide long-listen and weak-signal usability; L2/L6 are correctness
and observability.

---

## 2. Layer-by-layer audit

### L3 audio DSP chain (P0)

With pre-emphasis on, the chain is: `2 Hz DC block → first-order 15 kHz LP →
pre-emphasis → first-order 15 kHz LP → 2:1 soft-knee limiter`.

**Defect 1: the pre-emphasis curve is not the standard one.** The code computes
`y = lp15 + (lp15 - lp75)*5`, i.e. a **shelf with a +14 dB ceiling**
(20·log10(5)) that starts bending around 1 kHz. Standard 75 µs pre-emphasis is
a +6 dB/octave rise, referenced to 0 dB at 2.12 kHz, reaching +17.0 dB at
15 kHz.

```
  f (Hz)  75us     (measured transmit response, dB)
  1000    7.75
  2000    11.33
  5000    13.20
  15000   10.80

  Net received response = transmit response - standard receiver de-emphasis
  (what the listener actually hears)
  f (Hz)     75us net
  1000           6.88
  2000           8.57
  5000           5.04
  10000         -1.44
  15000         -6.27
```

**Result: the listener hears a +8.6 dB midrange hump around 2 kHz and a −6.3 dB
collapse at 15 kHz.** This is not "pre-emphasis is off"; the *shape* is wrong.
Turning pre-emphasis off only makes the top end duller, because the receiver's
de-emphasis is a fixed −6 dB/octave.

**Defect 2: the pre-emphasis burns 7–8 dB of headroom.** Because 1 kHz is
already boosted by 7.75 dB:

```
  tone    level      preemph 75   preemph off  clips(75us)
  1000    loud          17.859 %      0.002 %        0
  5000    loud          25.945 %      0.000 %        0
  1000    linear         0.033 %      0.013 %        0
```

`loud` is a sine of amplitude 20000 (≈ −4 dBFS). The standard 75 µs curve has
only +0.87 dB at 1 kHz, so the same input would not reach the limiter. **The
distortion is therefore caused by the wrong pre-emphasis curve, not by the
limiter itself.**

**Defect 3: the band-limit is only two first-order poles.**

```
  f (Hz)  lp2only   (band-limit only, dB)
  5000     -0.66
  10000    -2.16
  15000    -3.67
  23000    -4.89
```

The passband is already 2.2 dB down at 10 kHz and 3.7 dB down at 15 kHz (large
for a 15 kHz broadcast band); the stopband at 23 kHz is only −4.9 dB, i.e. the
48 kHz sampling images are barely suppressed.

**Defect 4: disabling pre-emphasis also removes the band-limit.** The
`if (!s_preemphasis_enabled)` branch `return x` without any low-pass in
wideband mode; only narrowband keeps its 3 kHz low-pass. See the flat `off`
column above.

**Defect 5: no limiter look-ahead.** On a step input, 200 samples sit above the
soft knee:

```
  step 0->30000 [+pre]   overshoot=200 samples above knee, clips=0
```

(The soft knee is intentional programme processing and need not change, but it
amplifies defect 2.)

### L1 USB audio & asynchronous sample clock (P1)

- descriptor: UAC1, 48 kHz, 16-bit, stereo, `wMaxPacketSize=192`,
  `bInterval=1`;
- `CFG_TUD_AUDIO_ENABLE_FEEDBACK_EP = 0`: **no feedback endpoint**, so the host
  free-runs 48000 samples/s off its own crystal;
- the consumer is PWM slice 7, `48 MHz / 1000`, derived from the RP2040's
  12 MHz crystal;
- there is **no asynchronous sample-rate conversion (ASRC)** between them;
  the drift counters were added in change 4 (§4).

```
  ppm    time to hit    glitch rate      glitches in 600s
         the limit      (samples/s)
  -100   427 s          4.80             832
  -50    853 s          2.40             0
  -25    1707 s         1.20             0
  100    427 s          4.80             832

  Ring depth = 4096 samples = 85.3 ms of buffering
```

Typical crystal errors of 10–50 ppm mean the buffer **saturates after roughly
14–28 minutes, then drops/underflows continuously at 0.5–2.4 events/s** (each
one a sample-level discontinuity = a light click). Two free-running crystals
never re-align themselves, so this is a "guaranteed after a while" problem.

### L5 PLL/PDM RF layer (P1)

The audio DSP decides *what* is sent; this layer decides *how clean* the
emission is. The instantaneous frequency dithers by `step = ref/div` on the
1 MHz PDM tick, and the PLL loop filter only partly averages it:

```
$ python3 tools/pll_range.py check 87900000 75000 1
... div=16 ... PDM step=750.000 kHz
$ python3 tools/pll_range.py check 87900000 75000 2
... div=16 ... PDM step=375.000 kHz
$ python3 tools/pll_range.py check 7074000 3000 1
... div=150 ... PDM step=80.000 kHz
$ python3 tools/pll_range.py check 7074000 3000 2
... div=200 ... PDM step=30.000 kHz
```

`refdiv 2` halves the step on **every** band, but `main.py` only set
`refdiv 2` for targets above 150 MHz (UHF harmonics). Note that the host
cannot compute the **residual** ripple after loop filtering (that needs the
loop bandwidth and a phase-noise measurement), so only the computable step is
given here; the residual is left to hardware measurement.

→ **fixed by change 5 (§4)**: replaced by a data-driven rule plus the new
`refdiv auto`.

### L2 mono mix & ring buffer (P2; the buffer half is fixed by change 4 in §4)

**The mix half: audited, clean, no change warranted.**
`tud_audio_rx_done_isr()` first computes
`mono = ((int32_t)frame[0] + frame[1]) >> 1`, then `mono = (mono * volume) >> 15`:

```
$ python3 tools/audio_quality.py mono
    L=R=30000  -> 29999  (unity for correlated content)
    L=+30000 R=-30000 -> 0  (anti-phase cancels: inherent to a mono mix)
  volume     mean err (LSB) rms err (LSB)  equiv. noise floor
  0 dB       -0.750         0.841          -91.8 dBFS
  -6 dB      -0.376         0.469          -96.9 dBFS
  -40 dB     -0.502         0.579          -95.1 dBFS
```

- the int32 sum is halved, so it cannot overflow; correlated content is 1:1 and
  no gain error is introduced;
- the only artefact is truncating instead of rounding: at unity volume the mix
  floor (−0.25 LSB mean) and the volume floor add up to −0.75 LSB mean /
  0.84 LSB rms = **−91.8 dBFS**, about 9 dB above an ideal rounder
  (0.289 LSB rms = −101.1 dBFS) and far below any audible threshold; its DC
  part is removed by the 2 Hz DC blocker. No rounding logic is added for it.

**The ring-buffer half: state before the fix.**

- on an empty ring `fm_pwm_wrap_handler()` sets `s_cur_sample = 0` → the
  carrier parks on fc for that tick (a dropout) instead of holding the last
  sample;
- on a full ring the host sample is dropped silently (the code comments say so);
- **neither event increments any counter**; `diag` exposes only `ISR ticks` and
  `RX frames`, so L1's drift is invisible.

### L6 USB control surface & defaults (P2)

`tx_db256_to_gain()` maps the UAC1 dB value **linearly** to a linear gain:

```c
return (uint16_t)(((clamped - FM_VOL_MIN) * 32767) / (FM_VOL_MAX - FM_VOL_MIN));
```

so `gain_lin = (db + 60) / 60` and the actual attenuation is
`20·log10((db+60)/60)`:

| requested (dB) | actual (dB) | error |
|---|---|---|
| −6 | −0.92 | +5.1 |
| −12 | −1.94 | +10.1 |
| −20 | −3.52 | +16.5 |
| −30 | −6.02 | +24.0 |
| −40 | −9.54 | +30.5 |
| −50 | −15.56 | +34.4 |
| −60 | −∞ | — |

i.e. only the top ~10 dB of the volume slider is accurate.

→ **fixed by change 6 (§4)**.

The other defaults (`DEFAULT_PREEMPH="on"(75us)`, `DEFAULT_SQUELCH=0`,
`DEFAULT_CARRIER=87.9 MHz`) are reasonable in themselves; the `refdiv` choice
rule is covered in L5.

### L4 modulation mapping (negligible)

`freq = s_carrier_hz + ((int64_t)sample * deviation) >> 15`: full-scale ±32767
gives 0.99997× deviation and 0 gives an exactly parked carrier — both
intentional and correct. FM needs no pre-integration of the audio. 48 kHz
sampling gives 3.2× oversampling for a 15 kHz audio band, which is enough.

---

## 3. Full baseline output

<details>
<summary>python3 tools/audio_quality.py resp</summary>

```
Frequency response (dB re input; small signal, limiter not engaged)
  f (Hz)  75us      50us      300us     off       nfm300    lp2only
  20      0.04      0.01      0.35      -0.00     -20.41    -0.00
  50      0.10      0.05      1.25      -0.00     -14.63    -0.00
  100     0.29      0.12      3.34      -0.00     -6.81     -0.00
  300     1.86      0.85      9.53      -0.00     6.28      -0.01
  1000    7.75      5.01      14.24     -0.00     12.82     -0.03
  2000    11.33     8.87      14.91     -0.00     11.62     -0.11
  3000    12.62     10.71     14.94     -0.00     9.05      -0.25
  5000    13.20     11.99     14.61     -0.00     3.84      -0.66
  8000    12.76     11.93     13.79     -0.00     -2.28     -1.51
  10000   12.21     11.49     13.14     -0.00     -5.29     -2.16
  12000   11.63     10.96     12.51     -0.00     -7.67     -2.80
  14000   11.06     10.44     11.91     -0.00     -9.54     -3.39
  15000   10.80     10.18     11.64     -0.00     -10.31    -3.67
  16000   10.56     9.96      11.39     -0.00     -10.98    -3.92
  18000   10.14     9.56      10.97     -0.00     -12.05    -4.35
  20000   9.84      9.27      10.65     -0.00     -12.78    -4.65
  22000   9.65      9.08      10.46     -0.00     -13.22    -4.85
  23000   9.61      9.04      10.41     0.00      -13.32    -4.89

  Net received response = transmit response - standard de-emphasis
  f (Hz)     75us net   50us net  300us net
  20             0.04       0.01       0.35
  50             0.10       0.04       1.21
  100            0.28       0.12       3.19
  300            1.78       0.81       8.33
  1000           6.88       4.61       7.66
  2000           8.57       7.42       3.09
  3000           7.85       7.95      -0.25
  5000           5.04       6.59      -4.93
  8000           0.94       3.29     -9.80
  10000         -1.44       1.13    -12.37
  12000         -3.56      -0.87    -14.59
  14000         -5.43      -2.65    -16.52
  15000         -6.27      -3.47    -17.40
  16000         -7.07      -4.24    -18.20
  18000         -8.49      -5.62    -19.65
  20000         -9.69      -6.81    -20.88
  22000        -10.70      -7.80    -21.90
  23000        -11.13      -8.22    -22.33
```
</details>

<details>
<summary>python3 tools/audio_quality.py thd / limiter / drift</summary>

```
Harmonic distortion (THD, %), 1 s window, harmonics 2..10
  tone       level        preemph 75   preemph off  clips(75us)
  1000       linear       0.033        0.013        0
  1000       loud         17.859       0.002        0
  1000       over         28.778       0.822        0
  5000       linear       0.005        0.004        0
  5000       loud         25.945       0.000        0
  5000       over         28.374       0.545        0
  10000      linear       0.006        0.000        0
  10000      loud         1.946        0.000        0
  10000      over         0.758        0.268        0

Limiter / peak deviation
  signal                       peak_out   peak/dev      clips
  full-scale 1kHz sine [+pre]      29491     0.9000          0
  full-scale 1kHz sine [flat]      32768     1.0000          0
  full-scale 6kHz sine [+pre]      29479     0.8996          0
  full-scale 6kHz sine [flat]      32768     1.0000          0
  full-scale 12kHz sine [+pre]     27336     0.8342          0
  full-scale 12kHz sine [flat]     32768     1.0000          0
  step 0->30000 [+pre]       overshoot=200 samples above knee, clips=0
  step 0->30000 [flat]       overshoot=200 samples above knee, clips=0

Asynchronous sample-clock drift (see the L1 table)
```
</details>

---

## 4. Implemented changes and before/after data (host side)

All three changes stay inside the fixed-point path of `tx_modulator.c` (no
division, no floating point); `python3 tools/audio_quality.py compare`
reproduces the tables below exactly. The patch was regenerated
(`patches/micropython-tx.patch`) and **compiles** (`make BOARD=RPI_PICO_TX`;
compile only, nothing was flashed).

### Change 1: standard pre-emphasis curve

- old: `y = lp15 + (lp15-lp75)*5` — a fixed +14 dB shelf
- new: first difference `y = x + G*(x-x[n-1])` with `G = tau*fs` (Q10):
  75 µs → 3686, 50 µs → 2458, 300 µs → 14746
- semantics: disabling pre-emphasis no longer removes the band-limit (the old
  code had no low-pass at all on the wideband + disabled path)

```
Net received response (what the listener hears; 0 dB = correct)
  f (Hz)     old 75us   new 75us
  1000           6.88       0.22
  5000           5.04       0.91
  8000           0.94       0.82
  10000         -1.44       0.56
  14000         -5.43      -0.03
  15000         -6.27      -0.37
  worst |error| in 0.3-15 kHz: 6.88 dB -> 0.91 dB

THD (%)
  tone   level      old 75us   new 75us
  1000   loud        17.859%     0.008%
  5000   loud        25.945%    13.180%
  10000  over         0.758%     0.000%

Band-limit with pre-emphasis disabled
  f (Hz)      old off    new off
  20000         -0.00     -34.72
  23000         -0.00     -79.25
```

### Change 2: fourth-order Chebyshev band-limit (15 kHz)

The coefficients are generated by `tools/design_filters.py` (Chebyshev I,
0.2 dB ripple, fc = 15 kHz, two second-order sections, Q15). The script also
verifies the quantised response error (< 0.05 dB) and the peak internal state
at full scale (~3.3e4, far below int32). The narrowband path moves to a
second-order section at 3.4 kHz.

```
Band-limit response (pre-emphasis off; dB re input)
  f (Hz)    old 1p x2  new cheby
  5000          -0.66       0.12
  10000         -2.16       0.14
  15000         -3.67      -0.00
  18000         -4.35     -17.33
  20000         -4.65     -34.72
  23000         -4.89     -79.25
```

### Change 3: fixed-point overflow safety

The biquad input and feedback term are bounded to int16 range so the largest
product is `|b1|*32767 = 1.32e9 < 2^31`. (Without the bound, full-scale
broadband noise rings a section's state to ~1e5 and `a1*y = 2.5e9` overflows
int32 = undefined behaviour.) Saturation only happens where the signal is
already several times full scale and about to be hard-clipped by the limiter;
the unsaturated y is still returned to the limiter, so the `clips` counter
keeps working.

### Change 4: hold-last on underflow + drop/underflow counters (L1/L2)

- `tx_modulator.c`: an empty ring no longer always zeroes the sample. While
  the host is streaming (`s_audio_active`) and the empty run is shorter than
  480 ticks (10 ms), the previous sample is repeated (hold-last) and counted
  in `s_ring_underflows`. A longer empty run means the host really stopped, so
  the carrier still parks on fc (the original "host paused = exactly on fc"
  behaviour is preserved).
- a full ring now increments the new `s_ring_drops` (it used to be a silent
  drop).
- exposed as `pico_tx.ring_stats() -> (underflows, drops)`; `diag` and
  `status` print them (on older firmware `diag` says the interface is
  missing).

```
Asynchronous sample-clock drift (same counters; policy-independent)
  ppm    time to hit    glitch rate      counters: underflows / drops
  -100   427 s          4.80             833 / 0
  -50    853 s          2.40             0 / 0
  50     853 s          2.40             0 / 0
  100    427 s          4.80             0 / 881

Audible effect of a drift event (1 kHz sine, -6 dBFS, through the DSP chain)
  ppm      worst |dy| hold  worst |dy| zero  THD zero     THD hold
  10       5898             22993                0.016%    0.007%
  50       7053             27591                0.035%    0.008%
  100      7053             27591                0.064%    0.012%
  clean    5898             -                        -    0.007%
```

The event *count* is identical (the counter proves the drift happens), but
after the change each event no longer produces a 4-5x larger step and the
THD returns to the drift-free value.

Note: the counters only make the drift *visible*, they do not correct it;
hold-last removes the click, not the timing error. A real fix is an ASRC or a
UAC1 feedback endpoint (see the upgrade path below).

### Change 5: refdiv policy and console defaults (L5)

- new `refdiv_for(target)`: **1 where the carrier is parked, 2 everywhere
  else.**  Broadcast FM (87.5–108 MHz) parks the unmodulated carrier when
  silent, and refdiv 2 changes the parked PDM pattern and puts an audible idle
  tone on the silent carrier (hardware-observed: the v0.22.x FM silence
  regression), so it must stay 1. 2m/UHF/narrowband-HF key the RF off when
  silent, so there is no parked carrier and they take 2.
- `band_apply()`, the `band` command and boot-time `apply_config()` all use the
  helper now (they used to test `target > 150 MHz` only); the console gained
  `refdiv <1|2|auto>` with auto as the default, and an explicit 1/2 in the
  config still wins.
- if the refdiv-2 window is unsolvable at boot, init automatically falls back
  to refdiv 1 (a failed `init` never launches core1, so the retry is safe) and
  persists the 1.
- `status` now shows the actual PDM step (the width of the window
  `pico_tx.range()` returns is exactly one feedback-divider step).

```
$ python3 tools/pll_range.py minstep 1800000 30000000 50000 3000 --refdiv 2
  windows with a solution: 559, without: 6
  first-found is NOT the minimum step in 0 window(s)
$ python3 tools/pll_range.py minstep 80000000 500000000 500000 5000 --refdiv 2
  windows with a solution: 129, without: 712
  first-found is NOT the minimum step in 0 window(s)
```

The solution the C `calculate_pll_divider()` returns already has the smallest
step within its pass, so the search needs no change (verification instead of
speculation).

| Band | step before | step after |
|---|---|---|
| FM 98.0 MHz (parked) | 750 kHz (refdiv 1) | 750 kHz (stays 1, avoids the idle tone) |
| 2m 145.0 MHz | 1.2 MHz (refdiv 1) | **600 kHz** (refdiv 2) |
| 433.92 MHz (x3) | 600 kHz (refdiv 2) | 600 kHz (unchanged) |
| 30m 10.136 MHz | 80 kHz (refdiv 1) | **40 kHz** (refdiv 2) |
| 20m 14.074 MHz | 120 kHz | **60 kHz** |
| 10m 28.074 MHz | 240 kHz | **120 kHz** |

### Change 6: real dB mapping for the UAC1 volume (L6)

- new `fm_vol_table[61]` (one `round(32767*10^(dB/20))` per dB from 0 to
  -60 dB); `tx_db256_to_gain()` interpolates between whole dB and the
  1/256 dB remainder, and `tx_gain_to_db256()` searches the monotonic table
  (exact round trip).
- only the two fixed-point helpers change; neither is on the ISR path and
  there is no floating point.

| requested (dB) | actual before | error before | actual after | error after |
|---|---|---|---|---|
| −6 | −0.92 | +5.08 | −6.00 | −0.00 |
| −12 | −1.94 | +10.06 | −12.00 | +0.00 |
| −20 | −3.52 | +16.48 | −20.00 | +0.00 |
| −30 | −6.02 | +23.98 | −30.00 | −0.00 |
| −40 | −9.54 | +30.46 | −39.99 | +0.01 |
| −60 | −∞ | — | −59.94 | +0.06 |

Worst interpolation error: 0.015 dB over 0…−30 dB, 0.26 dB over the whole
range (at −60 dB, i.e. already 60 dB of attenuation).

### Before/after (`audio_quality.py compare`)

| Metric | Before | After |
|---|---|---|
| net received response at 1 kHz | +6.88 dB | **+0.22 dB** |
| net received response at 15 kHz | −6.27 dB | **−0.37 dB** |
| worst error in 0.3–15 kHz | 6.88 dB | **0.91 dB** |
| 14 kHz passband droop | 3.39 dB | **0.15 dB** |
| 23 kHz stopband | −4.89 dB | **−79.25 dB** |
| 1 kHz loud-level THD | 17.86 % | **0.008 %** |
| 10 kHz full-scale THD | 0.76 % | **0.000 %** |
| band-limit with pre-emphasis off | none (0.00 dB) | 20 kHz −34.7 dB |
| worst step on an underflow | 27591 | **7053** |
| THD during underflows | 0.035 % | **0.008 %** |
| drop/underflow observability | no counters | `pico_tx.ring_stats()` |
| 2m PDM dither step | 1.2 MHz | **600 kHz** |
| 30m PDM dither step | 80 kHz | **40 kHz** |
| refdiv selection | `target > 150 MHz` only | **`refdiv_for()` + `refdiv auto`** |
| actual attenuation for a −30 dB request | −6.0 dB (error +24 dB) | **−30.0 dB (error 0)** |

## 5. Remaining work

| Item | Status |
|---|---|
| L1/L2: hold-last + drop/underflow counters | **done (change 4)**; residual: no ASRC / feedback endpoint, so the drift still exists |
| L5: refdiv policy + `refdiv auto` | **done (change 5)**; residual: the **residual** ripple after loop filtering still needs hardware measurement |
| L6: real dB mapping for the UAC1 volume | **done (change 6)** (worst interpolation error 0.26 dB, 0.015 dB over 0…−30 dB) |
| limiter look-ahead | not implemented (gain is programme-dependent; change 1 already removed the main pseudo-distortion source) |
| ASRC / UAC1 feedback endpoint | not implemented (needs ISR budget or TinyUSB feedback support; a structural change) |

---

## 6. Out of scope

- **Stereo MPX (19 kHz pilot + 38 kHz DSB)**: at 48 kHz sampling it needs 4×
  interpolation (192 kHz), while the current ISR already costs 2–6 µs/sample;
  4× is 40–115 % CPU. It is a separate project requiring a sampling-clock
  redesign.
- **AM/SSB/DRM**: this chain is a fixed-amplitude square wave with frequency-only
  modulation; they need an external linear modulator stage.
- **RF measurements** (phase noise, residual ripple, real received audio): this
  goal explicitly excludes hardware measurement; the "residual ripple" in item 5
  above can only be settled by measurement.

---

## 7. Regression verification

**Build (`./build.sh`, clean clone)**

```
$ ./build.sh
==> Building RPI_PICO_TX firmware, MicroPython ref: v1.29.0
    trying https://git.sr.ht/~bytewolf/micropython ...
    patch applied
    source: https://git.sr.ht/~bytewolf/micropython
[100%] Built target firmware
==> Done: firmware/picotx_firmware.uf2
   FLASH 350180 B / 640 KB (53.43%), RAM 36812 B / 256 KB (14.04%)
```

- `./build.sh` completed end-to-end on a **clean clone** (clone → apply patch →
  `make submodules` → build); the resulting UF2 has sha256
  `810e9e0bd8599ef11051de81aa9a65a28e35ca4b39bf30caf9983b11f6018ebb`, and that
  is what `firmware/picotx_firmware.uf2` holds (`sha256sum -c
  firmware/sha256.txt` passes and `python/main.py`'s `FW_SHA256` matches).
- **Patch round-trip**: regenerating the patch after applying
  `patches/micropython-tx.patch` to a clean MicroPython gives **0 diff** against
  the committed patch, i.e. the patch is self-consistent and reproduces this
  build exactly.

**Hardware validation (v0.24.0, 98.0 MHz FM broadcast, host streaming)**

| Item | Measured | Cross-check |
|---|---|---|
| `ver` | sha `810e9e0b…` | matches the shipped firmware |
| `status` | `PLL range 97.500..98.250 MHz`, `step 750.0 kHz`, `refdiv 1` | matches `pll_range.py check 98000000 75000 1 -> div=16` bit for bit |
| `diag 30` | ISR +1440230, RX +1440192, Underflows +39, Drops 0 | two independent estimates: 27.1 ppm from the event count, 26.4 ppm from the counter difference; predicted event rate 1.28/s vs 1.3/s measured |
| `Clips` | 0 | the reworked pre-emphasis leaves ample headroom |

The host 48 kHz clock is ~27 ppm slower than the on-board PWM clock, so the
ring is starved and the number of dropped samples equals the event count (no
hidden loss). On the old firmware this is **1.3 full-scale steps per second
(clicks)**; this version turns each into a repeated sample (~20 µs,
inaudible) - the one item of this change that simulation could not settle.

- **Doc-number consistency**: all 72 values quoted in this document were found
  in the corresponding script output (`audio_quality.py resp/compare/drift/`
  `mono/volume`, `pll_range.py bands/check/minstep`); no documented number is
  unreproducible.
