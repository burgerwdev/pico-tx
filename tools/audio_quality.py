#!/usr/bin/env python3
"""Host-side fixed-point replica of the FM audio chain.

This ports the fixed-point arithmetic of `fm_modulator.c: fm_audio_process()`
(exact coefficient constants, int16 truncation and int32 wrapping) so the
transmitted-sound quality can be measured **on the host**, without flashing
or probing hardware.

Sub-commands:
    resp     frequency response of the chain (pre-limiter, small signal)
    thd      harmonic distortion for test tones, linear and hot levels
    limiter  peak deviation / clipping behaviour on a stress signal
    drift    asynchronous USB-vs-PWM clock drift through the ring buffer
    all      everything, as one report block

Usage:
    python3 tools/audio_quality.py all
    python3 tools/audio_quality.py resp --tc 75 --nfm
    python3 tools/audio_quality.py drift --ring 4096
"""

import argparse
import sys

import numpy as np

FS = 48_000

# --- constants copied verbatim from ports/rp2/fm_transmitter/fm_modulator.c --
FM_LP15_A_Q15 = 28168    # a = 1 - exp(-2*pi*15000/48000)  (15 kHz LP)
FM_LP3K_A_Q15 = 10644    # a = 1 - exp(-2*pi*3000/48000)   (3 kHz LP)
FM_HP300_A_Q15 = 31509   # a = exp(-2*pi*300/48000)        (300 Hz HP)
FM_LP75_A_Q15 = 7953     # a = 1 - exp(-1/(75us*48000))    (2.1 kHz LP)
FM_LP50_A_Q15 = 11169    # a = 1 - exp(-1/(50us*48000))    (3.2 kHz LP)
FM_LP300_A_Q15 = 2199    # a = 1 - exp(-1/(300us*48000))   (530 Hz LP)
FM_PREEMPH_G_Q10 = 5120  # shelf gain 5.0 (~ +14 dB)       (Q10)
FM_DCB_A_Q15 = 9         # 2 Hz DC blocker
FM_LIM_SOFT = 26214      # soft-knee entry, 80% of full scale

TC_TO_COEF = {50: FM_LP50_A_Q15, 75: FM_LP75_A_Q15, 300: FM_LP300_A_Q15}


def i16(v):
    """C `(int16_t)v`: truncating (wrapping) conversion, not saturation."""
    v &= 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


def i32(v):
    """C int32_t wrap-around."""
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v >= 0x80000000 else v


