#!/usr/bin/env python3
"""Host-side replica of the pico-fractional-pll divider search.

This is a byte-for-byte port of calculate_pll_divider() / is_div_possible()
from ports/rp2/tx/pico_fractional_pll.c (see
patches/micropython-tx.patch).  It answers, without hardware, the only
question that decides whether a band is usable at all:

    "can the fractional PLL produce a range that covers carrier +/- deviation?"

and reports the PDM dither step (ref / div), i.e. the instantaneous frequency
jump between the two feedback-divider values the core1 loop alternates
between.  That step is the hard, computable quality metric of this RF chain:
the modulation *average* is arbitrarily fine (32-bit accumulator), but the
instantaneous wobble is one full step at the 1 MHz PDM rate.

Pure stdlib, no hardware, no MicroPython.  Usage:

    python3 tools/pll_range.py check 87900000 75000
    python3 tools/pll_range.py check 28400000 3 2
    python3 tools/pll_range.py scan 1800000 30000000 10000 3000
    python3 tools/pll_range.py bands
    python3 tools/pll_range.py wsjt
"""

import argparse
import sys

XO_HZ = 12_000_000               # crystal reference (RP2040 XIN)
VCO_FREQ_MIN = 750_000_000       # PLL VCO range, pico_fractional_pll.c
VCO_FREQ_MAX = 1_600_000_000
FOUTPOSTDIV_FREQ_MAX = 150_000_000  # post-divider output ceiling

