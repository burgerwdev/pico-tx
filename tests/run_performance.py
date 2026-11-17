#!/usr/bin/env python3
"""Exercise the actual patched C hot paths on the host (no Pico required).

python3 tests/run_performance.py
Requires a C compiler; enables undefined-behaviour sanitization. Hardware
registers/FIFO are stubbed, so this verifies data/timing semantics, not CPU load.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def extract_sources(dest):
    patch = (ROOT / "patches/micropython-tx.patch").read_text()
    for section in patch.split("diff --git ")[1:]:
        path = section.splitlines()[0].split(" b/", 1)[1]
        if path.startswith("ports/rp2/tx/"):
            assert "--- /dev/null" in section, path
            content = "\n".join(line[1:] for line in section.splitlines()
                                if line.startswith("+") and not line.startswith("+++")) + "\n"
            (dest / Path(path).name).write_text(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", type=Path, help="also inspect the compiled ARM firmware")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pico-tx-test-") as tmp:
        dest = Path(tmp)
        extract_sources(dest)
        # Test the real modulator, excluding the unrelated UAC control-request
        # handlers. Only hardware includes are replaced; DSP/ISR/RX are intact.
        src = (dest / "tx_modulator.c").read_text()
        src = src.split("// UAC1 feature-unit control selectors.")[0]
        src = re.sub(r'^#include "(?!tx_)[^"]+"\n', "", src, flags=re.M)
        src = '#include "platform.h"\n' + src + "\n#endif\n#endif\n"
        (dest / "modulator_under_test.h").write_text(src)
        subprocess.run([os.environ.get("CC", "cc"), "-std=c11", "-O2", "-g",
                        "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                        "-fsanitize=undefined", "-fno-sanitize-recover=all",
                        "-I", str(dest), "-I", str(ROOT / "tests"),
                        str(ROOT / "tests/test_modulator.c"), "-o", str(dest / "check")], check=True)
        subprocess.run([str(dest / "check")], check=True)
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tests"),
                    "-p", "test_console.py", "-q"], check=True)
    if args.elf:
        dis = subprocess.check_output(["arm-none-eabi-objdump", "-d", str(args.elf)], text=True)
        blocks = {name: (int(addr, 16), body) for addr, name, body in re.findall(
            r'^([0-9a-f]+) <([^>]+)>:\n(.*?)(?=\n[0-9a-f]+ <|\Z)', dis, re.M | re.S)}
        for name in ("tx_pwm_wrap_handler", "tud_audio_rx_done_isr",
                     "pico_fractional_pll_set_freq_u32", "pico_fractional_pll_enable_output"):
            addr, body = blocks[name]
            assert 0x20000000 <= addr < 0x20040000, (name, hex(addr), "must run from SRAM")
        irq = blocks["tx_pwm_wrap_handler"][1]
        assert not re.search(r'aeabi_\w*(?:div|lmul)', irq), "wide arithmetic in sample ISR"
        gate = blocks["pico_fractional_pll_enable_output"][1]
        assert not re.search(r'\bbl\s', gate), "RF keying must not call clock/pad setup"
        print("ARM firmware: hot entries in SRAM, no sample-ISR wide arithmetic, constant-time RF keying: OK")


if __name__ == "__main__":
    main()
