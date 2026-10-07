#!/usr/bin/env python3
"""Generate the test signals used by the hardware audio tests.

Writes 48 kHz stereo 16-bit WAVs (matching the UAC1 stream) into a directory:

    pink.wav    30 s pink noise, -12 dBFS   tonal balance / listening test
    sweep.wav   20 s log sweep 20 Hz-20 kHz, -26 dBFS   response measurement
    tone1k.wav  10 s 1 kHz sine, -12 dBFS   level / clipping checks

Usage:
    python3 tools/make_test_audio.py [outdir]     # default: ./fm_test_audio

Play one to the board (card 1 is the RP2040 when it is the only USB audio
device; check with `aplay -l`):

    aplay -D plughw:1,0 pink.wav

The levels are chosen so the sweep stays below the limiter (linear chain) and
pink/1 kHz exercise it gently, like real programme material.
"""

import os
import sys
import wave

import numpy as np

FS = 48_000


def write_wav(path, mono):
    """mono float in [-1, 1] -> stereo 16-bit WAV."""
    x = np.clip(mono, -1.0, 1.0)
    s16 = np.rint(x * 32767).astype("<i2")
    stereo = np.repeat(s16, 2)          # L = R, i.e. unity after the mono mix
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(FS)
        w.writeframes(stereo.tobytes())


def pink(n, rng, amp):
    """White noise shaped to -3 dB/octave (pink) in the frequency domain."""
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1.0 / FS)
    f[0] = f[1]
    spec /= np.sqrt(f)
    x = np.fft.irfft(spec, n)
    return x / np.max(np.abs(x)) * amp


def log_sweep(n, f0, f1, amp):
    t = np.arange(n) / FS
    T = n / FS
    k = np.log(f1 / f0) / T
    phase = 2 * np.pi * f0 * (np.exp(k * t) - 1) / k
    return amp * np.sin(phase)


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else "fm_test_audio"
    os.makedirs(outdir, exist_ok=True)
    rng = np.random.default_rng(1)

    files = [
        ("pink.wav", pink(30 * FS, rng, 0.25)),
        ("sweep.wav", log_sweep(20 * FS, 20.0, 20_000.0, 0.05)),
        ("tone1k.wav", 0.25 * np.sin(2 * np.pi * 1000 * np.arange(10 * FS) / FS)),
    ]
    for name, x in files:
        path = os.path.join(outdir, name)
        write_wav(path, x)
        peak = 20 * np.log10(max(np.max(np.abs(x)), 1e-9))
        print("%-12s %5.1f s  peak %+.1f dBFS  (%s)" % (
            name, len(x) / FS, peak, path))
    print()
    print("play with:  aplay -D plughw:1,0 %s/pink.wav" % outdir)


if __name__ == "__main__":
    main()