# possible_postdiv_values[][] from pico_fractional_pll.c (postdiv1, postdiv2)
POSSIBLE_POSTDIV = [
    (1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (6, 1), (7, 1),
    (4, 2), (3, 3), (5, 2), (6, 2), (7, 2), (5, 3), (4, 4), (6, 3),
    (5, 4), (7, 3), (6, 4), (5, 5), (7, 4), (6, 5), (7, 5), (6, 6),
    (7, 6), (7, 7),
]


def _is_div_possible(vco_freq, div):
    """Port of is_div_possible().  Returns (postdiv1, postdiv2, clkdiv) or None."""
    for postdiv1, postdiv2 in reversed(POSSIBLE_POSTDIV):
        postdiv = postdiv1 * postdiv2
        if div % postdiv == 0 and (vco_freq // postdiv) <= FOUTPOSTDIV_FREQ_MAX:
            return postdiv1, postdiv2, div // postdiv
    return None


def iter_solutions(freq_range_min, freq_range_max, refdiv=1):
    """Yield every divider solution in exactly the order the C considers them.

    Port of the calculate_pll_divider() loops: pass 0 (integer-only) first,
    then pass 1 (remainder edges accepted), each scanning fbdiv_high from the
    maximum downwards.  find_divider() returns the first item yielded.
    """
    ref = XO_HZ // refdiv
    fbdiv_max = VCO_FREQ_MAX // ref
    fbdiv_min = (VCO_FREQ_MIN + ref - 1) // ref

    for p in range(2):  # pass 0: integer only, pass 1: accept remainders
        for fbdiv_high in range(fbdiv_max, fbdiv_min, -1):
            fbdiv_low = fbdiv_high - 1
            vco_freq_low = ref * fbdiv_low
            vco_freq_high = ref * fbdiv_high

            div_low = (vco_freq_low + freq_range_min - 1) // freq_range_min
            div_high = vco_freq_high // freq_range_max
            if div_low != div_high:
                continue  # only a single div value is supported for the range

            div = div_high
            poss = _is_div_possible(vco_freq_high, div)
            if poss is None:
                continue
            postdiv1, postdiv2, clkdiv = poss

            div_freq_low = vco_freq_low // div
            div_freq_high = vco_freq_high // div
            if (vco_freq_low % div or vco_freq_high % div) and p == 0:
                continue

            yield {
                "pass": p,
                "freq_low": div_freq_low,
                "freq_high": div_freq_high,
                "fbdiv_low": fbdiv_low,
                "fbdiv_high": fbdiv_high,
                "div": div,
                "postdiv1": postdiv1,
                "postdiv2": postdiv2,
                "clkdiv": clkdiv,
                "step_hz": div_freq_high - div_freq_low,  # ref / div
                "non_integer": bool(vco_freq_low % div or vco_freq_high % div),
            }


def find_divider(freq_range_min, freq_range_max, refdiv=1):
    """Port of calculate_pll_divider().  Returns a dict or None.

    freq_range_min/max are the *requested output range* in Hz (what
    pico_tx.init() receives as carrier-deviation / carrier+deviation).
    """
    for sol in iter_solutions(freq_range_min, freq_range_max, refdiv):
        return sol
    return None


def check(carrier, dev, refdiv=1):
    """Return (result, refdiv) for a carrier/deviation window."""
    return find_divider(carrier - dev, carrier + dev, refdiv)


def fmt_check(carrier, dev, refdiv=1):
    r = check(carrier, dev, refdiv)
    head = "carrier=%10.3f kHz dev=%7.1f Hz refdiv=%d" % (
        carrier / 1e3, dev, refdiv)
    if r is None:
        return head + " -> FAIL (no single-divider solution)"
    return (head + " -> OK  div=%-4d postdiv=%dx%d clkdiv=%-3d "
            "PLL range %.3f..%.3f MHz  PDM step=%s%s" % (
                r["div"], r["postdiv1"], r["postdiv2"], r["clkdiv"],
                r["freq_low"] / 1e6, r["freq_high"] / 1e6,
                _fmt_hz(r["step_hz"]),
                "  [non-integer edges accepted]" if r["non_integer"] else ""))


def _fmt_hz(hz):
    if hz >= 1e6:
        return "%.3f MHz" % (hz / 1e6)
    if hz >= 1e3:
        return "%.3f kHz" % (hz / 1e3)
    return "%.3f Hz" % hz


# Amateur HF/VHF/UHF band plan entries used by the project (dial frequencies
# are the common narrow-band calling frequencies / the band centres).
BANDS = [
    ("160m", 1_840_000), ("80m", 3_573_000), ("60m", 5_357_000),
    ("40m", 7_074_000), ("30m", 10_136_000), ("20m", 14_074_000),
    ("17m", 18_100_000), ("15m", 21_074_000), ("12m", 24_915_000),
    ("10m", 28_074_000), ("6m", 50_313_000),
    ("2m", 145_000_000), ("FM", 87_900_000),
    ("433", 433_920_000), ("446", 446_006_000),
]

# WSJT-X modes: tone spacing and per-symbol duration (informational; the PLL
# only ever sees "shift the carrier by N * spacing for T ms").
WSJT_MODES = {
    "FT8":  (6.25, 0.160),
    "FT4":  (20.83, 0.048),
    "JT65": (2.69, 0.372),
    "JT9":  (1.736, 0.576),
    "Q65":  (None, None),   # many sub-modes, all narrower than the above
    "WSPR": (1.4648, 0.683),
}


def cmd_solutions(a):
    """List every divider solution for a window and check that the one the C
    returns (the first) is the one with the smallest PDM dither step."""
    sols = list(iter_solutions(a.carrier - a.dev, a.carrier + a.dev, a.refdiv))
    print("carrier=%d dev=%d refdiv=%d -> %d solution(s)" % (
        a.carrier, a.dev, a.refdiv, len(sols)))
    if not sols:
        print("  none: this window is not achievable with this refdiv")
        return 1
    print("  %-4s %-6s %-16s %-6s %-14s %s" % (
        "idx", "pass", "fbdiv range", "div", "PDM step", "used by C"))
    for i, s in enumerate(sols[:a.limit]):
        print("  %-4d %-6d %-16s %-6d %-14s %s" % (
            i, s["pass"], "%d..%d" % (s["fbdiv_low"], s["fbdiv_high"]),
            s["div"], _fmt_hz(s["step_hz"]), "<- yes" if i == 0 else ""))
    if len(sols) > a.limit:
        print("  ... %d more" % (len(sols) - a.limit))
    first = sols[0]
    same_pass = [s for s in sols if s["pass"] == first["pass"]]
    best = min(s["step_hz"] for s in same_pass)
    print("  C uses idx 0: pass %d, step %s" % (first["pass"], _fmt_hz(first["step_hz"])))
    print("  smallest step among pass-%d solutions: %s" % (
        first["pass"], _fmt_hz(best)))
    print("  VERDICT: %s" % ("first == minimum step" if first["step_hz"] == best
                              else "MISMATCH - the C should prefer a later solution"))
    return 0


def cmd_minstep(a):
    """Sweep many windows and verify the first-found divider solution always
    has the minimum PDM step within its pass."""
    checked = mismatch = nosol = 0
    examples = []
    f = a.lo
    while f <= a.hi:
        sols = list(iter_solutions(f - a.dev, f + a.dev, a.refdiv))
        if not sols:
            nosol += 1
        else:
            checked += 1
            first = sols[0]
            best = min(s["step_hz"] for s in sols if s["pass"] == first["pass"])
            if first["step_hz"] != best:
                mismatch += 1
                if len(examples) < 5:
                    examples.append((f, first["step_hz"], best))
        f += a.step
    print("min-step check: %.4f..%.4f MHz step %.1f kHz, dev %.1f Hz, refdiv %d" % (
        a.lo / 1e6, a.hi / 1e6, a.step / 1e3, a.dev, a.refdiv))
    print("  windows with a solution: %d, without: %d" % (checked, nosol))
    print("  first-found is NOT the minimum step in %d window(s)" % mismatch)
    for f, got, best in examples:
        print("    %.4f MHz: first %s, best %s" % (f / 1e6, _fmt_hz(got), _fmt_hz(best)))
    return 1 if mismatch else 0


def cmd_check(a):
    print(fmt_check(a.carrier, a.dev, a.refdiv))


def cmd_scan(a):
    ok = fail = 0
    first = last = None
    fails = []
    f = a.lo
    while f <= a.hi:
        r = check(f, a.dev, a.refdiv)
        if r is None:
            fail += 1
            fails.append(f)
        else:
            ok += 1
            if first is None:
                first = f
            last = f
        f += a.step
    print("scan %.4f..%.4f MHz step %.1f kHz, dev %.1f Hz, refdiv %d" % (
        a.lo / 1e6, a.hi / 1e6, a.step / 1e3, a.dev, a.refdiv))
    print("  OK %d  FAIL %d  first_ok=%s last_ok=%s" % (
        ok, fail,
        "%.4f MHz" % (first / 1e6) if first else "none",
        "%.4f MHz" % (last / 1e6) if last else "none"))
    if fails:
        print("  FAIL points: %s%s" % (
            " ".join("%.4f" % (x / 1e6) for x in fails[:20]),
            " ..." if len(fails) > 20 else ""))
    return 1 if (a.strict and fail) else 0


def pick_harmonic(target, ceiling=FOUTPOSTDIV_FREQ_MAX):
    """Port of main.py pick_harmonic(), as *used* there: only called when
    target exceeds the ceiling; targets below it are transmitted directly."""
    if target <= ceiling:
        return 1
    for n in (3, 5, 7):
        if target // n <= ceiling:
            return n
    return 1


def cmd_bands(a):
    print("%-5s %10s  %-6s %-6s  %s" % (
        "band", "dial Hz", "refdiv1", "refdiv2", "detail (best refdiv)"))
    for name, f in BANDS:
        # 3 kHz is a typical NB voice / CW-class window; the WSJT modes need
        # far less and are checked separately by `wsjt`.
        dev = 3000
        n = pick_harmonic(f)
        fund = round(f / n)
        fund_dev = max(1, round(dev / n))
        line = "%-5s %10d " % (name, f)
        best = None
        for rd in (1, 2):
            r = check(fund, fund_dev, rd)
            line += " %-6s" % ("OK" if r else "FAIL")
            if r and best is None:
                best = (rd, r)
        tag = "" if n == 1 else " [x%d harmonic of %.3f MHz]" % (n, fund / 1e6)
        if best:
            rd, r = best
            line += "  refdiv=%d div=%d PDM step=%s%s (on air: %s)" % (
                rd, r["div"], _fmt_hz(r["step_hz"]), tag,
                _fmt_hz(r["step_hz"] * n))
        else:
            line += "  no solution at 3 kHz window" + tag
        print(line)


def cmd_wsjt(a):
    """The decisive test for WSJT-X: can the PLL cover a very narrow window?

    A direct-FSK implementation only needs carrier +- ~50 Hz (FT8 spans
    0..43.75 Hz above the dial), not a 3 kHz FM window - so the window is the
    one thing WSJT-X needs that no current code path requests.
    """
    print("WSJT-X direct-FSK windows (refdiv 1 then 2), dev = half-span")
    for name, f in BANDS:
        if name not in ("160m", "80m", "40m", "30m", "20m", "17m",
                        "15m", "12m", "10m", "6m", "2m"):
            continue
        for label, dev in (("+-50 Hz", 50), ("+-200 Hz", 200)):
            r1 = check(f, dev, 1)
            r2 = check(f, dev, 2)
            print("%-5s %10d %-9s refdiv1=%-4s refdiv2=%-4s %s" % (
                name, f, label,
                "OK" if r1 else "FAIL", "OK" if r2 else "FAIL",
                ("step " + _fmt_hz((r1 or r2)["step_hz"])) if (r1 or r2) else ""))
    print()
    print("WSJT-X mode geometry (tone spacing / symbol length):")
    for m, (sp, sym) in WSJT_MODES.items():
        print("  %-5s spacing=%-8s symbol=%-7s" % (
            m, "-" if sp is None else "%.4f Hz" % sp,
            "-" if sym is None else "%.3f s" % sym))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="one carrier/deviation window")
    c.add_argument("carrier", type=int)
    c.add_argument("dev", type=int)
    c.add_argument("refdiv", type=int, nargs="?", default=1)
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("scan", help="scan a frequency range")
    s.add_argument("lo", type=int)
    s.add_argument("hi", type=int)
    s.add_argument("step", type=int, default=10_000, nargs="?")
    s.add_argument("dev", type=int, default=3000, nargs="?")
    s.add_argument("--refdiv", type=int, default=2)
    s.add_argument("--strict", action="store_true",
                   help="exit non-zero if any point fails (for CI)")
    s.set_defaults(func=cmd_scan)

    b = sub.add_parser("bands", help="amateur band table")
    b.set_defaults(func=cmd_bands)

    sol = sub.add_parser("solutions", help="all divider solutions for a window")
    sol.add_argument("carrier", type=int)
    sol.add_argument("dev", type=int)
    sol.add_argument("refdiv", type=int, nargs="?", default=1)
    sol.add_argument("--limit", type=int, default=20)
    sol.set_defaults(func=cmd_solutions)

    ms = sub.add_parser("minstep", help="verify first-found == minimum step")
    ms.add_argument("lo", type=int)
    ms.add_argument("hi", type=int)
    ms.add_argument("step", type=int, nargs="?", default=100_000)
    ms.add_argument("dev", type=int, nargs="?", default=3000)
    ms.add_argument("--refdiv", type=int, default=2)
    ms.set_defaults(func=cmd_minstep)

    w = sub.add_parser("wsjt", help="WSJT-X narrow-window feasibility")
    w.set_defaults(func=cmd_wsjt)

    a = ap.parse_args()
    sys.exit(a.func(a) or 0)


if __name__ == "__main__":
    main()