class Chain:
    """Replica of fm_audio_process() state and arithmetic."""

    def __init__(self, preemph=True, tc_us=75, nfm=False, squelch=0, lponly=False):
        self.preemph = preemph
        self.lponly = lponly
        self.lp_a = FM_LP3K_A_Q15 if nfm else FM_LP15_A_Q15
        self.lp75_a = TC_TO_COEF.get(tc_us, FM_LP75_A_Q15)
        self.nfm = nfm
        self.squelch = squelch
        self.clips = 0
        self.lp15 = self.lp75 = self.lp15b = 0
        self.dcb_xprev = self.dcb_yprev = 0
        self.hp_xprev = self.hp_yprev = 0

    def process(self, x):
        lp_a = self.lp_a

        # 2 Hz DC blocker (note: s_dcb_yprev stores the *unclamped* value,
        # s_dcb_xprev the raw input - replicated exactly).
        dcb = i32(self.dcb_yprev + (x - self.dcb_xprev)
                  - ((self.dcb_yprev * FM_DCB_A_Q15) >> 15))
        self.dcb_xprev = x
        self.dcb_yprev = dcb
        if dcb > 32767:
            dcb = 32767
        elif dcb < -32768:
            dcb = -32768
        x = i16(dcb)

        # NFM 300 Hz high-pass
        if self.nfm:
            hp = (FM_HP300_A_Q15 * (x + self.hp_yprev - self.hp_xprev)) >> 15
            self.hp_xprev = x
            self.hp_yprev = hp
            if hp > 32767:
                hp = 32767
            elif hp < -32768:
                hp = -32768
            x = i16(hp)

        if self.squelch:
            if abs(x) < self.squelch:
                x = 0

        if not self.preemph:
            if self.nfm:
                lp = i32(self.lp15 + ((lp_a * (x - self.lp15)) >> 15))
                self.lp15 = lp
                x = i16(lp)
            return x

        lp15 = i32(self.lp15 + ((lp_a * (x - self.lp15)) >> 15))
        self.lp15 = lp15

        if self.lponly:
            # just the two band-limit poles, no pre-emphasis (isolates the LPF)
            y = i32(self.lp15b + ((lp_a * (lp15 - self.lp15b)) >> 15))
            self.lp15b = y
            return i16(y)

        lp75 = i32(self.lp75 + ((self.lp75_a * (lp15 - self.lp75)) >> 15))
        self.lp75 = lp75
        y = i32(lp15 + (((lp15 - lp75) * FM_PREEMPH_G_Q10) >> 10))

        if y > 32767:
            y = 32767
        elif y < -32768:
            y = -32768

        lp15b = i32(self.lp15b + ((lp_a * (y - self.lp15b)) >> 15))
        self.lp15b = lp15b
        y = lp15b

        if y > FM_LIM_SOFT:
            y = FM_LIM_SOFT + ((y - FM_LIM_SOFT) >> 1)
        elif y < -FM_LIM_SOFT:
            y = -FM_LIM_SOFT + ((y + FM_LIM_SOFT) >> 1)
        if y > 32767:
            y = 32767
            self.clips += 1
        elif y < -32768:
            y = -32768
            self.clips += 1

        return i16(y)

    def run(self, xs):
        return np.array([self.process(int(v)) for v in xs], dtype=np.int64)


def sine(f, n, amp):
    t = np.arange(n) / FS
    return np.rint(amp * np.sin(2 * np.pi * f * t)).astype(np.int64)


# --- improved chain (fm_modulator.c after the audio-quality rework) ---------
FM_PRE_G_Q10 = {50: 2458, 75: 3686, 300: 14746}
FM_BQ = {
    "wide": [(20188, 40377, 20188, 25338, 22648),
             (9684, 19368, 9684, 1777, 4191)],
    "nfm": [(2540, 5080, 2540, -37018, 14411)],
}


def biquad_run(q, x):
    """C fm_biquad_run(): q = [b0,b1,b2,a1,a2,s1,s2] (Q15 coefficients).

    Input and feedback are bounded to int16 range (exactly as the C does) so
    the products cannot overflow int32.
    """
    if x > 32767:
        x = 32767
    elif x < -32768:
        x = -32768
    b0, b1, b2, a1, a2, s1, s2 = q
    y = ((b0 * x) >> 15) + s1
    yf = 32767 if y > 32767 else (-32768 if y < -32768 else y)
    q[5] = ((b1 * x) >> 15) - ((a1 * yf) >> 15) + s2
    q[6] = ((b2 * x) >> 15) - ((a2 * yf) >> 15)
    return y


