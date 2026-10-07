#!/usr/bin/env python3
"""Design and quantise the fixed-point audio filters used by tx_modulator.c.

Designs a Chebyshev type I low-pass (0.2 dB ripple) as a cascade of
second-order sections, applies the bilinear transform at fs = 48 kHz with
pre-warping, normalises each section to unity DC gain, then quantises the
coefficients to Q15 and reports the response error of the quantised cascade
against the ideal one.

Also emits the C constants for the pre-emphasis differentiator
(G = tau * fs in Q10).

Usage:
    python3 tools/design_filters.py            # 15 kHz wideband + 3 kHz NFM
    python3 tools/design_filters.py --emit-c   # print the C blocks only
"""

import argparse
import cmath
import math

FS = 48_000


def cheby1_poles(order, ripple_db):
    """Analog Chebyshev type I prototype poles (ripple band edge at w = 1)."""
    eps = math.sqrt(10 ** (ripple_db / 10) - 1)
    a = math.asinh(1 / eps) / order
    poles = []
    for k in range(order):
        theta = math.pi * (2 * k + 1) / (2 * order)
        poles.append(complex(-math.sinh(a) * math.sin(theta),
                             math.cosh(a) * math.cos(theta)))
    return poles


def design(order, fc, ripple_db=0.2, fs=FS):
    """Return a list of (b0,b1,b2,a1,a2) float sections, unity DC gain each."""
    omega = 2 * fs * math.tan(math.pi * fc / fs)   # pre-warped edge
    scale = omega
    poles = [p * scale for p in cheby1_poles(order, ripple_db)]
    # bilinear transform s -> 2*fs*(1-z^-1)/(1+z^-1)
    zd = [(1 + p / (2 * fs)) / (1 - p / (2 * fs)) for p in poles]

    # pair each upper-half-plane pole with its conjugate; order the sections
    # from lowest Q to highest Q (minimises the internal dynamic range)
    uniq = [z for z in zd if z.imag > 0]
    uniq.sort(key=lambda z: abs(z.real), reverse=True)   # lowest Q first

    sections = []
    for p in uniq:
        a1 = -2 * p.real
        a2 = abs(p) ** 2
        g = (1 + a1 + a2) / 4.0          # unit DC gain (numerator DC = (1+1)^2)
        sections.append((g, 2 * g, g, a1, a2))
    return sections


def quantise(sections, q=15):
    return [tuple(int(round(c * (1 << q))) for c in s) for s in sections]


def response(sections, q, freqs, fs=FS):
    """Magnitude (dB) of a cascade of Qq sections at the given frequencies."""
    out = []
    for f in freqs:
        z = cmath.exp(2j * math.pi * f / fs)
        h = 1 + 0j
        for b0, b1, b2, a1, a2 in sections:
            if q is not None:
                sc = float(1 << q)
                b0, b1, b2, a1, a2 = (b0 / sc, b1 / sc, b2 / sc, a1 / sc, a2 / sc)
            h *= (b0 + b1 / z + b2 / z ** 2) / (1 + a1 / z + a2 / z ** 2)
        out.append(20 * math.log10(max(abs(h), 1e-12)))
    return out


def max_state(sections, q, amp=32767, n=20000, f=1000.0):
    """Peak internal state under a loud sine, with the Qq fixed-point form.

    Mirrors the C implementation exactly:
        y  = ((b0*x) >> q) + s1
        s1 = ((b1*x) >> q) - ((a1*y) >> q) + s2
        s2 = ((b2*x) >> q) - ((a2*y) >> q)
    """
    peak = 0
    state = [[0, 0] for _ in sections]
    for i in range(n):
        x = int(round(amp * math.sin(2 * math.pi * f * i / FS)))
        for k, (b0, b1, b2, a1, a2) in enumerate(sections):
            s1, s2 = state[k]
            y = ((b0 * x) >> q) + s1
            state[k][0] = ((b1 * x) >> q) - ((a1 * y) >> q) + s2
            state[k][1] = ((b2 * x) >> q) - ((a2 * y) >> q)
            peak = max(peak, abs(state[k][0]), abs(state[k][1]),
                       abs(y) if k == len(sections) - 1 else 0)
            x = y
    return peak


def report(name, order, fc, freqs):
    sections = design(order, fc)
    qs = quantise(sections, 15)
    ideal = response(sections, None, freqs)
    quant = response(qs, 15, freqs)
    print("== %s: Chebyshev I order %d, 0.2 dB ripple, fc = %g Hz ==" % (
        name, order, fc))
    print("   ideal (float) sections: %s" % (
        "  ".join("b=[%.6f %.6f %.6f] a=[1 %.6f %.6f]" % s for s in sections)))
    print("   Q15 sections:")
    for i, s in enumerate(qs):
        print("     s%d: b=[%d %d %d] a=[%d %d]" % (i, *s))
    print("   %-8s %10s %10s %10s" % ("f (Hz)", "ideal", "Q15", "error"))
    for f, a, b in zip(freqs, ideal, quant):
        print("   %-8g %10.2f %10.2f %10.2f" % (f, a, b, b - a))
    print("   peak internal state at full scale: %d (int32 max 2147483647)"
          % max_state(qs, 15))
    print()
    return qs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-c", action="store_true")
    a = ap.parse_args()

    wide = [50, 300, 1000, 3000, 5000, 8000, 10000, 12000, 14000, 15000,
            16000, 18000, 20000, 23000]
    nfm = [100, 300, 500, 1000, 2000, 2500, 3000, 3500, 4000, 5000, 6000,
           8000, 12000]

    if not a.emit_c:
        q_wide = report("wideband band-limit", 4, 15000, wide)
        q_nfm = report("NFM voice band-limit", 2, 3400, nfm)
    else:
        q_wide = quantise(design(4, 15000), 15)
        q_nfm = quantise(design(2, 3400), 15)

    print("== pre-emphasis differentiator G = tau * fs (Q10) ==")
    for us in (50, 75, 300):
        g = us * 1e-6 * FS
        print("   tau=%3d us -> G=%.4f -> Q10 %d (%.4f)" % (
            us, g, round(g * 1024), round(g * 1024) / 1024))
    print("   HF boost at 15 kHz: %.1f dB" % (
        20 * math.log10(abs(1 + 3.6 * (1 - cmath.exp(-2j * math.pi * 15000 / FS))))))
    print()

    if a.emit_c:
        print("// ---- generated by tools/design_filters.py ----")
        print("// wideband: Chebyshev I, order 4, 0.2 dB ripple, fc = 15 kHz")
        for i, (b0, b1, b2, a1, a2) in enumerate(q_wide):
            print("static fm_biquad_t s_bq_w%d = { %d, %d, %d, %d, %d, 0, 0 };"
                  % (i, b0, b1, b2, a1, a2))
        print("// NFM: Chebyshev I, order 2, 0.2 dB ripple, fc = 3.4 kHz")
        for i, (b0, b1, b2, a1, a2) in enumerate(q_nfm):
            print("static fm_biquad_t s_bq_n%d = { %d, %d, %d, %d, %d, 0, 0 };"
                  % (i, b0, b1, b2, a1, a2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
