import array
import ast
import io
from pathlib import Path
import types
import unittest
from unittest.mock import Mock


class Clock:
    def __init__(self, now=0):
        self.now = now
        self.sleeps = []

    def ticks_ms(self):
        return self.now % (1 << 30)

    def ticks_add(self, tick, delta):
        return (tick + delta) % (1 << 30)

    def ticks_diff(self, a, b):
        return (a - b + (1 << 29)) % (1 << 30) - (1 << 29)

    def sleep_ms(self, ms):
        self.sleeps.append(ms)
        self.now += ms


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).parents[1] / "python/main.py").read_text())
        # Load console definitions without starting hardware or the prompt.
        tree.body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))
                     and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                              and isinstance(node.value.func, ast.Name) and node.value.func.id == "main")]
        self.clock = Clock()
        self.tx = Mock()
        self.tx.range.return_value = (87500000, 88500000)
        self.tx.send.side_effect = len
        self.tx.diag.return_value = (0, 0)
        self.tx.ring_stats.return_value = (0, 0)
        self.tx.mode.return_value = "tone"
        self.tx.sample_peak.return_value = 1000
        self.tx.clips.return_value = 0
        self.tx.squelch.return_value = 0
        self.stdin = Mock()
        self.stdin.readline.side_effect = AssertionError("must not wait for a newline")
        self.ns = dict(pico_tx=self.tx, array=array, time=self.clock, print=Mock(),
                       sys=types.SimpleNamespace(stdin=self.stdin, stdout=io.StringIO()))
        exec(compile(tree, "main.py", "exec"), self.ns)

    def test_transmit_returns_to_prompt(self):
        self.ns["show_log"] = Mock(side_effect=AssertionError("blocking autolog"))
        for command in ("tone 1000", "cw E", "fsk 1200 4500 55aa repeat 0",
                        "ook 1200 55aa", "psk 2400 4 0123",
                        "chirp 87900000 87901000 10"):
            with self.subTest(command=command):
                self.tx.start.reset_mock()
                self.assertTrue(self.ns["do_command"](command))
                self.tx.start.assert_called_once()
        self.ns["show_log"].assert_not_called()

    def test_log_preserves_input_and_checks_within_20ms(self):
        poller = Mock()
        poller.poll.side_effect = [[], [(0, 1)]]
        self.ns["make_poller"] = lambda: poller
        self.ns["show_log"]()
        self.assertEqual(self.clock.sleeps, [20])
        self.stdin.read.assert_not_called()

    def test_vbar_exits_on_single_byte(self):
        poller = Mock()
        poller.poll.return_value = [(0, 1)]
        self.ns["make_poller"] = lambda: poller
        self.ns["vbar"]()
        self.stdin.read.assert_called_once_with(1)
        self.stdin.readline.assert_not_called()
        self.assertTrue(self.ns["sys"].stdout.getvalue().endswith("\x1b[?25h\n"))

    def test_vbar_clock_wrap_and_native_meter(self):
        self.clock.now = (1 << 30) - 4
        poller = Mock()
        poller.poll.side_effect = [[], [], [], [(0, 1)]]
        self.ns["make_poller"] = lambda: poller
        self.ns["vbar"]()
        self.assertEqual(self.clock.sleeps, [20])
        self.assertEqual(self.tx.sample_peak.call_count, 2)
        self.tx.samples.assert_not_called()
        # Constant input must not alternate full/decayed values or redraw an
        # unchanged row. The clock crosses its wrap between the two samples.
        self.assertEqual(self.ns["sys"].stdout.getvalue().count("\r["), 1)

    def test_vbar_clears_suffix_when_becoming_silent(self):
        self.tx.sample_peak.side_effect = [16000, 0]
        poller = Mock()
        poller.poll.side_effect = [[], [], [], [(0, 1)]]
        self.ns["make_poller"] = lambda: poller
        self.ns["vbar"]()
        output = self.ns["sys"].stdout.getvalue()
        self.assertIn("\rsilent (any key or Ctrl-C to stop)\x1b[K", output)
        self.assertNotIn("\x1b[2K", output)  # no blank intermediate frame

    def test_vbar_restores_cursor_on_interrupt_or_error(self):
        for error in (KeyboardInterrupt, OSError):
            with self.subTest(error=error):
                self.ns["sys"].stdout = io.StringIO()
                poller = Mock()
                poller.poll.side_effect = error
                self.ns["make_poller"] = lambda: poller
                with self.assertRaises(error):
                    self.ns["vbar"]()
                output = self.ns["sys"].stdout.getvalue()
                self.assertTrue(output.startswith("\x1b[?25l"))
                self.assertTrue(output.endswith("\x1b[?25h\n"))

    def test_old_firmware_meter_and_counter_wrap(self):
        self.ns["pico_tx"] = types.SimpleNamespace(samples=lambda n: array.array("h", [-32768, 2]).tobytes())
        self.assertEqual(self.ns["sample_peak"](), 32768)
        self.assertIsNone(self.ns["timing_snapshot"]())
        self.assertEqual(self.ns["counter_delta"](3, 0xFFFFFFFE), 5)

    def test_poller_does_not_discard_pasted_input(self):
        poller = Mock()
        self.ns["select"] = types.SimpleNamespace(poll=lambda: poller, POLLIN=1)
        self.assertIs(self.ns["make_poller"](), poller)
        poller.poll.assert_not_called()
        self.stdin.read.assert_not_called()

    def test_pwm_on_old_firmware_never_invokes_intrusive_probe(self):
        probe = Mock(side_effect=AssertionError("old isr_cost mutates playback"))
        self.ns["pico_tx"] = types.SimpleNamespace(
            range=lambda: (87500000, 88500000),
            pwm_info=lambda: (999, 256, 1, 48000000),
            diag=lambda: (48000, 0), isr_cost=probe)
        self.assertTrue(self.ns["do_command"]("pwm"))
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
