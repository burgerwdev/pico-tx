# pico-tx console for the RPI_PICO_TX MicroPython firmware.
#
# Starts the transmitter, then provides an interactive serial console.
# Connect with a serial terminal (screen/minicom/PuTTY on the CDC port,
# 115200 baud) or run from Thonny.  Type `help` for the command list.
#
# Carrier/deviation can be persisted: `reinit` saves them to /tx_cfg.json and
# reboots, so the new values are used on every boot.
#
# WARNING: GPIO21 outputs a strong RF signal. Do NOT attach an antenna wire;
# radiating from this pin is most likely illegal in your country.

import array
import json
import machine
import os
import select
import sys
import time

import pico_tx

VERSION = "0.1.0"          # console release version (see release README)

# SHA-256 of the firmware this console is shipped with
# (release/firmware/picotx_firmware.uf2).  Shown by `ver`.
FW_SHA256 = "ee5c02ca67d3bf92988c75685b9e4f941dbc379d41acc8edece618a8fa81f224"

# Project links shown by the `ver` command.
PROJECT_URL = "https://git.sr.ht/~bytewolf/rp2040-fm-transmitter"
BILIBILI_URL = "https://space.bilibili.com/28447213"

RF_PIN = 21                # GP21 = CLK_GPOUT0 (configurable: 21/23/24/25)

# LED configuration: False = plain PWM LED on GPIO25 (or ledpin <0-29>);
# True = WS2812 NeoPixel (e.g. RP2040-Zero onboard LED), driven from Python.
LED_WS2812 = False
WS2812_PIN = 16

CFG_FILE = "/tx_cfg.json"
DEFAULT_CARRIER = 87_900_000
DEFAULT_DEVIATION = 75_000

# Default audio-processing settings (restored by `reset`).
# PREEMPH: "on" = 75us (default), "50" = 50us, "off" = disabled.
DEFAULT_PREEMPH = "on"
# Weak-sample mute (sq) threshold in % of full scale; 0 = off.
DEFAULT_SQUELCH = 0

# Broad limits for the `reinit` command (the PLL itself further restricts
# what is actually achievable; init() raises if a range is unusable).
REINIT_CARRIER_MIN = 10_000_000
# Targets above the fundamental ceiling (~150MHz, hardware limit) are reached
# via an ODD harmonic of the square wave (3rd/5th), which is what a UHF radio
# actually receives.  The console converts automatically; 470MHz covers the
# full 409/433/440 UHF receive range of a handheld radio.
REINIT_CARRIER_MAX = 470_000_000
HARMONIC_CEILING = 150_000_000   # max usable FUNDAMENTAL (PLL/GPOUT limit)
REINIT_DEV_MIN = 1000
REINIT_DEV_MAX = 500_000

# Console-tracked state (the firmware exposes the audio params but not these
# switches, so the console remembers what it set).
AUDIO_ON = True
# "on" | "50" | "off" - current pre-emphasis setting (on == 75us).
PREEMPH = DEFAULT_PREEMPH
# Squelch threshold in % of full scale (0 = off), mirrored from the firmware.
SQUELCH_PCT = DEFAULT_SQUELCH

# Harmonic operation: when the target frequency is above HARMONIC_CEILING the
# PLL runs on the fundamental (target/N) and the radio hears the Nth odd
# harmonic.  TARGET_FREQ / DEV_EFF are what the radio actually sees; the
# firmware is fed target/N and dev_eff/N.
HARMONIC = 1
TARGET_FREQ = DEFAULT_CARRIER
DEV_EFF = DEFAULT_DEVIATION
# Fine frequency trim (effective Hz, what the radio sees).  Compensates the
# crystal tolerance, which the harmonic multiplies (x3 on UHF: ~+5-8kHz).
TRIM_HZ = 0

# Silence handling.  Broadcast FM (87.5-108MHz) parks the unmodulated carrier
# on fc so an FM radio outputs silence; narrowband bands (2m/UHF, handheld
# radios) key the RF output off on silence instead, because a parked
# off-tune carrier is heard as a continuous tone by a handheld receiver.
# "auto" derives the mode from the band; the console stores the resolved
# value in /tx_cfg.json after each band change.
SILENCE_MODE = "auto"      # "auto" | "park" | "gate"

# General transmit scheme (mirrors pico_tx.mode()).
MODE = "fm"
# Boot service: a console command run automatically after setup (headless
# transmit).  Empty = interactive only.
SERVICE = ""
# Interactive console on/off.  When off the device stays in a service loop
# (still recoverable: Ctrl-C drops to the MicroPython REPL).
CONSOLE_ON = True
# Last chirp parameters (f0, f1, ms) shown by the `log` view.
LAST_CHIRP = (0, 0, 0)
# Short description of the last transmission (payload) for `log`/`status`.
LAST_TX = ""

# ITU Morse timing (dots/dashes/dah) - sent by the `cw` command.
MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".",
    "F": "..-.", "G": "--.", "H": "....", "I": "..", "J": ".---",
    "K": "-.", "L": ".-..", "M": "--", "N": "-.", "O": "---",
    "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-",
    "U": "..-", "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--",
    "Z": "--..", "0": "-----", "1": ".----", "2": "..---",
    "3": "...--", "4": "....-", "5": ".....", "6": "-....",
    "7": "--...", "8": "---..", "9": "----.", ".": ".-.-.-",
    ",": "--..--", "?": "..--..", "/": "-..-.", "-": "-....-",
    "=": "-...-", "+": ".-.-.", "@": ".--.-.",
}


def silence_gate_resolve(target):
    """Resolve the silence-gate mode for a band: broadcast FM parks the
    carrier; everything else (narrowband, handheld radios) gates it off."""
    return not (87_500_000 <= target <= 108_000_000)


def refdiv_for(target):
    """PLL reference divider for a band: 2 unless the carrier is parked.

    refdiv 2 halves the PDM dither step (ref/div, the instantaneous frequency
    jump between the two feedback-divider values) on every band; that step is
    what sets the residual frequency ripple a narrowband receiver hears.  It
    must NOT be used where the *unmodulated* carrier is parked for silence
    (broadcast FM 87.5-108 MHz): the parked PDM pattern changes and puts an
    audible idle tone on the silent carrier (hardware-observed).  Bands that
    key the RF off when silent have no parked carrier, so they take refdiv 2.

    Evidence: tools/pll_range.py (see docs/*/audio-quality.md).  Half the
    step, e.g. 87.9 MHz 750 kHz -> 375 kHz, 145 MHz 1.2 MHz -> 600 kHz.
    """
    return 2 if silence_gate_resolve(target) else 1


def pll_limits():
    """(lo, hi) actual PLL output range in Hz, and max sensible deviation."""
    lo, hi = pico_tx.range()
    return lo, hi, (hi - lo) // 2


def pick_harmonic(target):
    """Smallest odd harmonic N whose fundamental target/N fits under the
    hardware ceiling (odd harmonics of the 50%%-duty square wave are strong,
    even ones are suppressed).  Returns 1 for direct (fundamental) use."""
    for n in (3, 5, 7):
        if target // n <= HARMONIC_CEILING:
            return n
    return 1


def fundamental_of(target, harmonic):
    """Fundamental frequency (Hz) for an effective target at the harmonic."""
    return max(1, round(target / harmonic))


def dev_fund_of(dev_eff, harmonic):
    """Fundamental full-scale deviation (Hz) for an effective deviation."""
    return max(1, round(dev_eff / harmonic))


def pll_step_str():
    """The PDM dither step (ref/div) as a short string, for `status`.

    pico_tx.range() returns the window the divider search accepted, which is
    exactly one feedback-divider step wide (freq_delta = ref/div).  That width
    is the instantaneous frequency jump the core1 PDM loop produces - the
    hard limit behind the residual RF ripple.  Fundamental value; a harmonic
    band multiplies it by the harmonic at the radio.
    """
    lo, hi = pico_tx.range()
    d = hi - lo
    if d >= 1_000_000:
        return "%.2f MHz" % (d / 1e6)
    if d >= 1_000:
        return "%.1f kHz" % (d / 1e3)
    return "%d Hz" % d


def ring_stats():
    """(underflows, drops) from the firmware, or (None, None) on older builds.

    Underflows are empty-ring ticks while the host is streaming (host-clock
    vs PWM-clock drift); the last sample is repeated, so they are inaudible.
    Drops are host samples discarded because the ring was full.  Both grow at
    about 48000 * ppm * 1e-6 per second.
    """
    if hasattr(pico_tx, "ring_stats"):
        return pico_tx.ring_stats()
    return None, None