class NewChain:
    """Replica of fm_audio_process() after the audio-quality rework:
    DC block -> 300 Hz HP (nfm) -> pre-emphasis -> band-limit -> limiter."""

    def __init__(self, preemph=True, tc_us=75, nfm=False, squelch=0):
        self.preemph = preemph
        self.nfm = nfm
        self.squelch = squelch
        self.clips = 0
        self.pre_g = FM_PRE_G_Q10.get(tc_us, FM_PRE_G_Q10[75])
        self.pre_x1 = 0
        self.dcb_xprev = self.dcb_yprev = 0
        self.hp_xprev = self.hp_yprev = 0
        kind = "nfm" if nfm else "wide"
        self.bq = [[*c, 0, 0] for c in FM_BQ[kind]]

    def process(self, x):
        dcb = i32(self.dcb_yprev + (x - self.dcb_xprev)
                  - ((self.dcb_yprev * FM_DCB_A_Q15) >> 15))
        self.dcb_xprev = x
        self.dcb_yprev = dcb
        if dcb > 32767:
            dcb = 32767
        elif dcb < -32768:
            dcb = -32768
        x = i16(dcb)

        if self.nfm:
            hp = (FM_HP300_A_Q15 * (x + self.hp_yprev - self.hp_xprev)) >> 15
            self.hp_xprev = x
            self.hp_yprev = hp
            if hp > 32767:
                hp = 32767
            elif hp < -32768:
                hp = -32768
            x = i16(hp)

        if self.squelch and abs(x) < self.squelch:
            x = 0

        pre_d = x - self.pre_x1
        self.pre_x1 = x
        if self.preemph:
            pre = x + ((self.pre_g * pre_d) >> 10)
            if pre > 32767:
                pre = 32767
            elif pre < -32768:
                pre = -32768
            x = pre

        y = x
        for q in self.bq:
            y = biquad_run(q, y)

        if y > FM_LIM_SOFT:
            y = FM_LIM_SOFT + ((y - FM_LIM_SOFT) >> 1)
        elif y < -FM_LIM_SOFT:
            y = -FM_LIM_SOFT + ((y + FM_LIM_SOFT) >> 1)
        if y > 32767:
            y = 32767
            self.clips += 1
        elif y < -32768:
            y = -32768
            self.clips += 1
        return i16(y)

    def run(self, xs):
        return np.array([self.process(int(v)) for v in xs], dtype=np.int64)


def tone_db(y, f):
    """Amplitude of the f-Hz component of y, in dB relative to full scale."""
    n = len(y)
    spec = np.fft.rfft(y.astype(np.float64) * np.hanning(n))
    k = int(round(f * n / FS))
    mag = 2.0 * abs(spec[k]) / (n * np.hanning(n).sum() / n)
    if mag <= 0:
        return -np.inf
    return 20 * np.log10(mag / 32768.0)


def thd(y, f, nharm=10):
    n = len(y)
    w = np.hanning(n)
    spec = np.abs(np.fft.rfft(y.astype(np.float64) * w))
    norm = w.sum() / 2.0
    def binamp(k):
        return max(spec[k - 1], spec[k], spec[k + 1]) / norm
    f0 = binamp(int(round(f * n / FS)))
    if f0 <= 0:
        return float("inf")
    h = []
    for m in range(2, nharm + 1):
        if f * m >= 0.95 * FS / 2:   # above Nyquist: cannot exist
            break
        h.append(binamp(int(round(f * m * n / FS))))
    return float(np.sqrt(np.sum(np.square(h))) / f0)


# ---------------------------------------------------------------------------
def cmd_resp(a):
    """Small-signal frequency response of the whole chain (pre-limiter path)."""
    freqs = [20, 50, 100, 300, 1000, 2000, 3000, 5000, 8000, 10000, 12000,
             14000, 15000, 16000, 18000, 20000, 22000, 23000]
    n = 4 * FS
    amp = 800  # small enough that the +14 dB shelf never reaches the limiter
    chains = [
        ("75us", Chain(preemph=True, tc_us=75)),
        ("50us", Chain(preemph=True, tc_us=50)),
        ("300us", Chain(preemph=True, tc_us=300)),
        ("off", Chain(preemph=False)),
        ("nfm300", Chain(preemph=True, tc_us=300, nfm=True)),
        ("lp2only", Chain(lponly=True)),
    ]
    print("Frequency response (dB re input; small signal, limiter not engaged)")
    rows = {}
    for name, ch in chains:
        rows[name] = {}
        for f in freqs:
            x = sine(f, n, amp)
            y = ch.run(x)
            rows[name][f] = tone_db(y[FS:], f) - tone_db(x[FS:], f)
    print("  %-8s" % "f (Hz)" + "".join("%-10s" % n for n, _ in chains))
    for f in freqs:
        print("  %-8d" % f + "".join("%-10.2f" % rows[n][f] for n, _ in chains))
    print()
    print("  Net received response = transmit response - standard receiver")
    print("  de-emphasis (the radio applies the exact inverse of the standard")
    print("  curve, so this is what the listener actually hears):")
    print("  %-8s %10s %10s %10s" % ("f (Hz)", "75us net", "50us net", "300us net"))
    for f in freqs:
        row = [rows[k][f] - 10 * np.log10(1 + (2 * np.pi * f * tc * 1e-6) ** 2)
               for k, tc in (("75us", 75), ("50us", 50), ("300us", 300))]
        print("  %-8d %10.2f %10.2f %10.2f" % (f, row[0], row[1], row[2]))
    print()
    print("  Notes:")
    print("   - the +14 dB shelf ceiling (20*log10(5)) vs the standard 75us")
    print("     curve (20*log10(15000/2122) = +17.0 dB at 15 kHz)")
    print("   - preemph off + wideband = NO band-limit at all (see the report)")
    return None


