# SPDX-License-Identifier: MIT
"""Block C starts from a reset MAGIC count (spec §4.7, §4.12, §7.2; review findings A-1 and A-3).

A-1: a Block A that ends after MAGIC #1's SET_CUR and before MAGIC #2's (a stop in MAGIC #1's or STREAM_OFF's ack
poll or in the 250 ms sleep, or MAGIC #1's ack lost) sends STREAM_ON first, as a failed MAGIC #3 does, so Block C's
MAGICs are the cadence's first, second and (7b) third. With teardown_magic3 its OFF writes then latch even when PWM
was already on when the session began (an earlier Block C whose XU part aborted after its STREAM_OFF). A Block A
stopped before MAGIC #1 sends nothing. Driver level and through the Service.
A-3: check_trace splits such a session right (Block A, that STREAM_ON, the whole Block C) and reports a Block C that
starts at another MAGIC count (the driver before A-1) as misaligned, instead of three unrelated violations; a session
whose Block A never started (a stop before MAGIC #1: Block C alone) is no longer read as a broken activation."""
import ffutil  # first: installs the port guard

import collections
import re
import unittest

ffutil.setup_path()
from test_ff_service import SvcCase  # noqa: E402
from frameface.config import Config  # noqa: E402
from frameface.policy import STRATEGIES  # noqa: E402
from frameface.sim import VftModel, check_trace, make_rig  # noqa: E402
from frameface.usbfs import UsbDevice  # noqa: E402
from frameface.vft import Aborted, DriverConfig, VftDriver, XuTimeout  # noqa: E402

XU = ("MAGIC", "STREAM_OFF", "STREAM_ON", "REG_WRITE")
TD15 = ["MAGIC", "STREAM_OFF", "MAGIC"] + ["REG_WRITE"] * 11 + ["MAGIC"]       # Block C's SET_CURs with 7b
MISALIGNED_1 = "(9) Block C's MAGIC cadence misaligned: Block C starts at MAGIC count 1"


def pwm(rig):
    return [rig.vft.regs.get(a) for a in (2, 3, 4)]


def names(ops):
    return [o["name"] for o in ops if o["kind"] == "ctrl" and o["name"] in XU]


def driver(rig, magic3=True):
    node = rig.node()
    drv = VftDriver(UsbDevice(rig.kernel, node.devpath), node, rig.clock, None, DriverConfig(teardown_magic3=magic3),
                    inline=True)
    drv.open()
    rig.kernel.note(strategy="S0", n=64, set_config=False)
    return drv


def ended_early(test, rig, drv, at=None, lose=None):
    """Block A stopped at `at` (kind, phase, nth, delay; "now": before it starts) or with the ack of `lose` (kind, nth)
    lost, then Block C."""
    seen = collections.Counter()

    def hook(kind, phase):
        seen[kind, phase] += 1
        if at not in (None, "now") and (kind, phase, seen[kind, phase]) == at[:3]:
            rig.clock.call_later(at[3], drv.abort.set)     # request_stop() lands
        if lose is not None and phase == "set":
            rig.vft.m.ack_never = (kind, seen[kind, phase]) == lose
    rig.vft.on_xu = hook
    if at == "now":
        drv.abort.set()
    with test.assertRaises(Aborted if at is not None else XuTimeout):
        drv.activate(STRATEGIES["S0"], 0xFF, 0xB2, 64)
    rig.vft.on_xu, rig.vft.m.ack_never = None, False
    rig.kernel.note(abort=at is not None)
    drv.teardown()