# Command categories for the two-level help.  `help <category>` lists one
# group; `help <command>` still shows the long CMD_DETAILS entry below.
HELP_CATEGORIES = [
    ("carrier", "carrier / deviation / output", [
        ("freq <Hz>", "fine-tune carrier (effective; live in-band, else reboot)"),
        ("dev <Hz>", "full-scale deviation (effective)"),
        ("reinit <car> <dev> [pin]", "save carrier/deviation/pin and reboot"),
        ("band <fm|2m|409|433|446>", "band preset (save + reboot)"),
        ("trim <+/-Hz>", "frequency trim to null the crystal offset"),
        ("pin <21|23|24|25>", "RF output GPIO (save + reboot)"),
        ("pwr <2|4|8|12>", "RF drive strength in mA"),
        ("rf on|off", "RF output on/off"),
        ("silence <auto|park|gate>", "silent-state behaviour"),
    ]),
    ("sound", "USB audio -> FM path (mode fm)", [
        ("audio on|off", "USB audio -> FM routing"),
        ("vol <0-100>", "volume in percent"),
        ("mute on|off", "mute the audio stream"),
        ("pre on|off|50|75|300", "pre-emphasis + band-limit + limiter"),
        ("sq <0-100>", "zero samples below this % of full scale (0 = off)"),
    ]),
    ("modes", "transmit modes and symbol senders", [
        ("mode <fm|tone|fsk|ook|cw|chirp|psk>", "select the transmit scheme"),
        ("tx on|off", "start/stop the modulation engine"),
        ("tone <hz> [hz2] [lvl%]", "internal DDS tone(s) -> FM | tone off"),
        ("fsk <baud> <shift> <hex> [repeat [n]]", "2-FSK symbols"),
        ("ook <baud> <hex> [repeat [n]]", "on/off keying symbols"),
        ("psk <baud> <2|4> <hex> [repeat [n]]", "BPSK/QPSK symbols"),
        ("cw <text> [repeat [n]]", "Morse on the carrier (20 wpm)"),
        ("chirp <f0> <f1> <ms> [gap] [repeat]", "linear sweep | chirp off"),
        ("", "repeat: absent = once, 0 = forever, n = n times"),
    ]),
    ("tuning", "PLL tuning", [
        ("refdiv <1|2|auto>", "PLL reference divider (save + reboot)"),
        ("pdm <1|2|3|4>", "PDM dither rate in MHz (save + reboot)"),
    ]),
    ("debug", "status, meters and diagnostics", [
        ("status / s", "show all transmitter parameters"),
        ("log", "live transmit log (Ctrl-C stops; 'log' resumes)"),
        ("vbar", "live audio level meter"),
        ("ring", "ring buffer fill %"),
        ("diag [s]", "ISR/RX rates + clock drift (default 1s, max 60)"),
        ("pwm", "PWM slice registers + ISR cost"),
        ("pll", "PLL diagnostics"),
        ("sweep [lo hi step]", "sweep the carrier (PLL self-test)"),
    ]),
    ("system", "console and system", [
        ("ver", "version, firmware sha256, project links"),
        ("led <0|1|2|3>", "LED mode: 0 off, 1 always, 2 stream, 3 VU"),
        ("ledpin <0-29> | ws2812 [gpio]", "status LED pin (save + reboot)"),
        ("service [cmd...]", "command to run at boot (headless) | service off"),
        ("reboot", "save current settings and reboot"),
        ("reset", "delete saved config and reboot to defaults"),
        ("cls", "clear the terminal screen"),
        ("exit", "leave the console (back to the REPL)"),
        ("help [category|command]", "this help"),
    ]),
]


def build_help(what=None):
    """Two-level help.  what=None -> category index; a category name -> its
    commands; 'all' -> every category; anything else -> None so the caller
    falls back to the per-command CMD_DETAILS text."""
    if what is None:
        lines = ["pico-tx console - command categories:",
                 "  help              this index",
                 "  help <category>   commands in one category (e.g. help modes)",
                 "  help <command>    detailed help for one command",
                 "  help all          every command",
                 ""]
        for key, title, _cmds in HELP_CATEGORIES:
            lines.append("  %-8s %s" % (key, title))
        return "\n".join(lines)
    w = what.lower()
    if w == "all":
        out = []
        for key, title, cmds in HELP_CATEGORIES:
            out.append("== %s (%s) ==" % (key, title))
            for c, d in cmds:
                out.append("  %-38s %s" % (c, d))
            out.append("")
        return "\n".join(out)
    for key, title, cmds in HELP_CATEGORIES:
        if key == w:
            out = ["%s - %s" % (key, title)]
            for c, d in cmds:
                out.append("  %-38s %s" % (c, d))
            return "\n".join(out)
    return None


# Detailed help for `help <command>`: purpose, arguments, defaults, example.
CMD_DETAILS = {
    "help": """\
help [command] - show this list, or detailed help for one command.
  help               list all commands (short form)
  help <command>     detailed help for that command, e.g. help reinit
  ?                  same as help""",
    "ver": """\
ver - show version, firmware sha256 and project links.
  Useful to check the console matches the flashed firmware (see FW_SHA256).
  Example: ver""",
    "status": """\
status (alias: s) - show all audio/FM parameters.
  Includes: PLL ready/range, RF output, carrier, deviation, audio routing,
  ring fill, volume, mute, pre-emphasis, squelch, clips, RF power, LED.
  Example: status""",
    "freq": """\
freq <Hz> - set the modulation centre (EFFECTIVE frequency: what the radio
  hears).  Live within the current PLL band; outside it the target is saved
  and the board reboots.  UHF targets (above ~150MHz) are carried on the
  3rd/5th harmonic of a lower fundamental and are divided down automatically.
  Default: %d Hz.  Range: %d..%d Hz.
  Example: freq 433920000""" % (DEFAULT_CARRIER, REINIT_CARRIER_MIN, REINIT_CARRIER_MAX),
    "dev": """\
dev <Hz> - set the full-scale deviation (EFFECTIVE: at the radio).
  A full-scale audio sample shifts the carrier by +/- this amount at the
  harmonic the radio hears.  Range: %d..%d Hz (upper bound = half the PLL
  range x harmonic; `status` shows it).  Default: %d Hz.
  Example: dev 12000""" % (REINIT_DEV_MIN, REINIT_DEV_MAX, DEFAULT_DEVIATION),
    "reinit": """\
reinit <carrier_Hz> <deviation_Hz> [rf_pin] - switch band and reboot.
  Saves the new carrier/deviation (and optional RF pin) to %s, then reboots.
  carrier %d..%d Hz: FM 88..108M, 2m 144..148M, UHF 409/433/440M - UHF
  targets are reached on the 3rd (or 5th) harmonic of a <=150MHz fundamental
  and converted automatically; deviation is the EFFECTIVE value at the radio.
  deviation %d..%d Hz, pin 21/23/24/25.
  Example: reinit 145000000 12000 21  (2m, WIDE mode)
  Example: reinit 433920000 12000 21   (433.92M on the 3rd harmonic)"""
  % (CFG_FILE, REINIT_CARRIER_MIN, REINIT_CARRIER_MAX,
     REINIT_DEV_MIN, REINIT_DEV_MAX),
    "pdm": """\
pdm <1|2|3|4> - PDM dither rate in MHz (experimental).
  The fractional PLL dithers the feedback divider at this rate; the PLL loop
  filter averages it, but imperfectly - the residual ripple is what narrowband
  radios hear as noise.  1 MHz = default/library-tested.  Hardware testing
  showed rates above 1MHz sound WORSE (the M0+ systick latency and PLL write
  timing break down), so keep 1 unless you are experimenting.  Saves to %s
  and reboots (a live PLL re-init without reboot deadlocks the board).
  Example: pdm 1""" % CFG_FILE,
    "refdiv": """\
refdiv <1|2|auto> - PLL reference divider.  Default auto.
  REFDIV=2 halves the feedback-divider step (12MHz -> 6MHz per fbdiv LSB)
  and therefore the residual frequency ripple a narrowband receiver hears;
  it also narrows the reachable PLL window.  'auto' picks 2 for every band
  that gates the RF off when silent, and 1 where the unmodulated carrier is
  parked for silence (broadcast FM 87.5-108 MHz), because a parked carrier
  with refdiv 2 has an audible idle tone (hardware-observed).
  Saves to %s and reboots (a live PLL re-init without reboot deadlocks the
  board).  Example: refdiv auto""" % CFG_FILE,
    "pin": """\
pin <21|23|24|25> - change the RF output GPIO, save and reboot.
  RF can ONLY go to 21/23/24/25: the RP2040 clock-output mux (clk_gpout0-3)
  is hardwired to these pins.  Original Pico / Pico W: only GPIO21 is
  broken out (23/24/25 internal; on Pico W taken by WiFi; 25 = LED).
  RP2040-Zero and many clones also expose 23/24.
  Example: pin 21""",
    "pwr": """\
pwr <2|4|8|12> - RF output drive strength in mA (transmit power).
  Default: 12 mA (max).  8/12 mA give a more symmetric square wave (better
  even-order harmonic suppression); odd harmonics are inherent to the
  square wave and unchanged.  Use a real 50-ohm load to see the difference.
  Example: pwr 8""",
    "rf": """\
rf on|off - enable/disable the RF output.
  Turning the output off silences the radio completely (no carrier).
  Example: rf on""",
    "audio": """\
audio on|off - start/stop the USB-audio -> FM routing.
  `audio off` silences the radio (carrier parks on fc) and is reported as
  muted to the host (Windows shows the mute state).  `audio on` resumes.
  Example: audio on""",
    "vol": """\
vol <0-100> - set the volume in percent (mapped to the internal gain).
  0 = muted; 100 = full scale.  Keep the PC volume moderate so the limiter
  is not constantly engaged (watch clips in `vbar`/`status`).
  Example: vol 70""",
    "mute": """\
mute on|off - mute/unmute the audio stream (routing stays on).
  `mute on` silences the radio immediately and is reported as muted to the
  host (Windows shows the mute state).  `audio off` stops the routing
  instead; `vol 0` is another way to get a quiet carrier.
  Example: mute on""",
    "pre": """\
pre on|off|50|75|300 - 15kHz band-limit + pre-emphasis + soft limiter.
  on = 75us (default, US/EU broadcast), 50 = 50us (China/Japan), 300 = 300us
  (handheld-radio / voice standard - pick this for a walkie-talkie), off = no
  processing.  Pick the value matching your radio's de-emphasis to reduce
  high-frequency hiss.  Saved to %s.
  Example: pre 300""" % CFG_FILE,
    "band": """\
band <fm|2m|409|433|446> - one-command band preset (saves and reboots).
  fm   98.0 MHz    FM broadcast (parked-carrier silence, 15kHz audio)
  2m   145.0 MHz   2m amateur (refdiv 2, WIDE mode, RF gated when silent)
  409  409.75 MHz  PRC license-free PMR on the 3rd harmonic (refdiv 2, gate)
  433  433.92 MHz  ISM/ham on the 3rd harmonic (refdiv 2, gate)
  446  446.00625 MHz  PMR446 ch1 on the 3rd harmonic (refdiv 2, gate, 2.5kHz)
  Sets the carrier/deviation, the PLL reference divider, the silence mode and
  the NFM voice-band audio (300Hz HP + 3kHz LP on non-broadcast bands)
  automatically.  Custom bands: use `reinit`.
  Example: band 433""",
    "trim": """\
trim <±Hz> - fine frequency trim, effective at the radio.
  The crystal tolerance (+-20-30ppm) is multiplied by the harmonic on UHF
  bands (x3: +-6-9kHz), so the emission lands a few kHz off the nominal
  channel.  A trim shifts the fundamental to compensate: e.g. if the 433.92M
  preset measures 433.925M on the radio -> `trim -5000` centres it.
  Applies immediately (no reboot) and is saved to config.  `trim 0` disables.
  Example: trim -5000""",
    "silence": """\
silence <auto|park|gate> - what to radiate when there is no audio.
  park - leave the unmodulated carrier on fc (broadcast FM: an FM radio
    outputs silence on a parked carrier).  For a handheld radio an off-tune
    parked carrier is heard as a continuous tone, so:
  gate - key the RF output OFF on silence (no stream / mute / audio off);
    the handheld's squelch closes -> silence.  RF returns as soon as audio
    flows (PTT-style).
  auto (default) - park on FM broadcast bands, gate on 2m/UHF.
  Applies immediately and is saved to %s.
  Example: silence auto""" % CFG_FILE,
    "sq": """\
sq <0-100> - weak-sample mute threshold in % of full scale.
  Samples below the threshold are zeroed (not modulated), so faint
  background noise in the stream does not reach the radio.  0 = off
  (default).  Paused/muted audio is silent anyway (carrier parks on fc).
  Watch the T marker in `vbar` while tuning.
  Example: sq 5""",
    "led": """\
led <0|1|2|3> - LED mode.
  0 = off, 1 = always on, 2 = on while streaming, 3 = audio-level VU
  (default).  With `ledpin ws2812`, mode 3 is a green->yellow->red VU.
  Example: led 2""",
    "ledpin": """\
ledpin <0-29> - set the plain-LED GPIO, save and reboot.
  Default: 25 (Pico onboard LED).
  Example: ledpin 25""",
    "vbar": """\
vbar - single-line audio level meter (~50 fps).
  Shows the peak bar, the sq threshold (T marker) and the clip rate;
  displays "silent" with a slow refresh while the input is quiet.
  Any key (or Ctrl-C) stops it.  Example: vbar""",
    "ring": """\
ring - ring buffer fill in percent.
  Near 0% = producer/consumer balanced; 99% = the consumer stalled
  (check `diag`).  Example: ring""",
    "diag": """\
diag [seconds] - measure ISR/RX rates and ring-buffer health (default 1 s).
  ISR ticks and RX frames should both be ~48000/s while streaming; a low
  ISR rate means the 48kHz sample IRQ is being starved.
  Underflows = empty-ring ticks while the host is streaming (host-vs-PWM
  crystal drift).  The last sample is repeated, so these are inaudible
  drift events, not clicks - the counter is the only trace of them.
  Drops = host samples discarded because the ring was full.
  Both are reported as a clock offset in ppm, computed against the PWM tick
  count (so the estimate does not depend on how long the sleep really took).
  A longer window sharpens it: 'diag 10' is 10x finer than 'diag'.
  Example: diag 10""",
    "pwm": """\
pwm - PWM slice-7 registers + measured ISR cost (diagnostics).
  Expected: TOP=999, DIV=/1.0, clk_sys=48000000, ISR cost ~2-6 us.
  Example: pwm""",
    "cls": """\
cls - clear the terminal screen (ANSI escape).  Example: cls""",
    "sweep": """\
sweep [lo hi step] - pause audio and sweep the carrier (PLL self-test).
  Defaults: the current PLL range, step 500 kHz.  Values are clamped to
  the PLL range.  Watch the signal move on a spectrum analyser / radio.
  Example: sweep 87000000 88500000 500000""",
    "pll": """\
pll - PLL diagnostics: ready state, achievable range, last written freq.
  Example: pll""",
    "reboot": """\
reboot - save the current settings to %s and reboot without resetting
  them (same effect as unplugging/replugging).  Use it when a change only
  takes effect after a replug.  Example: reboot""" % CFG_FILE,
    "reset": """\
reset - delete the saved config (%s) and reboot to defaults
  (87.9 MHz / 75 kHz / GPIO21 / pre 75us / sq off).
  Example: reset""" % CFG_FILE,
    "exit": """\
exit - stop the console and return to the MicroPython REPL.
  Example: exit""",
}