def _resp(make, freqs, amp, secs=2):
    ch = make()
    out = {}
    for f in freqs:
        x = sine(f, secs * FS, amp)
        y = ch.run(x)
        out[f] = tone_db(y[FS:], f) - tone_db(x[FS:], f)
    return out


def _ideal_deemph_db(f, tc_us):
    return 10 * np.log10(1 + (2 * np.pi * f * tc_us * 1e-6) ** 2)


def cmd_mono(a):
    """Stereo -> mono mix + volume, i.e. what tud_audio_rx_done_isr() does
    before the DSP chain:  mono = (L + R) >> 1 ;  out = (mono * volume) >> 15.
    """
    rng = np.random.default_rng(7)
    n = 200000
    L = rng.integers(-32768, 32768, n).astype(np.int64)
    R = rng.integers(-32768, 32768, n).astype(np.int64)

    def rx(l, r, vol):
        return ((l + r) >> 1) * vol >> 15

    print("Stereo -> mono mix + volume (before the DSP chain)")
    print("  monotonic behaviour:")
    A = 30000
    print("    L=R=%d  -> %d  (unity, correlated content)" % (
        A, rx(np.int64(A), np.int64(A), 32767)))
    print("    L=+%d R=-%d -> %d  (anti-phase cancels: inherent to a mono mix)" % (
        A, A, rx(np.int64(A), np.int64(-A), 32767)))
    print("  quantization vs the ideal float mix+gain (random stereo, n=%d):" % n)
    print("  %-10s %-14s %-14s %s" % ("volume", "mean err (LSB)",
                                        "rms err (LSB)", "equiv. noise floor"))
    for vol, label in ((32767, "0 dB"), (16384, "-6 dB"), (3277, "-20 dB"),
                       (328, "-40 dB")):
        got = rx(L, R, vol).astype(np.float64)
        ideal = (L + R) / 2.0 * vol / 32768.0
        err = got - ideal
        rms = float(np.sqrt(np.mean(err ** 2)))
        floor = 20 * np.log10(max(rms, 1e-9) / 32768.0)
        print("  %-10s %-14.3f %-14.3f %.1f dBFS" % (
            label, float(np.mean(err)), rms, floor))
    print()
    print("  Reading: the mix is a plain average, so it is unity for correlated")
    print("  content (29999 vs 30000 is the truncation below), cancels anti-phase")
    print("  (L-R) content by definition, and cannot overflow (the int32 sum is")
    print("  halved).  The only artefact is truncating instead of rounding: at")
    print("  unity volume the mix floor (-0.25 LSB mean) and the volume floor add")
    print("  up to -0.75 LSB mean and 0.84 LSB rms = -91.8 dBFS, i.e. ~9 dB above")
    print("  an ideal rounder (0.289 LSB rms = -101.1 dBFS), and far below")
    print("  anything audible; the DC part is removed by the 2 Hz DC blocker.")
    print("  Verdict: no change warranted.")
    return None