class DriverCadence(unittest.TestCase):
    def latched_on(self):
        """A rig whose PWM 0x02-0x04 are on when the next session opens: a session's Block A latched them and its
        Block C lost the ack of its second MAGIC (after STREAM_OFF), so its XU part aborted (W usb: teardown xu
        aborted)."""
        rig = make_rig(VftModel())
        self.addCleanup(rig.close)
        drv, seen = driver(rig), collections.Counter()
        drv.activate(STRATEGIES["S0"], 0xFF, 0xB2, 64)

        def hook(kind, phase):
            if phase == "set":
                seen[kind] += 1
                rig.vft.m.ack_never = (kind, seen[kind]) == ("magic", 2)
        rig.vft.on_xu = hook
        drv.teardown()
        rig.vft.on_xu, rig.vft.m.ack_never = None, False
        rig.clock.advance(10.0)                 # past the fake's 8 s settle window
        self.assertEqual((pwm(rig), drv.teardown_path), ([0xFF] * 3, "clean"))
        return rig

    def test_block_a_ended_after_magic_1_restarts_the_cadence_and_block_c_switches_the_ir_off(self):
        cases = {"magic1_poll": (("magic", "set", 1, 0.0005), None, ["MAGIC", "STREAM_ON"]),
                 "magic1_ack": (("magic", "done", 1, 0.00005), None, ["MAGIC", "STREAM_OFF", "STREAM_ON"]),
                 "stream_off_poll": (("stream_off", "set", 1, 0.0005), None, ["MAGIC", "STREAM_OFF", "STREAM_ON"]),
                 "sleep_250ms": (("stream_off", "done", 1, 0.1), None, ["MAGIC", "STREAM_OFF", "STREAM_ON"]),
                 "magic1_lost": (None, ("magic", 1), ["MAGIC", "STREAM_ON"]),
                 "before_magic1": ("now", None, [])}           # nothing reached the device: no STREAM_ON
        for name, (at, lose, block_a) in cases.items():
            with self.subTest(case=name):
                rig = self.latched_on()
                mark = len(rig.kernel.ops)
                drv = driver(rig)
                ended_early(self, rig, drv, at, lose)
                self.assertEqual(names(rig.kernel.ops[mark:]), block_a + TD15)
                self.assertEqual(pwm(rig), [0, 0, 0])                  # the 7b MAGIC is the cadence's third
                self.assertEqual((drv.teardown_path, rig.kernel.violations, check_trace(rig.kernel.ops)),
                                 ("clean", [], []))


class TraceSplit(unittest.TestCase):
    """A-3: check_trace on a session whose Block A was stopped in the 250 ms sleep before MAGIC #2."""

    def trace(self, magic3, restart):
        rig = make_rig(VftModel())
        self.addCleanup(rig.close)
        ended_early(self, rig, driver(rig, magic3), ("stream_off", "done", 1, 0.1))
        ops = rig.kernel.ops
        on = [i for i, o in enumerate(ops) if o["kind"] == "ctrl" and o["name"] == "STREAM_ON"]
        if on and not restart:                  # the driver before A-1: Block C right after STREAM_OFF
            nxt = next(j for j in range(on[0] + 1, len(ops)) if ops[j]["kind"] == "ctrl" and ops[j]["name"] in XU)
            ops = [o for j, o in enumerate(ops) if not (on[0] <= j < nxt and o["kind"] == "ctrl")]   # notes stay
        self.assertEqual(names(ops)[:3], ["MAGIC", "STREAM_OFF", "STREAM_ON" if restart else "MAGIC"])
        return ops

    def test_a_block_a_that_ended_after_magic_1_and_stream_off(self):
        for magic3 in (True, False):
            with self.subTest(magic3=magic3):
                self.assertEqual(check_trace(self.trace(magic3, True)), [])
                got = [re.sub(r"^fd \d+ ", "", p) for p in check_trace(self.trace(magic3, False))]
                self.assertEqual(got, [MISALIGNED_1])  # not (3) unexpected XU commands, (9) no teardown XU part...


class ServiceCadence(SvcCase):
    def test_a_stop_before_magic_2_switches_off_ir_that_was_already_on(self):
        rig, s = self.service(cfg=Config(token="", teardown_magic3=True))
        self.stream_ok(s)
        seen, fired = collections.Counter(), []

        def lose(kind, phase):                  # the idle teardown's XU part aborts after its STREAM_OFF: IR stays on
            if phase == "set" and s.state == "TEARDOWN":
                seen[kind] += 1
                rig.vft.m.ack_never = (kind, seen[kind]) == ("magic", 2)
        rig.vft.on_xu = lose
        self.addCleanup(setattr, rig.vft, "on_xu", None)
        s.client_override = 0
        self.assertTrue(self.run_for(s, 60, lambda s: s.state == "IDLE"))
        rig.vft.on_xu, rig.vft.m.ack_never = None, False
        self.assertEqual(pwm(rig), [0xFF] * 3)
        self.assertTrue(s.log.has("W usb: teardown xu aborted"))
        mark = len(rig.kernel.ops)

        def stop(kind, phase):                  # request_stop() lands in the 250 ms sleep before MAGIC #2
            if (kind, phase) == ("stream_off", "done") and s.state == "ACTIVATING" and not fired:
                fired.append(rig.clock.monotonic())
                rig.clock.call_later(0.1, s.request_stop)
        rig.vft.on_xu = stop
        s.client_override = 1
        self.assertTrue(self.run_for(s, 60, lambda s: s.state == "STOPPED", stop_at=()))
        self.assertEqual((len(fired), names(rig.kernel.ops[mark:])), (1, ["MAGIC", "STREAM_OFF", "STREAM_ON"] + TD15))
        self.assertEqual((pwm(rig), s.drivers[-1].teardown_path, s.store.load_runtime().dirty, s.failures),
                         ([0, 0, 0], "clean", False, []))
        self.check(rig, s)


if __name__ == "__main__":
    unittest.main()