def parse_int(s, what):
    """Parse an integer argument; prints a friendly error on bad input."""
    try:
        return int(s)
    except (ValueError, TypeError):
        print("error: '%s' is not a number (%s)" % (s, what))
        return None


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def vol_pct(vol):
    """Internal gain (0..32767) -> percent, rounded (avoids off-by-one)."""
    return (vol * 100 + 16384) // 32767


def pct_to_vol(pct):
    """Percent (0..100) -> internal gain, rounded (avoids off-by-one)."""
    return (pct * 32767 + 50) // 100


def parse_hex_bytes(s):
    """Parse a hex symbol string into bytes: '0x0f1e' / '0f 1e' / '0f1e'.
    Returns None (and prints) on bad input."""
    t = s.replace("0x", "").replace("0X", "").replace(",", " ").replace(" ", "")
    if t == "":
        return None
    if len(t) % 2:
        t = "0" + t
    try:
        return bytes.fromhex(t)
    except ValueError:
        print("error: '%s' is not hex" % s)
        return None


def morse_ook(text, wpm=20):
    """Encode `text` as an OOK symbol stream (1 = carrier on, 0 = off) at the
    Morse element rate.  Returns (baud, bytes).  Because CW is just keyed
    OOK, it reuses the symbol engine, so `repeat` works for CW too and the
    console is never blocked while sending."""
    baud = max(1, (wpm * 5 + 3) // 6)  # dot = 1/baud s; wpm*5/6 rounded
    syms = []
    for ch in text.upper():
        if ch == " ":
            syms.extend((0, 0, 0, 0))   # char gap 3 + 4 = 7 (word gap)
            continue
        code = MORSE.get(ch)
        if code is None:
            continue
        for e in code:
            if e == ".":
                syms.extend((1, 0))
            else:
                syms.extend((1, 1, 1, 0))
        syms.extend((0, 0))             # 3 total between characters
    syms.extend((0, 0, 0))              # trailing gap
    return baud, bytes(syms)


def hex_str(data, limit=48):
    """Hex preview of a byte payload, truncated with '..'."""
    h = "".join("%02x" % b for b in data)
    return h if len(h) <= limit else h[:limit] + ".."


def parse_repeat(parts):
    """Pop a trailing 'repeat [n]' from a token list.  Returns (n, toks):
    n = 1 (no repeat), 0 (forever), or the given count; toks has the tokens
    with the repeat clause removed."""
    toks = list(parts)
    if toks and toks[-1].lower() == "repeat":
        return 0, toks[:-1]
    if len(toks) >= 2 and toks[-2].lower() == "repeat":
        n = parse_int(toks[-1], "repeat count")
        if n is None or n < 0:
            print("repeat count must be 0 (forever) or a positive number")
            return None, toks
        return n, toks[:-2]
    return 1, toks


def load_cfg():
    try:
        with open(CFG_FILE) as f:
            return json.load(f)
    except OSError:
        return {}


def cfg_int(cfg, key, default):
    """Read an integer config value defensively.  A JSON file edited by hand
    (or written by an older version) may hold a numeric string or a float;
    anything non-numeric falls back to `default` so setup never crashes."""
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def save_cfg(cfg):
    try:
        with open(CFG_FILE, "w") as f:
            json.dump(cfg, f)
    except OSError:
        print("(warning: cannot write %s)" % CFG_FILE)


def save_current(carrier=None, deviation=None, rf_pin=None, pdm_rate=None, refdiv=None):
    """Persist the current transmitter settings (defaults = current values)."""
    save_cfg({
        "carrier": carrier if carrier is not None else pico_tx.carrier(),
        "deviation": deviation if deviation is not None else pico_tx.deviation(),
        "target_freq": TARGET_FREQ,
        "harmonic": HARMONIC,
        "dev_eff": DEV_EFF,
        "trim_hz": TRIM_HZ,
        "rf_pin": rf_pin if rf_pin is not None else RF_PIN,
        "power_ma": pico_tx.power(),
        "led_pin": "ws2812" if LED_WS2812 else pico_tx.led_pin(),
        "ws2812_pin": WS2812_PIN if LED_WS2812 else 16,
        "preemph": PREEMPH,
        "squelch": SQUELCH_PCT,
        "silence_mode": SILENCE_MODE,
        "pdm_rate": pdm_rate if pdm_rate is not None else pico_tx.pdm_rate(),
        "refdiv": refdiv if refdiv is not None else pico_tx.refdiv(),
        "mode": MODE,
        "service": SERVICE,
        "console": CONSOLE_ON,
    })


def apply_runtime_cfg(cfg, start_ws2812=False):
    """Re-apply persisted power/LED/pre-emphasis/squelch/silence-gate after a
    PLL init (tx_modulator_init resets them).  Used at boot."""
    global PREEMPH, SQUELCH_PCT, LED_WS2812, WS2812_PIN, SILENCE_MODE
    power_ma = cfg_int(cfg, "power_ma", 12)
    if power_ma not in (2, 4, 8, 12):
        power_ma = 12
    pico_tx.set_power(power_ma)
    pre = cfg.get("preemph", DEFAULT_PREEMPH)
    if pre not in ("on", "50", "75", "300", "off"):
        pre = DEFAULT_PREEMPH
    PREEMPH = pre
    if pre == "off":
        pico_tx.set_preemphasis(False)
    else:
        pico_tx.set_preemphasis(True)
        pico_tx.set_preemphasis_tc({"50": 50, "300": 300}.get(pre, 75))
    sq = cfg_int(cfg, "squelch", DEFAULT_SQUELCH)
    if not (0 <= sq <= 100):
        sq = DEFAULT_SQUELCH
    SQUELCH_PCT = sq
    # Silence handling: broadcast FM parks the unmodulated carrier on fc;
    # narrowband (handheld-radio) bands key the RF output off on silence so
    # the radio's squelch closes instead of hearing an off-tune parked CW.
    smode = cfg.get("silence_mode", "auto")
    if smode not in ("auto", "park", "gate"):
        smode = "auto"
    SILENCE_MODE = smode
    if smode == "auto":
        pico_tx.set_silence_gate(silence_gate_resolve(TARGET_FREQ))
    else:
        pico_tx.set_silence_gate(smode == "gate")
    # NFM voice-band chain (300Hz HP + 3kHz LP) on the same non-broadcast
    # bands as the gate; broadcast FM keeps the 15kHz audio band.
    pico_tx.set_nfm_audio(silence_gate_resolve(TARGET_FREQ))
    pico_tx.set_squelch(SQUELCH_PCT * 32767 // 100)
    led_pin = cfg.get("led_pin", 25)
    if led_pin == "ws2812":
        LED_WS2812 = True
        wpin = cfg_int(cfg, "ws2812_pin", 16)
        if not (0 <= wpin <= 29):
            wpin = 16
        WS2812_PIN = wpin
        pico_tx.set_led_mode(0)  # plain-LED path off; Python drives the NeoPixel
        if start_ws2812:
            start_ws2812_vu(WS2812_PIN)
    else:
        LED_WS2812 = False
        led_pin = cfg_int(cfg, "led_pin", 25)
        if not (0 <= led_pin <= 29):
            led_pin = 25
        pico_tx.set_led_pin(led_pin)
        pico_tx.set_led_mode(3)  # LED follows the audio level


def band_apply(target, dev_eff, pin):
    """Pick the harmonic for an effective target, save and reboot."""
    global HARMONIC, TARGET_FREQ, DEV_EFF, SILENCE_MODE
    TARGET_FREQ = target
    DEV_EFF = dev_eff
    HARMONIC = 1 if target <= HARMONIC_CEILING else pick_harmonic(target)
    # Band-appropriate PLL reference divider (see refdiv_for()): refdiv 2
    # halves the PDM dither step, but must stay 1 where the unmodulated
    # carrier is parked for silence - a leftover refdiv 2 on FM changes the
    # parked PDM pattern and puts an audible idle tone on the silent carrier
    # (this is what broke the FM pause-silence after a previous UHF session).
    pico_tx.set_refdiv(refdiv_for(target))
    SILENCE_MODE = "auto"   # band-derived: FM parks the carrier, NFM gates it
    save_current(rf_pin=pin)
    if HARMONIC > 1:
        print("saved %.3f MHz / %.1f kHz (fundamental %.3f MHz x%d) - rebooting..."
              % (target / 1e6, dev_eff / 1e3,
                 fundamental_of(target, HARMONIC) / 1e6, HARMONIC))
    else:
        print("saved %.3f MHz / %.1f kHz - rebooting..."
              % (target / 1e6, dev_eff / 1e3))
    time.sleep_ms(100)
    machine.reset()


def start_ws2812_vu(pin):
    """Drive a WS2812 (NeoPixel, e.g. the RP2040-Zero onboard LED on GPIO16)
    as the status LED: follows pico_tx.led_mode() (0=off, 1=white always,
    2=white while streaming, 3=audio-level green->yellow->red VU).  Runs on a
    30 Hz soft timer so it works alongside the console."""
    from machine import Timer
    import neopixel
    np = neopixel.NeoPixel(machine.Pin(pin), 1)
    peak = [0]
    last_mode = [None]

    def tick(t):
        try:
            mode = pico_tx.led_mode()
            if mode != last_mode[0]:
                last_mode[0] = mode
                if mode == 0:
                    peak[0] = 0
            if mode == 0:
                np[0] = (0, 0, 0)
            elif mode == 1:
                np[0] = (255, 255, 255)
            elif mode == 2:
                np[0] = (255, 255, 255) if pico_tx.audio_active() else (0, 0, 0)
            else:  # mode 3: VU meter
                raw = pico_tx.samples(256)
                pk = 0
                for v in array.array("h", raw):
                    a = v if v >= 0 else -v
                    if a > pk:
                        pk = a
                if pk > peak[0]:
                    peak[0] = pk
                else:
                    peak[0] = peak[0] * 15 // 16  # slow decay
                lvl = peak[0] * 255 // 32768
                if lvl > 255:
                    lvl = 255
                # green -> yellow -> red
                if lvl < 128:
                    np[0] = (lvl * 2, 255, 0)
                else:
                    np[0] = (255, 255 - (lvl - 128) * 2, 0)
            np.write()
        except Exception:
            pass  # never let the LED timer kill anything

    return Timer(period=33, mode=Timer.PERIODIC, callback=tick)


def show_status():
    lo, hi = pico_tx.range()
    cf = pico_tx.current_freq()
    print("----- pico-tx status -----")
    print("PLL ready      : %s" % ("yes" if pico_tx.ready() else "NO"))
    print("TX engine      : %s" % ("running" if pico_tx.running() else "stopped"))
    print("Mode           : %s" % pico_tx.mode())
    if pico_tx.mode() == "tone":
        print("Tone           : %s" % ("%d Hz" % pico_tx.tone_hz() if pico_tx.tone_on() else "off"))
    elif pico_tx.mode() in ("fsk", "ook", "cw", "psk"):
        rep = pico_tx.repeat()
        print("Symbols        : %d queued (repeat %s)"
              % (pico_tx.pending(), "forever" if rep == 0 else ("x%d" % rep)))
    if LAST_TX:
        print("Last TX        : %s" % LAST_TX)
    if not pico_tx.output_on():
        rf_txt = "OFF (rf off)"
    elif not pico_tx.running():
        rf_txt = "OFF (engine stopped)"
    elif pico_tx.rf_gated():
        rf_txt = "ON - keyed off now"
    else:
        rf_txt = "ON"
    print("RF output      : %s (GPIO%d)" % (rf_txt, RF_PIN))
    if HARMONIC > 1:
        print("Carrier        : %.3f MHz effective (fundamental %.3f MHz x%d)"
              % (TARGET_FREQ / 1e6, pico_tx.carrier() / 1e6, HARMONIC))
        print("PLL range      : %.3f..%.3f MHz (fundamental)" % (lo / 1e6, hi / 1e6))
    else:
        print("Carrier        : %.3f MHz (PLL range %.3f..%.3f MHz)"
              % (pico_tx.carrier() / 1e6, lo / 1e6, hi / 1e6))
    print("Last ISR freq  : %s" % ("%.3f MHz" % (cf / 1e6) if cf else "(ISR never ran)"))
    if HARMONIC > 1:
        print("Deviation      : %.1f kHz effective (%.1f kHz fundamental)"
              % (DEV_EFF / 1e3, pico_tx.deviation() / 1e3))
    else:
        print("Deviation      : %.1f kHz" % (pico_tx.deviation() / 1e3))
    print("Audio routing  : %s" % ("ON" if AUDIO_ON else "OFF"))
    print("Host streaming : %s" % ("yes" if pico_tx.audio_active() else "no"))
    print("Ring buffer    : %d%% full" % pico_tx.ring_level())
    u, d = ring_stats()
    if u is not None:
        print("Ring drift     : %d underflows, %d drops" % (u, d))
    print("Volume         : %d%%" % vol_pct(pico_tx.volume()))
    print("Mute           : %s" % ("yes" if pico_tx.muted() else "no"))
    pre_txt = {"on": "on (75us)", "50": "on (50us)", "300": "on (300us)", "off": "off"}.get(PREEMPH, "on (75us)")
    print("Pre-emphasis   : %s" % pre_txt)
    sq_txt = "off" if SQUELCH_PCT == 0 else "%d%%" % SQUELCH_PCT
    print("Squelch        : %s" % sq_txt)
    gate_on = pico_tx.silence_gate()
    gated_now = pico_tx.rf_gated()
    if gate_on:
        print("Silence        : gate (RF keyed off when silent%s)"
              % (" - gated now" if gated_now else ""))
    else:
        print("Silence        : park (unmodulated carrier on fc)")
    print("Audio band     : %s" % ("voice (300Hz HP, 3kHz LP)" if pico_tx.nfm_audio()
                                  else "broadcast (15kHz LP)"))
    if TRIM_HZ:
        print("Trim           : %+d Hz effective (%+d Hz fundamental)"
              % (TRIM_HZ, round(TRIM_HZ / HARMONIC)))
    print("Clips          : %d (since boot)" % pico_tx.clips())
    print("RF power       : %d mA" % pico_tx.power())
    print("PDM dither     : %d MHz (PLL refdiv %d, step %s)"
          % (pico_tx.pdm_rate(), pico_tx.refdiv(), pll_step_str()))
    print("LED           : %s, mode %d (0=off 1=always 2=stream 3=VU)"
          % ("WS2812 GPIO%d" % WS2812_PIN if LED_WS2812
             else "GPIO%d" % pico_tx.led_pin(), pico_tx.led_mode()))
    print("---------------------------------")


def _vu_bar(width=10, n=128):
    """Peak level bar from the most recent samples (FM audio path)."""
    pk = 0
    for v in array.array("h", pico_tx.samples(n)):
        a = v if v >= 0 else -v
        if a > pk:
            pk = a
    filled = pk * width // 32768
    return "[" + "#" * filled + "." * (width - filled) + "]"


def show_log():
    """Live transmit log: one status line per second until Ctrl-C.  Run
    `log` again to resume.  Auto-started after cw/fsk/ook/psk/tone/chirp.

    Fields are mode-aware: FM shows audio/drift/VU, keyed modes show the
    symbol queue and repeat, chirp shows its sweep.  t = seconds in the log."""
    t0 = time.ticks_ms()
    prev_isr = pico_tx.diag()[0]
    prev_t = t0
    u0, d0 = ring_stats()
    print("tx log - Ctrl-C to stop")
    if LAST_TX:
        print("payload: %s" % LAST_TX)
    print("    t     mode   carrier         RF   detail")
    try:
        while True:
            time.sleep_ms(1000)
            now = pico_tx.diag()[0]
            t = time.ticks_ms()
            dt = time.ticks_diff(t, prev_t) or 1
            isr = (now - prev_isr) * 1000 // dt
            prev_isr, prev_t = now, t
            el = time.ticks_diff(t, t0) / 1000.0
            mode = pico_tx.mode()
            run = pico_tx.running()
            if HARMONIC > 1:
                car = "%.3fM x%d" % (TARGET_FREQ / 1e6, HARMONIC)
            else:
                car = "%.4f MHz" % (pico_tx.carrier() / 1e6)
            if not run or not pico_tx.output_on():
                rf = "off"
            elif mode in ("ook", "cw"):
                rf = "key"       # keyed on/off, not a continuous carrier
            elif pico_tx.rf_gated():
                rf = "gate"      # FM silence gate holding the RF off
            else:
                rf = "on"
            if not run:
                detail = "engine stopped"
            elif mode == "fm":
                u, d = ring_stats()
                du = (u - u0) if (u is not None and u0 is not None) else 0
                dd = (d - d0) if (d is not None and d0 is not None) else 0
                u0, d0 = u, d
                detail = "dev=%.1fk host=%s ring=%3d%% drift=+%d/-%d clips=%d %s" % (
                    pico_tx.deviation() / 1e3,
                    "yes" if pico_tx.audio_active() else "no",
                    pico_tx.ring_level(), du, dd, pico_tx.clips(), _vu_bar())
            elif mode == "tone":
                detail = "tone=%-6s isr=%d/s" % (
                    ("%dHz" % pico_tx.tone_hz()) if pico_tx.tone_on() else "off", isr)
            elif mode in ("fsk", "ook", "psk", "cw"):
                rep = pico_tx.repeat()
                detail = "sym=%4d repeat=%-7s isr=%d/s" % (
                    pico_tx.pending(),
                    "forever" if rep == 0 else ("x%d" % rep), isr)
            elif mode == "chirp":
                f0, f1, ms = LAST_CHIRP
                detail = "%.3f->%.3f MHz %dms isr=%d/s" % (
                    f0 / 1e6, f1 / 1e6, ms, isr)
            else:
                detail = "isr=%d/s" % isr
            print("%6.1fs %-6s %-15s %-4s %s" % (el, mode, car, rf, detail))
    except KeyboardInterrupt:
        print("(log stopped; type 'log' to resume)")


def make_poller():
    """A select.poll() on stdin, or None if not supported."""
    try:
        p = select.poll()
        p.register(sys.stdin, select.POLLIN)
        # Drain any pending input so the first poll does not fire immediately.
        while p.poll(0):
            try:
                sys.stdin.read(1)
            except OSError:
                pass
        return p
    except Exception:
        return None


def peak_of(raw):
    vals = array.array("h", raw)
    pk = 0
    for v in vals:
        a = v if v >= 0 else -v
        if a > pk:
            pk = a
    return pk


def write_stdout(s):
    """Write to stdout without depending on sys.stdout.flush() (MicroPython's
    TextIOWrapper has none; CDC writes are transmitted immediately anyway)."""
    sys.stdout.write(s)
    try:
        sys.stdout.flush()
    except AttributeError:
        pass


def vbar():
    """Single-line audio level meter, refreshed at ~50 fps with \r redraw.
    Shows the audio peak, the squelch threshold (T marker, when enabled) and
    the clip rate.  While the input is silent the refresh drops to ~2 fps and
    shows "silent" - this avoids constant USB CDC traffic, whose digital
    activity couples noise into the RF path.  Any key (or Ctrl-C) stops it."""
    poller = make_poller()
    peak = 0
    print("vbar - audio level meter (T = squelch threshold), any key or Ctrl-C to stop")
    next_frame = time.ticks_ms()
    last_clips = pico_tx.clips()
    last_sec = next_frame
    clips_rate = 0
    silent = False
    while True:
        if poller is not None and poller.poll(0):
            try:
                sys.stdin.readline()
            except OSError:
                pass
            break
        now = time.ticks_ms()
        if now < next_frame:
            time.sleep_ms(2)
            continue
        # Clip rate, once per second (delta of the firmware clip counter).
        if now - last_sec >= 1000:
            clips_rate = pico_tx.clips() - last_clips
            last_clips += clips_rate
            last_sec = now
        raw = pico_tx.samples(256)
        pk = peak_of(raw)
        if pk > peak:
            peak = pk
        else:
            peak = (peak * 7) // 8  # short trail so the bar follows the beat
        if pk == 0:
            # Silent: throttle to ~2 fps so the USB CDC stream (and the
            # digital noise it couples into the RF) stays minimal.
            if not silent:
                silent = True
                write_stdout("\rsilent (any key or Ctrl-C to stop)   ")
            next_frame = now + 500
            continue
        silent = False
        next_frame = now + 20  # ~50 fps
        nbars = peak * 40 // 32768
        thr = pico_tx.squelch()
        thrpos = thr * 40 // 32768 if thr else -1
        row = []
        for i in range(40):
            if i == thrpos:
                row.append("T")
            elif i < nbars:
                row.append("#")
            else:
                row.append("-")
        write_stdout("\r[%s] %5d clips=%d" % ("".join(row), peak, clips_rate))
    print()


def do_command(line, autolog=True):
    global AUDIO_ON, PREEMPH, SQUELCH_PCT, HARMONIC, TARGET_FREQ, DEV_EFF, SILENCE_MODE, TRIM_HZ
    global MODE, SERVICE, LAST_CHIRP, LAST_TX
    parts = line.split()
    if not parts:
        return True
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else None
    arg2 = parts[2] if len(parts) > 2 else None
    lo, hi, dev_max = pll_limits()

    try:
        if cmd in ("help", "?"):
            if arg is None:
                print(build_help())
            else:
                h = build_help(arg)
                if h is not None:
                    print(h)
                else:
                    d = CMD_DETAILS.get(arg.lower())
                    if d is None:
                        print("no help for '%s' (try 'help' for the category index)" % arg)
                    else:
                        print(d)
        elif cmd == "ver":
            print("pico-tx v%s" % VERSION)
            print("Firmware sha256: %s" % FW_SHA256)
            try:
                mpy = sys.implementation.version
                print("MicroPython: %d.%d.%d" % mpy[:3])
            except Exception:
                pass
            print("Project: %s" % PROJECT_URL)
            print("Bilibili: %s" % BILIBILI_URL)
        elif cmd in ("status", "s"):
            show_status()
        elif cmd == "freq":
            if arg is None:
                print("usage: freq <Hz>  (effective frequency; live within the current")
                print("  PLL band, else saves and reboots)")
            else:
                v = parse_int(arg, "freq")
                if v is not None:
                    if v < REINIT_CARRIER_MIN or v > REINIT_CARRIER_MAX:
                        print("error: carrier out of supported range %d..%d Hz"
                              % (REINIT_CARRIER_MIN, REINIT_CARRIER_MAX))
                    else:
                        h = HARMONIC if HARMONIC > 1 else \
                            (pick_harmonic(v) if v > HARMONIC_CEILING else 1)
                        fund = fundamental_of(v, h) + round(TRIM_HZ / h)
                        if h == HARMONIC and lo <= fund <= hi:
                            pico_tx.set_carrier(fund)
                            TARGET_FREQ = v
                            print("carrier set to %.3f MHz (live)" % (v / 1e6))
                        else:
                            print("target outside the current PLL band "
                                  "(%.3f..%.3f MHz) - saving and rebooting"
                                  % (lo / 1e6, hi / 1e6))
                            band_apply(v, DEV_EFF, RF_PIN)
        elif cmd in ("dev", "deviation"):
            if arg is None:
                print("usage: dev <Hz>  (effective deviation; range %d..%d Hz)"
                      % (REINIT_DEV_MIN, dev_max * HARMONIC))
            else:
                v = parse_int(arg, "dev")
                if v is not None:
                    eff_max = dev_max * HARMONIC
                    if v < REINIT_DEV_MIN or v > eff_max:
                        v = clamp(v, REINIT_DEV_MIN, eff_max)
                        print("(clamped to %d Hz)" % v)
                    DEV_EFF = v
                    pico_tx.set_deviation(dev_fund_of(v, HARMONIC))
                    print("deviation set to %.1f kHz" % (DEV_EFF / 1e3))
        elif cmd == "reinit":
            if arg is None or arg2 is None:
                print("usage: reinit <carrier_Hz> <deviation_Hz> [rf_pin]")
                print("  carrier %d..%d Hz (FM 88..108M, 2m 144..148M, UHF 409/433/440M"
                      % (REINIT_CARRIER_MIN, REINIT_CARRIER_MAX))
                print("  via 3rd/5th harmonic of a <=150MHz fundamental),")
                print("  deviation %d..%d Hz (effective), rf_pin 21/23/24/25"
                      % (REINIT_DEV_MIN, REINIT_DEV_MAX))
            else:
                car = parse_int(arg, "carrier")
                dev = parse_int(arg2, "deviation")
                new_pin = RF_PIN
                if len(parts) > 3:
                    new_pin = parse_int(parts[3], "rf_pin")
                    if new_pin is None or new_pin not in (21, 23, 24, 25):
                        print("error: rf_pin must be 21, 23, 24 or 25")
                        new_pin = None
                if car is not None and dev is not None and new_pin is not None:
                    if not (REINIT_CARRIER_MIN <= car <= REINIT_CARRIER_MAX):
                        print("error: carrier out of range %d..%d Hz"
                              % (REINIT_CARRIER_MIN, REINIT_CARRIER_MAX))
                    elif not (REINIT_DEV_MIN <= dev <= REINIT_DEV_MAX):
                        print("error: deviation out of range %d..%d Hz"
                              % (REINIT_DEV_MIN, REINIT_DEV_MAX))
                    else:
                        h = pick_harmonic(car)
                        fund = fundamental_of(car, h)
                        if h > 1 and 118_000_000 <= fund <= 137_000_000:
                            print("WARNING: fundamental %.3f MHz sits inside the AERONAUTICAL"
                                  % (fund / 1e6))
                            print("         band (118-137MHz)!  Suppress the fundamental with a")
                            print("         band-pass filter before radiating, or use the 5th")
                            print("         harmonic instead.")
                        band_apply(car, dev, new_pin)
        elif cmd == "pdm":
            if arg is None or arg not in ("1", "2", "3", "4"):
                print("usage: pdm <1|2|3|4>  (PDM dither rate in MHz; 1 = default)")
                print("  Note: on hardware, rates >1MHz measured WORSE - keep 1.")
                print("  Saves and reboots to apply.")
            else:
                pico_tx.set_pdm_rate(int(arg))
                save_current()
                print("PDM rate set to %s MHz - rebooting..." % arg)
                time.sleep_ms(100)
                machine.reset()
        elif cmd == "refdiv":
            if arg is None or arg not in ("1", "2", "auto"):
                print("usage: refdiv <1|2|auto>")
                print("  PLL reference divider.  2 halves the PDM dither step")
                print("  (ref/div) on every band, which is what sets the residual")
                print("  frequency ripple a narrowband receiver hears; it also")
                print("  narrows the reachable PLL window.  'auto' (default) picks")
                print("  2 unless the band parks the unmodulated carrier for")
                print("  silence (broadcast FM), where refdiv 2 puts an audible")
                print("  idle tone on the silent carrier.")
                print("  Saves and reboots to apply.")
            else:
                v = refdiv_for(TARGET_FREQ) if arg == "auto" else int(arg)
                pico_tx.set_refdiv(v)
                save_current()
                print("REFDIV set to %d (%s) - rebooting..." % (v, arg))
                time.sleep_ms(100)
                machine.reset()
        elif cmd == "band":
            BANDS = {
                "fm":  (98_000_000, 75_000),     # FM broadcast (centre)
                "2m":  (145_000_000, 12_000),    # 2m amateur, WIDE mode
                "409": (409_750_000, 12_500),    # PRC license-free PMR, 3rd harm.
                "433": (433_920_000, 12_500),    # 433.92 ISM/ham, 3rd harmonic
                "446": (446_006_250, 2_500),     # PMR446 ch1 (EU), 3rd harmonic
            }
            if arg is None or arg.lower() not in BANDS:
                print("usage: band <fm|2m|409|433|446>")
                for k, (f, d) in BANDS.items():
                    print("  %-4s -> %.4f MHz / %.1f kHz%s" % (
                        k, f / 1e6, d / 1e3,
                        " (3rd harmonic)" if f > HARMONIC_CEILING else ""))
            else:
                target, dev = BANDS[arg.lower()]
                # The UHF harmonic recipe and every gated (non-parked) band use
                # refdiv 2: half the PDM step.  See refdiv_for().
                pico_tx.set_refdiv(refdiv_for(target))
                h = pick_harmonic(target)
                fund = fundamental_of(target, h)
                if h > 1 and 118_000_000 <= fund <= 137_000_000:
                    print("WARNING: fundamental %.3f MHz sits inside the AERONAUTICAL"
                          % (fund / 1e6))
                    print("         band (118-137MHz)!  Add a band-pass filter to")
                    print("         suppress the fundamental before radiating.")
                print("band %s -> " % arg.lower(), end="")
                band_apply(target, dev, RF_PIN)
        elif cmd == "silence":
            if arg is None or arg not in ("auto", "park", "gate"):
                print("usage: silence <auto|park|gate>")
                print("  auto (default) - FM broadcast parks the carrier on fc;")
                print("    narrowband bands (2m/UHF) gate the RF off on silence")
                print("    (handheld squelch closes -> no off-tune CW tone).")
                print("  park - always leave the unmodulated carrier on fc.")
                print("  gate - always key the RF off when there is no audio.")
                print("  Applies immediately; saved to config.")
            else:
                SILENCE_MODE = arg
                gate = silence_gate_resolve(TARGET_FREQ) if arg == "auto" else (arg == "gate")
                pico_tx.set_silence_gate(gate)
                save_current()
                print("silence mode: %s (RF %s when silent)"
                      % (arg, "off" if gate else "parked on fc"))
        elif cmd == "trim":
            if arg is None:
                print("usage: trim <±Hz>  (fine frequency trim, effective at the radio; 0 = off)")
                print("  Compensates the crystal tolerance, which the harmonic")
                print("  multiplies (x3 on UHF: e.g. trim -5000 centres a 433.925M")
                print("  emission on 433.920M).  Applies immediately; saved.")
            else:
                v = parse_int(arg, "trim")
                if v is not None:
                    old = TRIM_HZ
                    TRIM_HZ = v
                    fund = fundamental_of(TARGET_FREQ, HARMONIC) + round(TRIM_HZ / HARMONIC)
                    if lo <= fund <= hi:
                        pico_tx.set_carrier(fund)
                        save_current()
                        print("trim %+d Hz effective (%+d Hz fundamental), carrier live"
                              % (TRIM_HZ, round(TRIM_HZ / HARMONIC)))
                    else:
                        TRIM_HZ = old
                        print("error: trimmed carrier outside the PLL band - trim unchanged")
        elif cmd == "pin":
            if arg is None or arg not in ("21", "23", "24", "25"):
                print("usage: pin <21|23|24|25>  (RF output GPIO; saves and reboots)")
            else:
                new_pin = int(arg)
                if new_pin != RF_PIN:
                    save_current(rf_pin=new_pin)
                    print("RF pin changed to GPIO%d - rebooting..." % new_pin)
                    time.sleep_ms(100)
                    machine.reset()
                else:
                    print("RF pin is already GPIO%d" % RF_PIN)
        elif cmd == "reset":
            print("resetting to defaults - deleting %s and rebooting..." % CFG_FILE)
            try:
                os.remove(CFG_FILE)
            except OSError:
                pass
            time.sleep_ms(100)
            machine.reset()
        elif cmd == "reboot":
            # Keep the current configuration: persist it first, then reboot
            # (equivalent to unplugging/replugging, without losing settings).
            save_current()
            print("rebooting - settings saved to %s..." % CFG_FILE)
            time.sleep_ms(100)
            machine.reset()
        elif cmd == "rf":
            if arg not in ("on", "off"):
                print("usage: rf on|off")
            else:
                pico_tx.enable_output(arg == "on")
                print("RF output %s" % ("on" if pico_tx.output_on() else "off"))
        elif cmd == "audio":
            if arg not in ("on", "off"):
                print("usage: audio on|off")
            else:
                pico_tx.audio(arg == "on")
                AUDIO_ON = arg == "on"
                print("audio routing %s" % arg)
        elif cmd == "mode":
            if arg is None:
                print("mode: %s" % pico_tx.mode())
                print("usage: mode <fm|tone|fsk|ook|cw|chirp|psk>")
            elif arg in ("fm", "tone", "fsk", "ook", "cw", "chirp", "psk"):
                MODE = arg
                LAST_TX = ""              # payload belongs to the previous mode
                LAST_CHIRP = (0, 0, 0)
                pico_tx.clear_symbols()   # break any running repeat
                pico_tx.set_mode(arg)
                if arg == "fm":
                    pico_tx.audio(AUDIO_ON)
                else:
                    pico_tx.tone_stop()
                    pico_tx.start()
                print("mode -> %s" % arg)
            else:
                print("usage: mode <fm|tone|fsk|ook|cw|chirp|psk>")
        elif cmd == "tx":
            if arg not in ("on", "off"):
                print("usage: tx on|off  (start/stop the modulation engine)")
            else:
                if arg == "on":
                    pico_tx.start()
                else:
                    pico_tx.stop()
                    pico_tx.clear_symbols()   # break any running repeat
                    LAST_TX = ""
                    LAST_CHIRP = (0, 0, 0)
                print("tx %s" % arg)
        elif cmd == "tone":
            if arg is None or arg == "off":
                pico_tx.tone_stop()
                print("tone off")
            else:
                hz = parse_int(arg, "tone Hz")
                hz2 = parse_int(arg2, "tone2 Hz") if arg2 is not None else 0
                lvl = parse_int(parts[3], "tone level %") if len(parts) > 3 else 100
                if None not in (hz, hz2, lvl) and 0 <= hz <= 20000 \
                        and 0 <= hz2 <= 20000 and 0 <= lvl <= 100:
                    MODE = "tone"
                    LAST_TX = "tone %d Hz%s @ %d%%" % (
                        hz, (" + %d Hz" % hz2) if hz2 else "", lvl)
                    pico_tx.tone(hz, hz2 or 0, lvl)
                    pico_tx.start()
                    print("tone %d Hz%s at %d%% (FM deviation)"
                          % (hz, (" + %d Hz" % hz2) if hz2 else "", lvl))
                    if autolog:
                        show_log()
                else:
                    print("usage: tone <hz> [hz2] [level%% 0-100]  |  tone off")
        elif cmd in ("fsk", "ook", "psk"):
            rep, toks = parse_repeat(parts[1:])
            need = 2 if cmd == "ook" else 3
            data = None
            baud = None
            if rep is None or len(toks) < need:
                print("usage: %s <baud> %s <hex-symbols> [repeat [n]]"
                      % (cmd, "<shift_hz>" if cmd == "fsk" else ("<order 2|4>" if cmd == "psk" else "")))
                print("  e.g. fsk 1200 4500 55aa0f  |  ook 2000 aaaa repeat 0  |  psk 2400 2 abcd repeat 5")
            else:
                baud = parse_int(toks[0], "baud")
                desc = cmd
                if baud is not None and baud > 0:
                    if cmd == "fsk":
                        shift = parse_int(toks[1], "shift Hz")
                        data = parse_hex_bytes(toks[2]) if shift is not None else None
                        if data is not None:
                            MODE = "fsk"
                            desc = "fsk %d baud +/-%d Hz" % (baud, shift)
                            pico_tx.fsk_config(baud, shift)
                    elif cmd == "psk":
                        order = parse_int(toks[1], "order 2|4")
                        data = parse_hex_bytes(toks[2]) if order in (2, 4) else None
                        if data is not None:
                            MODE = "psk"
                            desc = "psk %d baud %s" % (baud, "BPSK" if order == 2 else "QPSK")
                            pico_tx.psk_config(baud, order)
                    else:  # ook
                        data = parse_hex_bytes(toks[1])
                        if data is not None:
                            MODE = "ook"
                            desc = "ook %d baud" % baud
                            pico_tx.ook_config(baud)
                if data is None:
                    print("usage: %s <baud> ... <hex-symbols> [repeat [n]]" % cmd)
                else:
                    LAST_TX = "%s, %d sym: %s" % (desc, len(data), hex_str(data))
                    pico_tx.clear_symbols()
                    pico_tx.set_repeat(rep)
                    pico_tx.start()
                    n = pico_tx.send(data)
                    print("%s %d baud: queued %d/%d symbols%s"
                          % (cmd.upper(), baud, n, len(data),
                             " - repeat forever" if rep == 0
                             else (" - x%d" % rep if rep > 1 else "")))
                    if autolog:
                        show_log()
        elif cmd == "cw":
            rep, toks = parse_repeat(parts[1:])
            text = " ".join(toks)
            if rep is None or not text:
                print("usage: cw <text> [repeat [n]]  (Morse on the carrier, 20 wpm)")
            else:
                baud, data = morse_ook(text)
                MODE = "cw"
                LAST_TX = "cw 20wpm: %s" % text
                pico_tx.set_mode("cw")
                pico_tx.set_symbol_rate(baud)
                pico_tx.clear_symbols()
                pico_tx.set_repeat(rep)
                pico_tx.start()
                n = pico_tx.send(data)
                print("cw 20 wpm (baud %d): queued %d/%d elements%s"
                      % (baud, n, len(data),
                         " - repeat forever" if rep == 0
                         else (" - x%d" % rep if rep > 1 else "")))
                if autolog:
                    show_log()
        elif cmd == "chirp":
            if arg is None or arg == "off":
                pico_tx.chirp_stop()
                print("chirp stopped")
            else:
                f0 = parse_int(arg, "f0")
                f1 = parse_int(arg2, "f1") if arg2 is not None else None
                ms = parse_int(parts[3], "duration ms") if len(parts) > 3 else None
                gap = parse_int(parts[4], "gap ms") if len(parts) > 4 else 0
                rep = len(parts) > 5 and parts[5].lower() in ("1", "on", "yes", "repeat")
                if None not in (f0, f1, ms) and ms > 0:
                    MODE = "chirp"
                    LAST_CHIRP = (f0, f1, ms)
                    LAST_TX = "chirp %.4f->%.4f MHz %dms%s" % (
                        f0 / 1e6, f1 / 1e6, ms, " repeat" if rep else "")
                    pico_tx.chirp(f0, f1, ms, gap, rep)
                    pico_tx.start()
                    print("chirp %d -> %d Hz over %d ms%s"
                          % (f0, f1, ms, " (repeating)" if rep else ""))
                    if autolog:
                        show_log()
                else:
                    print("usage: chirp <f0> <f1> <ms> [gap_ms] [repeat]  |  chirp off")
        elif cmd == "service":
            cmdline = line.split(None, 1)
            if arg is None:
                print("service: %s" % (SERVICE if SERVICE else "(none)"))
                print("usage: service <console command...>  |  service off")
            elif arg == "off":
                SERVICE = ""
                save_current()
                print("boot service cleared")
            else:
                SERVICE = cmdline[1]
                save_current()
                print("boot service set: %s" % SERVICE)
        elif cmd == "log":
            show_log()
        elif cmd == "vol":
            if arg is None:
                print("usage: vol <0-100>, e.g. vol 70")
            else:
                v = parse_int(arg, "volume (percent)")
                if v is not None:
                    if v < 0 or v > 100:
                        v = clamp(v, 0, 100)
                        print("(clamped to %d%%)" % v)
                    pico_tx.set_volume(pct_to_vol(v))
                    print("volume set to %d%%" % vol_pct(pico_tx.volume()))
        elif cmd == "mute":
            if arg not in ("on", "off"):
                print("usage: mute on|off")
            else:
                pico_tx.set_mute(arg == "on")
                print("mute %s" % arg)
        elif cmd == "pre":
            if arg in ("on", "off", "50", "75", "300"):
                if arg in ("on", "75"):
                    PREEMPH = "on"
                    pico_tx.set_preemphasis(True)
                    pico_tx.set_preemphasis_tc(75)
                    print("pre-emphasis on (75us)")
                elif arg == "50":
                    PREEMPH = "50"
                    pico_tx.set_preemphasis(True)
                    pico_tx.set_preemphasis_tc(50)
                    print("pre-emphasis on (50us)")
                elif arg == "300":
                    PREEMPH = "300"
                    pico_tx.set_preemphasis(True)
                    pico_tx.set_preemphasis_tc(300)
                    print("pre-emphasis on (300us - handheld radio)")
                else:
                    PREEMPH = "off"
                    pico_tx.set_preemphasis(False)
                    print("pre-emphasis off")
                save_current()
            else:
                print("usage: pre on|off|50|75|300  (on = 75us; 300 = handheld radio)")
        elif cmd == "sq":
            if arg is None:
                print("usage: sq <0-100>  (0 = off, default)")
            else:
                v = parse_int(arg, "squelch (%)")
                if v is not None:
                    if v < 0 or v > 100:
                        print("error: squelch must be 0..100")
                    else:
                        SQUELCH_PCT = v
                        pico_tx.set_squelch(v * 32767 // 100)
                        print("squelch %s" % ("off" if v == 0 else "%d%%" % v))
                        save_current()
        elif cmd == "led":
            if arg is None or arg not in ("0", "1", "2", "3"):
                print("usage: led <0|1|2|3>  (0=off 1=always-on 2=stream 3=VU)")
            else:
                pico_tx.set_led_mode(int(arg))
                print("LED mode %s" % arg)
        elif cmd == "ledpin":
            if arg is None:
                print("usage:")
                print("  ledpin <0-29>            plain LED on a GPIO (saves, reboots)")
                print("  ledpin ws2812 [gpio]     WS2812 NeoPixel (default GPIO16,"
                      " e.g. RP2040-Zero onboard LED); led 0-3 control it")
            elif arg == "ws2812":
                wpin = 16
                if len(parts) > 2:
                    wpin = parse_int(parts[2], "ws2812 gpio")
                    if wpin is None or not (0 <= wpin <= 29):
                        print("error: ws2812 gpio must be 0..29")
                        wpin = None
                if wpin is not None:
                    save_cfg({"carrier": pico_tx.carrier(),
                              "deviation": pico_tx.deviation(),
                              "rf_pin": RF_PIN,
                              "power_ma": pico_tx.power(),
                              "led_pin": "ws2812",
                              "ws2812_pin": wpin,
                              "preemph": PREEMPH,
                              "squelch": SQUELCH_PCT})
                    print("LED set to WS2812 on GPIO%d - rebooting..." % wpin)
                    time.sleep_ms(100)
                    machine.reset()
            else:
                v = parse_int(arg, "ledpin")
                if v is not None:
                    if v < 0 or v > 29:
                        print("error: ledpin must be 0..29 or 'ws2812'")
                    else:
                        save_cfg({"carrier": pico_tx.carrier(),
                                  "deviation": pico_tx.deviation(),
                                  "rf_pin": RF_PIN,
                                  "power_ma": pico_tx.power(),
                                  "led_pin": v,
                                  "preemph": PREEMPH,
                                  "squelch": SQUELCH_PCT})
                        print("LED pin changed to GPIO%d - rebooting..." % v)
                        time.sleep_ms(100)
                        machine.reset()
        elif cmd in ("pwr", "power"):
            if arg is None or arg not in ("2", "4", "8", "12"):
                print("usage: pwr <2|4|8|12>  (RF output drive strength in mA; "
                      "higher = stronger signal, more harmonics)")
            else:
                pico_tx.set_power(int(arg))
                print("RF power set to %d mA" % pico_tx.power())
                save_current()
        elif cmd == "cls":
            # ANSI clear-screen + home; works on most serial terminals.
            write_stdout("\x1b[2J\x1b[H")
        elif cmd == "vbar":
            vbar()
        elif cmd == "ring":
            print("ring buffer %d%% full" % pico_tx.ring_level())
        elif cmd == "pwm":
            top, div, csr, clk = pico_tx.pwm_info()
            cost = pico_tx.isr_cost()
            i0 = pico_tx.diag()[0]
            time.sleep_ms(1000)
            i1 = pico_tx.diag()[0]
            print("clk_sys     : %d Hz" % clk)
            print("PWM slice7  : TOP=%d DIV=0x%04X CSR=0x%02X"
                  "  (INT=1 FRAC=0 -> /1.0, wrap 48kHz)" % (top, div, csr))
            print("ISR cost    : ~%d us/call (expect ~2-6)" % cost)
            print("ISR rate    : %d/s over 1s (expect ~48000)" % (i1 - i0))
        elif cmd == "diag":
            # Optional window length: a longer window divides the 1-sample
            # quantisation of the counters, so the ppm estimate gets sharper
            # (1 s -> ~21 ppm steps, 10 s -> ~2 ppm).
            secs = 1
            if arg is not None:
                v = parse_int(arg, "seconds")
                if v is None:
                    return
                secs = max(1, min(60, v))
            i0, r0 = pico_tx.diag()
            u0, d0 = ring_stats()
            time.sleep_ms(secs * 1000)
            i1, r1 = pico_tx.diag()
            u1, d1 = ring_stats()
            di, dr = i1 - i0, r1 - r0
            print("window    : %d s, PWM ticks +%d (~%.0f/s over this window)"
                  % (secs, di, di / secs))
            print("ISR ticks : %d -> %d   (+%d, expect ~%d)"
                  % (i0, i1, di, 48000 * secs))
            print("RX frames : %d -> %d   (+%d, expect ~%d while streaming)"
                  % (r0, r1, dr, 48000 * secs))
            if u1 is None:
                print("ring drift: (firmware has no ring_stats; update the firmware)")
            else:
                du, dd = u1 - u0, d1 - d0
                print("Underflows: %d -> %d   (+%d, host clock slower than PWM)"
                      % (u0, u1, du))
                print("Drops     : %d -> %d   (+%d, host clock faster than PWM)"
                      % (d0, d1, dd))
                if du == 0 and dd == 0:
                    print("            no drift event in this window")
                else:
                    # ppm relative to the PWM clock, using the ISR tick count as
                    # the time base: independent of how long the sleep really
                    # took (sleep_ms(1000) overshoots by ~0.6% with the console
                    # overhead, which would bias a /48000-based estimate).
                    print("            ~%.1f ppm clock offset (%.1f events/s)"
                          % ((du + dd) * 1e6 / max(di, 1), (du + dd) / secs))
            print("ring %d%% full, last ISR freq %.3f MHz"
                  % (pico_tx.ring_level(), pico_tx.current_freq() / 1e6))
        elif cmd == "sweep":
            vlo = parse_int(arg if arg is not None else str(lo), "sweep lo")
            vhi = parse_int(arg2 if arg2 is not None else str(hi), "sweep hi")
            vstep = parse_int(parts[3] if len(parts) > 3 else "500000", "sweep step")
            if None not in (vlo, vhi, vstep):
                vlo = clamp(vlo, lo, hi)
                vhi = clamp(vhi, lo, hi)
                if vstep <= 0:
                    print("error: step must be positive")
                elif vlo >= vhi:
                    print("error: lo must be < hi (got %d >= %d)" % (vlo, vhi))
                else:
                    print("sweeping %.3f..%.3f MHz step %.0f kHz (audio paused%s)"
                          % (vlo / 1e6, vhi / 1e6, vstep / 1e3,
                             "; radio hears x%d" % HARMONIC if HARMONIC > 1 else ""))
                    pico_tx.audio(False)
                    f = vlo
                    while f <= vhi:
                        pico_tx.set_freq(f)
                        time.sleep_ms(80)
                        f += vstep
                    pico_tx.audio(AUDIO_ON)
                    print("sweep done")
        elif cmd == "pll":
            cf = pico_tx.current_freq()
            print("ready: %s, range %.3f..%.3f MHz, last ISR freq: %s"
                  % ("yes" if pico_tx.ready() else "no", lo / 1e6, hi / 1e6,
                     "%.3f MHz" % (cf / 1e6) if cf else "(ISR never ran)"))
        elif cmd == "exit":
            print("bye")
            return False
        else:
            print("unknown command: %s  (type 'help')" % cmd)
    except Exception as e:
        # Never let a command error kill the console.
        print("error: %s" % e)
    return True


def wait_terminal():
    """Wait until a terminal/serial program actually has the CDC port open
    (DTR asserted), so the banner is printed to a live console."""
    if pico_tx.cdc_connected():
        return
    print("waiting for a terminal connection...", end="")
    while not pico_tx.cdc_connected():
        time.sleep_ms(250)
    print(" connected.")


def do_setup():
    """Transmitter setup: init, RF on, then wait for a terminal and print
    the banner.  Runs inside the retry loop so an interrupt during setup
    restarts it instead of dropping to the plain REPL."""
    global RF_PIN, LED_WS2812, WS2812_PIN, PREEMPH, SQUELCH_PCT
    global HARMONIC, TARGET_FREQ, DEV_EFF, TRIM_HZ
    global MODE, SERVICE, CONSOLE_ON
    cfg = load_cfg()
    rf_pin = cfg_int(cfg, "rf_pin", RF_PIN)
    if rf_pin not in (21, 23, 24, 25):
        rf_pin = 21
    RF_PIN = rf_pin
    # Effective target frequency + harmonic: a UHF target (above the ~150MHz
    # fundamental ceiling) is carried on the Nth odd harmonic of a lower
    # fundamental.  Legacy configs without target_freq/harmonic behave exactly
    # as before (harmonic 1).
    harmonic = cfg_int(cfg, "harmonic", 1)
    if harmonic not in (1, 3, 5, 7):
        harmonic = 1
    target_freq = cfg_int(cfg, "target_freq", 0)
    TRIM_HZ = cfg_int(cfg, "trim_hz", 0)
    if target_freq:
        HARMONIC = harmonic
        TARGET_FREQ = target_freq
        carrier = fundamental_of(target_freq, harmonic) + round(TRIM_HZ / harmonic)
        dev_eff = cfg_int(cfg, "dev_eff", 0)
        if dev_eff:
            DEV_EFF = dev_eff
            deviation = dev_fund_of(dev_eff, harmonic)
        else:
            deviation = cfg_int(cfg, "deviation", DEFAULT_DEVIATION)
            DEV_EFF = deviation * harmonic
    else:
        HARMONIC = 1
        carrier = cfg_int(cfg, "carrier", DEFAULT_CARRIER) + round(TRIM_HZ / harmonic)
        TARGET_FREQ = carrier - round(TRIM_HZ / harmonic)
        deviation = cfg_int(cfg, "deviation", DEFAULT_DEVIATION)
        DEV_EFF = deviation

    # Experimental narrowband-noise controls, applied BEFORE init() (both are
    # read once at PLL/core1 launch): REFDIV=2 halves the PDM step, and a
    # faster PDM dither is averaged more strongly by the PLL loop filter -
    # both lower the residual ripple heard by narrowband (12.5/25kHz) radios.
    refdiv = cfg_int(cfg, "refdiv", 0)
    if refdiv not in (1, 2):
        refdiv = refdiv_for(TARGET_FREQ)   # unset / invalid -> auto
    pico_tx.set_refdiv(refdiv)
    pdm_rate = cfg_int(cfg, "pdm_rate", 1)
    if not (1 <= pdm_rate <= 4):
        pdm_rate = 1
    pico_tx.set_pdm_rate(pdm_rate)

    ok = False
    try:
        pico_tx.init(carrier, deviation, RF_PIN)
        ok = True
    except ValueError as exc:
        # refdiv 2 halves the PDM step but narrows the reachable PLL window;
        # a window that only fits with refdiv 1 is still perfectly valid.  A
        # failed init() never launches core1, so retrying is safe (unlike a
        # deinit/init on a running PLL).
        if refdiv == 2:
            print("NOTE: %s" % exc)
            print("Retrying with refdiv 1 (wider PLL window, larger PDM step)")
            refdiv = 1
            pico_tx.set_refdiv(1)
            try:
                pico_tx.init(carrier, deviation, RF_PIN)
                ok = True
                save_current(refdiv=1)   # do not repeat the probe every boot
            except ValueError as exc2:
                exc = exc2
        if not ok:
            print("ERROR: init with saved config failed: %s" % exc)
            print("Falling back to defaults %.3f MHz / %.1f kHz"
                  % (DEFAULT_CARRIER / 1e6, DEFAULT_DEVIATION / 1e3))
            try:
                os.remove(CFG_FILE)
            except OSError:
                pass
            HARMONIC = 1
            TARGET_FREQ = DEFAULT_CARRIER
            DEV_EFF = DEFAULT_DEVIATION
            pico_tx.set_refdiv(refdiv_for(DEFAULT_CARRIER))
            try:
                pico_tx.init(DEFAULT_CARRIER, DEFAULT_DEVIATION, RF_PIN)
                ok = True
            except ValueError as exc2:
                print("ERROR: default config also failed: %s" % exc2)
    if ok:
        pico_tx.enable_output(True)
        MODE = cfg.get("mode", "fm")
        if MODE not in ("fm", "tone", "fsk", "ook", "cw", "chirp", "psk"):
            MODE = "fm"
        if MODE == "fm":
            pico_tx.audio(AUDIO_ON)
        else:
            pico_tx.set_mode(MODE)
            pico_tx.start()
        apply_runtime_cfg(cfg, start_ws2812=True)
    # Boot service (headless transmit): run one console command automatically.
    SERVICE = cfg.get("service", "")
    if not isinstance(SERVICE, str):
        SERVICE = ""
    CONSOLE_ON = bool(cfg.get("console", True))
    if SERVICE:
        print("boot service: %s" % SERVICE)
        try:
            do_command(SERVICE, autolog=False)
        except Exception as e:
            print("(service error: %s)" % e)
    if not CONSOLE_ON:
        print("headless mode (console off; Ctrl-C returns to the REPL)")
        return
    # Only now wait for a terminal, then print the banner to it.
    wait_terminal()
    print("==============================================")
    print(" pico-tx console v%s" % VERSION)
    if HARMONIC > 1:
        print(" carrier %.3f MHz (fundamental %.3f MHz x%d), deviation %.1f kHz,"
              " GPIO%d"
              % (TARGET_FREQ / 1e6, carrier / 1e6, HARMONIC,
                 DEV_EFF / 1e3, RF_PIN))
    else:
        print(" carrier %.3f MHz, deviation %.1f kHz, GPIO%d"
              % (carrier / 1e6, deviation / 1e3, RF_PIN))
    print("==============================================")
    if ok:
        print("Select 'pico-tx' as audio output.")
        show_status()
    print("Type 'help' for the full command list.")


def console_loop():
    """The interactive prompt.  Returns only when the user types 'exit'."""
    while True:
        try:
            line = input("tx> ")
        except KeyboardInterrupt:
            # A Ctrl-C at the prompt (e.g. Thonny connect): just re-prompt.
            print("\n(interrupted - type 'exit' to return to the REPL)")
            time.sleep_ms(100)
            continue
        except EOFError:
            continue
        if not do_command(line):
            return


def main():
    # Setup retries on any interrupt/error, but capped so a persistently
    # broken config cannot spin forever (worst case: console runs without RF,
    # and the device always stays flashable via BOOTSEL).
    attempts = 0
    while True:
        try:
            do_setup()
            break
        except KeyboardInterrupt:
            print("\n(setup interrupted - retrying)")
            time.sleep_ms(300)
        except Exception as e:
            attempts += 1
            print("\n(setup error: %s - retrying)" % e)
            time.sleep_ms(300)
            if attempts >= 10:
                print("setup still failing after 10 tries - continuing to console")
                print("(if this repeats, /tx_cfg.json may be corrupt: type 'reset' to restore defaults)")
                break
    if not CONSOLE_ON:
        # Headless service: keep the device alive running the boot service.
        # Ctrl-C drops back to the MicroPython REPL for recovery.
        while True:
            time.sleep_ms(1000)
    while True:
        try:
            console_loop()
            return
        except KeyboardInterrupt:
            print("\n(console resumed)")
            continue
        except Exception as e:
            print("\n(console error: %s - resumed)" % e)
            continue


main()