def cmd_volume(a):
    """UAC1 volume mapping: old linear dB->gain vs the new table.

    Mirrors fm_modulator.c fm_db256_to_gain()/fm_gain_to_db256() exactly.
    """
    tbl = [32767, 29204, 26028, 23197, 20675, 18426, 16422, 14636,
           13045, 11626, 10362, 9235, 8231, 7336, 6538, 5827,
           5193, 4628, 4125, 3677, 3277, 2920, 2603, 2320,
           2067, 1843, 1642, 1464, 1304, 1163, 1036, 923,
           823, 734, 654, 583, 519, 463, 413, 368,
           328, 292, 260, 232, 207, 184, 164, 146,
           130, 116, 104, 92, 82, 73, 65, 58,
           52, 46, 41, 37, 33]

    def gain_new(db256):      # table + 1/256 dB interpolation
        mag = -db256
        if mag < 0:
            mag = 0
        if mag > 15360:
            mag = 15360
        i, f = mag >> 8, mag & 0xFF
        if i >= 60:
            return tbl[60]
        return tbl[i] + (((tbl[i + 1] - tbl[i]) * f) >> 8)

    def gain_old(db256):      # the old linear dB -> linear gain mapping
        c = max(-15360, min(0, db256))
        return ((c + 15360) * 32767) // 15360

    def db(g):
        return -999.0 if g <= 0 else 20 * np.log10(g / 32767.0)

    def db256_new(gain):      # inverse search (fm_gain_to_db256)
        if gain > 32767:
            gain = 32767
        for i in range(60):
            if gain >= tbl[i + 1]:
                span = tbl[i] - tbl[i + 1]
                off = tbl[i] - gain
                return -((i << 8) + (((off << 8) // span) if span > 0 else 0))
        return -15360

    print("UAC1 volume mapping (requested dB -> actual attenuation)")
    print("  %-10s %14s %10s %14s %10s" % (
        "requested", "old actual", "old err", "new actual", "new err"))
    for r in (-6, -12, -20, -30, -40, -50, -60):
        o, n = gain_old(r * 256), gain_new(r * 256)
        print("  %-10d %14.2f %+10.2f %14.2f %+10.2f" % (
            r, db(o), db(o) - r, db(n), db(n) - r))
    print()
    for lo in (-30, -40, -50, -60):
        worst = max(abs(db(gain_new(d)) - d / 256.0)
                    for d in range(lo * 256, 1))
        print("  worst new error over [%d, 0] dB: %.3f dB" % (lo, worst))
    rt = max(abs(gain_new(db256_new(g)) - g)
             for g in (tbl[0], tbl[10], tbl[30], tbl[59], tbl[60]))
    print("  table round-trip (gain -> dB256 -> gain) max error: %d" % rt)
    print()
    return None


def cmd_compare(a):
    """Before/after for the audio-quality rework (task-2)."""
    bl = [1000, 5000, 8000, 10000, 12000, 14000, 15000, 16000, 18000,
          20000, 23000]
    amp = 800  # small enough that the limiter never engages

    old_bl = _resp(lambda: Chain(lponly=True), bl, amp)
    new_bl = _resp(lambda: NewChain(preemph=False), bl, amp)
    print("Band-limit response (pre-emphasis off; dB re input)")
    print("  %-8s %10s %10s %10s" % ("f (Hz)", "old 1p x2", "new cheby", "delta"))
    for f in bl:
        print("  %-8d %10.2f %10.2f %+10.2f" % (
            f, old_bl[f], new_bl[f], new_bl[f] - old_bl[f]))
    print("  passband |droop| at 10 kHz: old %.2f dB -> new %.2f dB" % (
        -old_bl[10000], -new_bl[10000]))
    print("  stopband at 23 kHz:         old %.2f dB -> new %.2f dB" % (
        old_bl[23000], new_bl[23000]))
    print()

    print("Net received response (transmit - standard receiver de-emphasis);")
    print("this is what the listener hears.  0 dB = correct tone.")
    print("  %-8s %10s %10s %10s" % ("f (Hz)", "old 75us", "new 75us", "delta"))
    old = _resp(lambda: Chain(preemph=True, tc_us=75), bl, amp)
    new = _resp(lambda: NewChain(preemph=True, tc_us=75), bl, amp)
    for f in bl:
        o = old[f] - _ideal_deemph_db(f, 75)
        n = new[f] - _ideal_deemph_db(f, 75)
        print("  %-8d %10.2f %10.2f %+10.2f" % (f, o, n, n - o))
    worst_old = max(abs(old[f] - _ideal_deemph_db(f, 75)) for f in bl
                    if f <= 15000)
    worst_new = max(abs(new[f] - _ideal_deemph_db(f, 75)) for f in bl
                    if f <= 15000)
    print("  worst |error| in 0.3-15 kHz: old %.2f dB -> new %.2f dB" % (
        worst_old, worst_new))
    print()

    print("Harmonic distortion (THD %%, 1 s, harmonics 2..10)")
    print("  %-6s %-8s %10s %10s" % ("tone", "level", "old 75us", "new 75us"))
    for f in (1000, 5000, 10000):
        for lvl, label in ((4000, "linear"), (20000, "loud"), (32000, "over")):
            o = Chain(preemph=True, tc_us=75)
            n = NewChain(preemph=True, tc_us=75)
            x = sine(f, FS, lvl)
            to = thd(o.run(x)[FS // 2:], f)
            tn = thd(n.run(x)[FS // 2:], f)
            print("  %-6d %-8s %9.3f%% %9.3f%%" % (f, label, to * 100, tn * 100))
    print()

    print("Transient / limiter (step 0 -> 30000)")
    step = np.concatenate([np.zeros(FS // 2), np.full(FS // 2, 30000)])
    for name, ch in (("old", Chain(preemph=True, tc_us=75)),
                     ("new", NewChain(preemph=True, tc_us=75))):
        y = ch.run(step)
        i = FS // 2
        print("  %-4s samples above knee in first 200: %d, clips: %d, peak: %d" % (
            name, int(np.sum(np.abs(y[i:i + 200]) > FM_LIM_SOFT)), ch.clips,
            int(np.max(np.abs(y)))))
    print()

    print("Wideband chain with pre-emphasis disabled (the old code had NO")
    print("band-limit on this path):")
    print("  %-8s %10s %10s" % ("f (Hz)", "old off", "new off"))
    oo = _resp(lambda: Chain(preemph=False), [15000, 20000, 23000], amp)
    nn = _resp(lambda: NewChain(preemph=False), [15000, 20000, 23000], amp)
    for f in (15000, 20000, 23000):
        print("  %-8d %10.2f %10.2f" % (f, oo[f], nn[f]))
    print()
    return None


def cmd_thd(a):
    print("Harmonic distortion (THD, %), 1 s window, harmonics 2..10")
    print("  %-10s %-12s %-12s %-12s %s" % (
        "tone", "level", "preemph 75", "preemph off", "clips(75us)"))
    for f in (1000, 5000, 10000):
        for amp, label in ((4000, "linear"), (20000, "loud"), (32000, "over")):
            out = []
            clips = None
            for pre in (True, False):
                ch = Chain(preemph=pre, tc_us=75)
                y = ch.run(sine(f, FS, amp))
                out.append(thd(y[FS // 2:], f))
                if pre:
                    clips = ch.clips
            print("  %-10d %-12s %-12.3f %-12.3f %d" % (
                f, label, out[0] * 100, out[1] * 100, clips))
    print()


def cmd_limiter(a):
    """Peak deviation and clip rate on a stress signal (worst-case content)."""
    print("Limiter / peak-deviation behaviour")
    print("  a full-scale int16 sample maps to +/- deviation*32767/32768")
    print("  soft knee at 80%% (26214), 2:1 above, hard clip at full scale")
    print("  %-26s %10s %10s %10s" % ("signal", "peak_out", "peak/dev", "clips"))
    n = FS
    cases = [
        ("full-scale 1kHz sine", sine(1000, n, 32767)),
        ("full-scale 6kHz sine", sine(6000, n, 32767)),
        ("full-scale 12kHz sine", sine(12000, n, 32767)),
    ]
    # a pre-emphasised 12 kHz tone is the worst case for the shelf
    for name, x in cases:
        for pre in (True, False):
            ch = Chain(preemph=pre, tc_us=75)
            y = ch.run(x)
            tag = name + (" [+pre]" if pre else " [flat]")
            print("  %-26s %10d %10.4f %10d" % (
                tag, int(np.max(np.abs(y))),
                np.max(np.abs(y)) / 32768.0, ch.clips))
    # a step (transient) - shows the lack of look-ahead
    step = np.concatenate([np.zeros(FS // 2), np.full(FS // 2, 30000)])
    for pre in (True, False):
        ch = Chain(preemph=pre, tc_us=75)
        y = ch.run(step)
        i = FS // 2
        print("  %-26s overshoot=%d samples above knee, clips=%d" % (
            "step 0->30000 [%s]" % ("+pre" if pre else "flat"),
            int(np.sum(np.abs(y[i:i + 200]) > FM_LIM_SOFT)), ch.clips))
    print()


def _ring_sim(ppm, seconds, ring):
    """1 ms-resolution model of the SPSC ring; returns (underflows, drops).

    producer = host USB 48 kHz clock * (1 + ppm/1e6), consumer = exactly
    48 kHz.  Same counters the firmware now maintains.
    """
    per_ms = FS / 1000.0
    fill = ring // 2.0
    under = drops = 0
    frac = 0.0
    for _ in range(int(seconds * 1000)):
        frac += per_ms * (1 + ppm * 1e-6)
        n = int(frac)
        frac -= n
        space = (ring - 1) - fill
        if n > space:
            drops += int(n - space)
            fill = ring - 1
        else:
            fill += n
        if fill >= per_ms:
            fill -= per_ms
        else:
            under += int(round(per_ms - fill))
            fill = 0.0
    return under, drops


def _artifact(ppm, policy, secs=4.0, f=1000.0, amp=16000):
    """Inject one drift event every 1e6/ppm samples, run the DSP chain, and
    measure the worst sample-to-sample step and the THD.

    policy "zero" = the old behaviour (s_cur_sample = 0 on an empty ring);
    policy "hold" = repeat the previous sample (the new behaviour).
    """
    n = int(FS * secs)
    x = np.rint(amp * np.sin(2 * np.pi * f * np.arange(n) / FS)).astype(np.int64)
    period = int(round(1e6 / abs(ppm))) if ppm else 0
    if period:
        for i in range(period, n, period):
            x[i] = 0 if policy == "zero" else x[i - 1]
    ch = NewChain(preemph=True, tc_us=75)
    y = ch.run(x)
    return int(np.max(np.abs(np.diff(y)))), thd(y[FS:], f)


def cmd_drift(a):
    """Asynchronous producer (USB host clock) vs consumer (RP2040 PWM) drift."""
    ring = a.ring
    secs = a.seconds
    init = ring // 2
    print("Asynchronous sample-clock drift through the %d-sample ring" % ring)
    print("  producer = host USB 48 kHz clock, consumer = PWM 48 kHz from the")
    print("  RP2040 crystal.  No resampler and no feedback endpoint, so the two")
    print("  free-running crystals drift forever; the ring only delays the event.")
    print("  ppm > 0: host clock faster (ring fills -> drops); < 0: slower.")
    print()
    print("  Ring health counters over a %.0f s run.  These are policy-" % secs)
    print("  independent: the firmware counts the same events either way, so the")
    print("  counter proves the drift is happening / is fixed.")
    print("  %-6s %-14s %-16s %s" % (
        "ppm", "time to hit", "glitch rate", "counters: underflows / drops"))
    print("  %-6s %-14s %s" % ("", "the limit", "(samples/s)"))
    for ppm in (-100, -50, -25, -10, 10, 25, 50, 100):
        rate = FS * abs(ppm) * 1e-6
        t_hit = init / rate
        u, d = _ring_sim(ppm, secs, ring)
        print("  %-6d %-14s %-16.2f %d / %d" % (
            ppm, "%.0f s" % t_hit, rate, u, d))
    print()
    print("  Ring depth = %d samples = %.1f ms of buffering; it starts centred," % (
        ring, 1000.0 * ring / FS))
    print("  so it absorbs %.0f s of drift at 25 ppm before the first event." % (
        init / (FS * 25e-6)))
    print()

    base_step, base_thd = _artifact(0, "hold")
    print("  Audible effect of the drift events (1 kHz sine, -6 dBFS, through the")
    print("  current DSP chain; 4 s window):")
    print("  %-8s %-16s %-16s %-12s %s" % (
        "ppm", "worst |dy| hold", "worst |dy| zero", "THD zero", "THD hold"))
    for ppm in (10, 25, 50, 100):
        hs, ht = _artifact(ppm, "hold")
        zs, zt = _artifact(ppm, "zero")
        print("  %-8d %-16d %-16d %9.3f%% %8.3f%%" % (ppm, hs, zs, zt * 100,
                                                          ht * 100))
    print("  %-8s %-16d %-16s %9s %8.3f%%" % (
        "clean", base_step, "-", "-", base_thd * 100))
    print()
    print("  Reading: with hold-last the worst sample-to-sample step stays at the")
    print("  clean-signal value (a duplicated sample only advances the waveform")
    print("  by one sample), while zeroing injects a near-full-scale step on every")
    print("  event.  The counters show the same number of events in both cases:")
    print("  the fix changes the bite, not the count.")
    print()
    print("  Residual limits and upgrade path:")
    print("   - the counters make the drift visible but do not correct it, and")
    print("     hold-last removes the click, not the timing error")
    print("   - a real fix is an asynchronous sample-rate converter (ASRC) or a")
    print("     UAC1 feedback endpoint so the host tracks the PWM clock; both are")
    print("     larger changes (ISR budget / TinyUSB feedback support)")
    print("   - a cheaper middle ground is to trim the ring in software, dropping")
    print("     or duplicating one sample with linear interpolation whenever the")
    print("     fill crosses 50%: that removes the event class, not just its bite")
    print()
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn in (("resp", cmd_resp), ("thd", cmd_thd),
                     ("limiter", cmd_limiter), ("drift", cmd_drift),
                     ("mono", cmd_mono), ("volume", cmd_volume),
                     ("compare", cmd_compare)):
        p = sub.add_parser(name)
        p.set_defaults(func=fn)

    d = sub.choices["drift"]
    d.add_argument("--ring", type=int, default=4096)
    d.add_argument("--seconds", type=float, default=600.0)

    r = sub.choices["resp"]
    r.add_argument("--tc", type=int, default=75)
    r.add_argument("--nfm", action="store_true")

    p = sub.add_parser("all")
    p.set_defaults(func=None)

    a = ap.parse_args()
    if a.cmd == "all":
        for fn in (cmd_resp, cmd_thd, cmd_limiter, cmd_drift, cmd_volume):
            ns = argparse.Namespace(ring=4096, seconds=600.0)
            print("=" * 72)
            print(fn.__name__.replace("cmd_", "").upper())
            print("=" * 72)
            fn(ns)
        return 0
    return a.func(a) or 0


if __name__ == "__main__":
    sys.exit(main())
