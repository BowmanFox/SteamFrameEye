# SPDX-License-Identifier: MIT
"""The stop budget (spec §4.6, known item K1): from request_stop() (SIGTERM) to the end of the teardown at most
7.0 s, whatever the tracker's XU ack latency, so the unit's TimeoutStopSec=8 leaves the interpreter at least 1 s.

A stop inside the cadence window (MAGIC #2 .. STREAM_ON's SET_CUR) still waits for STREAM_ON's SET_CUR, but once
the stop is seen each window command waits at most teardown_ack_ms for its ack and the window ends STOP_WINDOW_S
after the stop; hitting either limit ends Block A on the in-window failure path (recovery prefix, or STREAM_ON
first) with Aborted. On SimClock, driver level (VftDriver on FakeKernel) and service level (inline Service), with
teardown_magic3 (the longest Block C): acks after 0.19 s (just under the 0.2 s limit: the longest Block C, and
the IR still ends off), after 0.99 s, and never from the stop on; the stop at every window position, and while a
Block C with the recovery prefix runs. No USB reset goes out once a stop is pending. A normal tracker keeps its
exact sequences: test_ff_driver_matrix and test_ff_service_matrix Cadence, test_ff_driver BlockC.

A silent EP0 (SilentEp0*: FakeKernel.silence_ep0, every control transfer ends at its own timeout with ETIMEDOUT, a
CLEAR_HALT or SETINTERFACE after the kernel's 5 s) is inside the budget too: once a control transfer has ended with
ETIMEDOUT and none answered since, Block C skips step 11's CLEAR_HALT (in every teardown; W usb: clear_halt skipped
why=ep0_timeout; any stop skips it too: I ... why=stopping), the reaper keeps a halt after an EPIPE (also with a stop
pending), and with a stop pending a control transfer times out after teardown_ack_ms, OPENING and Block B send nothing
more to a silent EP0 and Block C ends at its first ETIMEDOUT. Measured with EP0 silenced at the stop (SimClock,
driver): S0 0.73 s in the cadence window, 0.63 s between MAGIC #1 and #2, 0.23 s while STARTING or STREAMING; S4
(GET_LEN around each SET_CUR) 1.13, 0.83 and 0.43 s (with 1 s timeouts: 3.13 / 3.03 / 1.03 s and 6.13 / 5.03 / 2.03 s;
with step 11 too: 8.13 / 8.03 / 6.03 s and 11.13 / 10.03 / 7.03 s). Controls: an XU ack timeout, a STALL, EPROTO, slow
transfers and a timeout followed by an answer or by STALLs keep step 11 and Block C's exact sequence. EP0 silent later
in the stop: test_ff_stop_ep0."""
import ffutil  # first: installs the port guard

import collections
import errno
import threading
import unittest
from unittest import mock

ffutil.setup_path()
from test_ff_driver_matrix import ops  # noqa: E402
from test_ff_service import SvcCase  # noqa: E402
from frameface import uvc  # noqa: E402
from frameface.assembler import BulkReassembler, FrameAssembler, FramePool  # noqa: E402
from frameface.config import Config  # noqa: E402
from frameface.logutil import Log  # noqa: E402
from frameface.policy import STRATEGIES, Decision, Failure as F, xu_len_for  # noqa: E402
from frameface.sim import VftModel, check_trace, make_rig  # noqa: E402
from frameface.usbfs import CONTROL, UsbDevice  # noqa: E402
from frameface.vft import (ActivationError, Aborted, DriverConfig, ProbeReject, VftDriver, XuError,  # noqa: E402
                           XuTimeout)

BUDGET_S = 7.0
XU = ("MAGIC", "STREAM_OFF", "STREAM_ON", "REG_WRITE")
WINDOW = [("magic", 2)] + [("reg_write", i) for i in range(1, 12)] + [("magic", 3), ("stream_on", 1)]
# every stop position inside the window: just after a SET_CUR (in its ack poll), just after an ack (before the next
# command; STREAM_ON's ack never comes within its 100 ms poll here) and inside the 100 ms sleep before MAGIC #3
POSITIONS = [(k, "set", i, 0.0005) for k, i in WINDOW] + [(k, "done", i, 0.0001) for k, i in WINDOW[:-1]] + \
            [("reg_write", "done", 11, 0.05)]
LATENCIES = {"0.19": (0.19, False), "0.99": (0.99, False), "never": (0.0, True)}    # (ack delay, no ack from the stop)
# between MAGIC #1 and #2: in MAGIC #1's ack poll, at its ack, in STREAM_OFF's poll, in the 250 ms sleep
EARLY = [("magic", "set", 1, 0.0005), ("magic", "done", 1, 0.0001), ("stream_off", "set", 1, 0.0005),
         ("stream_off", "done", 1, 0.1)]
SKIPPED = "W usb: clear_halt skipped why=ep0_timeout"
STOPPING = "I usb: clear_halt skipped why=stopping"    # INFO: the normal stop path, not a fault
TAIL = ["release1", "release0", "close"]


def pwm(rig):
    return [rig.vft.regs.get(a) for a in (2, 3, 4)]


def tail(ops_):
    """Steps 11-13 as op kinds: clear_halt, release<i>, close."""
    return [o["kind"] + str(o.get("iface", "")) for o in ops_ if o["kind"] in ("clear_halt", "release", "close")]


def from_timeout(ops_):
    """The ops from the first EP0 request that ended with ETIMEDOUT on ([] without one)."""
    return ops_[next((i for i, o in enumerate(ops_) if o.get("err") == "ETIMEDOUT"), len(ops_)):]


def skips(log, line=SKIPPED):
    return sum(line in x for x in log.lines)


class DriverBudget(unittest.TestCase):
    def stop_in_block_a(self, delay, never, at):
        """A stop at `at` (kind, phase, nth, delay) of Block A; seconds from the stop to the end of Block C."""
        rig = make_rig(VftModel(ack_delay_s=0.0 if never else delay, ack_busy_polls=0 if delay else 2))
        self.addCleanup(rig.close)
        node, seen, t = rig.node(), collections.Counter(), {}
        drv = VftDriver(UsbDevice(rig.kernel, node.devpath), node, rig.clock, None, DriverConfig(teardown_magic3=True),
                        inline=True)
        drv.open()
        rig.kernel.note(strategy="S0", n=64, set_config=False)

        def stop():                             # request_stop(): the abort flag (and, `never`, a dead tracker)
            t["stop"] = rig.clock.monotonic()
            rig.vft.m.ack_never = never
            drv.abort.set()

        def hook(kind, phase):
            seen[kind, phase] += 1
            if "stop" not in t and (kind, phase, seen[kind, phase]) == at[:3]:
                rig.clock.call_later(at[3], stop)
        rig.vft.on_xu = hook
        with self.assertRaises(Aborted):        # a stop ends Block A with Aborted, never as a device failure
            drv.activate(STRATEGIES["S0"], 0xFF, 0xB2, 64)
        rig.kernel.note(abort=True)
        drv.teardown()
        rig.vft.on_xu = None
        self.assertEqual((drv.teardown_path, rig.kernel.violations, check_trace(rig.kernel.ops)), ("clean", [], []))
        return rig.clock.monotonic() - t["stop"], rig

    def test_a_stop_anywhere_in_the_cadence_window_ends_within_the_budget(self):
        worst = {}
        for name, (delay, never) in LATENCIES.items():
            for at in POSITIONS:
                if at[1] == "done" and never:
                    at = at[:3] + (0.0,)        # the stop at the ack itself: every later ack is lost
                with self.subTest(latency=name, at=at[:3]):
                    took, rig = self.stop_in_block_a(delay, never, at)
                    self.assertLessEqual(took, BUDGET_S)
                    worst[name] = max(worst.get(name, 0.0), took)
                    if name == "0.19":          # acks within 0.2 s: Block C still switches the IR off
                        self.assertEqual(pwm(rig), [0, 0, 0])
        self.assertLess(worst["0.99"], 1.0, worst)          # one capped command, then Block C's first timeout
        self.assertLess(worst["never"], 1.0, worst)
        self.assertGreater(worst["0.19"], 5.0, worst)       # the recovery prefix and steps 3-7b at 0.19 s each

    def test_block_c_with_the_recovery_prefix_and_slow_acks_fits_the_budget(self):
        """The longest Block C: REG_WRITE 8's ack lost (no stop), then the prefix and steps 3-7b at 0.19 s per ack.
        A stop that lands anywhere in it waits at most for the rest of it."""
        rig = make_rig(VftModel(ack_delay_s=0.19, ack_busy_polls=0))
        self.addCleanup(rig.close)
        node, seen = rig.node(), collections.Counter()
        drv = VftDriver(UsbDevice(rig.kernel, node.devpath), node, rig.clock, None, DriverConfig(teardown_magic3=True),
                        inline=True)
        drv.open()
        rig.kernel.note(strategy="S0", n=64, set_config=False)

        def hook(kind, phase):
            if phase == "set":
                seen[kind] += 1
                rig.vft.m.ack_never = (kind, seen[kind]) == ("reg_write", 8)
        rig.vft.on_xu = hook
        with self.assertRaises(XuTimeout):
            drv.activate(STRATEGIES["S0"], 0xFF, 0xB2, 64)
        start = rig.clock.monotonic()
        drv.teardown()
        took = rig.clock.monotonic() - start
        names = [o["name"] for o in rig.kernel.ops if o["kind"] == "ctrl" and o["name"] in XU]
        self.assertEqual(names[-27:], ["REG_WRITE"] * 11 + ["MAGIC", "MAGIC", "STREAM_OFF", "MAGIC"] +
                         ["REG_WRITE"] * 11 + ["MAGIC"])
        self.assertTrue(5.0 < took <= BUDGET_S - 0.5, took)   # 0.5 s left for the stop path around it
        self.assertEqual((pwm(rig), drv.teardown_path, rig.kernel.violations, check_trace(rig.kernel.ops)),
                         ([0, 0, 0], "clean", [], []))


class ServiceBudget(SvcCase):
    def stop_service(self, delay, never, at, lose=None):
        """An inline Service whose first activation gets request_stop() at `at`; seconds from request_stop() to
        STOPPED (the teardown and the shutdown). `lose`: the (kind, n) SET_CUR whose ack never comes (no stop)."""
        rig, s = self.service(cfg=Config(token="", teardown_magic3=True), ack_delay_s=0.0 if never else delay,
                              ack_busy_polls=0 if delay else 2)
        seen, t = collections.Counter(), {}

        def stop():
            t["stop"] = rig.clock.monotonic()
            rig.vft.m.ack_never = never
            s.request_stop()

        def hook(kind, phase):
            seen[kind, phase] += 1
            if lose is not None and phase == "set":
                rig.vft.m.ack_never = (kind, seen[kind, phase]) == lose
            if "stop" not in t and (kind, phase, seen[kind, phase]) == at[:3] and s.state == at[4]:
                rig.clock.call_later(at[3], stop)
        rig.vft.on_xu = hook
        self.addCleanup(setattr, rig.vft, "on_xu", None)
        self.assertTrue(self.run_for(s, 120, lambda s: s.state == "STOPPED", stop_at=()))
        stopped = next(t_ for st, t_ in s.states if st == "STOPPED")
        self.assertEqual((s.drivers[-1].teardown_path, s.store.load_runtime().dirty), ("clean", False))
        self.check(rig, s)
        return stopped - t["stop"], rig, s

    def test_a_stop_anywhere_in_the_cadence_window_stops_the_service_within_the_budget(self):
        for name, (delay, never) in LATENCIES.items():
            for at in POSITIONS[::2] + [("reg_write", "done", 11, 0.05)]:
                with self.subTest(latency=name, at=at[:3]):
                    took, rig, s = self.stop_service(delay, never, at + ("ACTIVATING",))
                    self.assertLessEqual(took, BUDGET_S)
                    self.assertEqual(s.failures, [])        # a stop is no device failure: no decision, no reset
                    if name == "0.19":
                        self.assertEqual(pwm(rig), [0, 0, 0])

    def test_a_stop_during_a_block_c_with_the_recovery_prefix(self):
        took, rig, s = self.stop_service(0.19, False, ("reg_write", "set", 9, 0.0005, "TEARDOWN"),
                                         lose=("reg_write", 8))   # the stop lands at the prefix's first OFF write
        self.assertLessEqual(took, BUDGET_S)
        self.assertEqual((s.failures, pwm(rig)), ([F.XU_TIMEOUT], [0, 0, 0]))    # REG_WRITE 8's own failure

    def test_no_usb_reset_once_a_stop_is_pending(self):
        rig, s = self.service()
        self.stream_ok(s)
        s.act.request, s._next_tick = (F.NO_FRAMES, {}), s.clock.monotonic()     # the tick that fails runs now ...
        s.request_stop()                        # ... with the SIGTERM already in: its reset would delay the stop
        with mock.patch("frameface.supervisor.on_failure", return_value=Decision("reset_port", strategy="S0", row=13)):
            s.step(0.01)
        self.assertEqual((s.state, ops(s, "reset"), s.counts["resets"], s.failures), ("STOPPED", [], 0, [F.NO_FRAMES]))
        self.assertTrue(s.log.has("W usb: reset skipped reason=no_frames why=stopping"))
        self.check(rig, s)


class _Slow:
    """A Sys around the fake kernel: a slow but answering EP0. The next `count` CONTROL ioctls (None: all) take
    `seconds` each, then are answered."""

    def __init__(self, inner, clock, seconds, count=None):
        self.inner, self.clock, self.seconds, self.count = inner, clock, seconds, count

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def ioctl(self, fd, request, arg):
        if request == CONTROL and self.count != 0:
            self.count = None if self.count is None else self.count - 1
            self.clock.advance(self.seconds)
        return self.inner.ioctl(fd, request, arg)


class _Ep0Case(unittest.TestCase):
    """Driver level: VftDriver (inline, teardown_magic3, its own Log) on FakeKernel + SimClock."""

    def session(self, strategy="S0", magic3=True, **knobs):
        rig = make_rig(VftModel(**knobs))
        self.addCleanup(rig.close)
        node, s = rig.node(), STRATEGIES[strategy]
        log = Log("debug", None, journal=True, stream=None, clock=rig.clock, keep=400)
        drv = VftDriver(UsbDevice(rig.kernel, node.devpath), node, rig.clock, log, DriverConfig(teardown_magic3=magic3),
                        inline=True)
        ident = drv.open(set_config=s.set_config)
        n = xu_len_for(strategy, "auto", None, None, ident.get_len)
        rig.kernel.note(strategy=strategy, n=n, set_config=s.set_config)
        return rig, drv, log, s, n, ident

    @staticmethod
    def stream(rig, drv, s, n, ident, seconds=1.0):
        drv.activate(s, 0xFF, 0xB2, n)
        params = drv.negotiate(s, 0xFF, 0xB2, n)
        drv.start(params, BulkReassembler(FrameAssembler(320000, FramePool(320000), rig.clock), params.max_payload,
                                          ident.layout.max_packet))
        end = rig.clock.monotonic() + seconds
        while rig.clock.monotonic() < end:
            drv.pump(0.01)

    def stop_silent(self, strategy, at):
        """The stop at `at` (a Block A position, "starting" or "streaming"), EP0 silent from then on; (seconds from the
        stop to the end of Block C, rig, log, driver)."""
        rig, drv, log, s, n, ident = self.session(strategy)
        seen, t = collections.Counter(), {}

        def stop():                             # request_stop() as the tracker's EP0 stops answering
            t["stop"] = rig.clock.monotonic()
            rig.kernel.silence_ep0()
            drv.abort.set()

        def hook(kind, phase):
            seen[kind, phase] += 1
            if "stop" not in t and (kind, phase, seen[kind, phase]) == at[:3]:
                rig.clock.call_later(at[3], stop)
        if isinstance(at, str):
            self.stream(rig, drv, s, n, ident, 0.0 if at == "starting" else 1.0)
            stop()
        else:
            rig.vft.on_xu = hook
            with self.assertRaises(Aborted):
                drv.activate(s, 0xFF, 0xB2, n)
            rig.vft.on_xu = None
            rig.kernel.note(abort=True)
        drv.teardown()
        return rig.clock.monotonic() - t["stop"], rig, log, drv

    @staticmethod
    def stop_at(rig, drv, delay):
        """request_stop() `delay` s from now (inside the request that is about to wait on a silent EP0)."""
        t = {}

        def stop():
            t["stop"] = rig.clock.monotonic()
            drv.abort.set()
        rig.clock.call_later(delay, stop)
        return t

    def assert_silent_teardown(self, rig, log, drv, service=True):
        """No CLEAR_HALT once a request ended with ETIMEDOUT, one WARN line, steps 12-13, a clean trace."""
        after = from_timeout(rig.kernel.ops)
        self.assertTrue(after, "EP0 was never asked")
        self.assertEqual((tail(after), skips(log)), (TAIL, 1))
        self.assertEqual((drv.teardown_path, rig.kernel.violations, check_trace(rig.kernel.ops, service)),
                         ("clean", [], []))

    def assert_step_11(self, rig, log, drv, service=True):
        """Control: step 11's CLEAR_HALT went out and no WARN line."""
        self.assertEqual((tail(rig.kernel.ops)[-4:], skips(log), skips(log, STOPPING)), (["clear_halt"] + TAIL, 0, 0))
        self.assertEqual((drv.teardown_path, rig.kernel.violations, check_trace(rig.kernel.ops, service)),
                         ("clean", [], []))

    def assert_stop_skip(self, rig, log, drv):
        """A stop pending at step 11 and EP0 answering: no CLEAR_HALT, one I usb: clear_halt skipped why=stopping."""
        t = tail(rig.kernel.ops)
        self.assertEqual((t[-3:], t[-4:-3], skips(log), skips(log, STOPPING)), (TAIL, [], 0, 1))
        self.assertEqual((drv.teardown_path, rig.kernel.violations, check_trace(rig.kernel.ops)), ("clean", [], []))


class SilentEp0(_Ep0Case):
    def test_a_silent_ep0_at_the_stop_ends_within_the_budget_without_step_11(self):
        worst = collections.defaultdict(float)
        for strategy in ("S0", "S4"):
            for group, positions in (("window", POSITIONS), ("early", EARLY), ("stream", ("starting", "streaming"))):
                for at in positions:
                    with self.subTest(strategy=strategy, at=at if isinstance(at, str) else at[:3]):
                        took, rig, log, drv = self.stop_silent(strategy, at)
                        self.assertLessEqual(took, BUDGET_S)
                        self.assert_silent_teardown(rig, log, drv)
                        worst[strategy, group] = max(worst[strategy, group], took)
        # one stop-path timeout (teardown_ack_ms, 200 ms) per transfer still sent: the window command's, the sleep
        # before MAGIC #3, Block A's STREAM_ON (MAGIC #1 or #3 sent), Block C's first command; S4 adds a GET_LEN before
        # each SET_CUR of Block A. Measured S0 0.73 / 0.63 / 0.23 s, S4 1.13 / 0.83 / 0.43 s (with 1 s timeouts: S0
        # 3.13 / 3.03 / 1.03 s, S4 6.13 / 5.03 / 2.03 s)
        bound = {("S0", "window"): 0.8, ("S0", "early"): 0.7, ("S0", "stream"): 0.3,
                 ("S4", "window"): 1.2, ("S4", "early"): 0.9, ("S4", "stream"): 0.5}
        self.assertEqual({k: v for k, v in worst.items() if v > bound[k]}, {}, dict(worst))

    def test_an_idle_teardown_with_a_silent_ep0_skips_step_11_too(self):
        rig, drv, log, s, n, ident = self.session()
        self.stream(rig, drv, s, n, ident)
        rig.kernel.silence_ep0()                # no stop: a teardown for idle, not worn, a failure...
        t0 = rig.clock.monotonic()
        drv.teardown()
        self.assertLess(rig.clock.monotonic() - t0, 1.1)     # Block C's first MAGIC (1 s), not 5 s more for step 11
        self.assert_silent_teardown(rig, log, drv)

    def test_controls_an_answering_ep0_keeps_step_11(self):
        """Only an ETIMEDOUT makes EP0 silent; a STALL (EPIPE) is an answer; another error (EPROTO) changes nothing."""
        for case in ("xu_timeout", "stall", "slow_one", "slow_all", "timeout_then_answer", "eproto", "stall_all",
                     "timeout_stall"):
            with self.subTest(case=case):
                rig, drv, log, s, n, ident = self.session()
                self.stream(rig, drv, s, n, ident)
                m = len(rig.kernel.ops)
                if case == "xu_timeout":        # acks stop, control transfers are answered (XuTimeout after 200 ms)
                    rig.vft.m.ack_never = True
                elif case in ("stall", "eproto"):   # Block C's first SET_CUR STALLs (XuError): an answer too; EPROTO
                    def fail(kind, phase, code=errno.EPIPE if case == "stall" else errno.EPROTO):
                        if phase == "set":
                            rig.vft.on_xu = None
                            raise OSError(code, case)
                    rig.vft.on_xu = fail
                elif case.startswith("slow"):   # Block C's first SET_CUR, or every transfer, answered after 0.9 s
                    drv.dev.sys = _Slow(rig.kernel, rig.clock, 0.9, 1 if case == "slow_one" else None)
                elif case != "stall_all":       # one request times out, the next is answered (or STALLs)
                    rig.kernel.silence_ep0(count=1)
                    with self.assertRaises(XuError):
                        drv.xu(uvc.reg_read(2, n), 200)
                    if case == "timeout_then_answer":
                        self.assertEqual(drv.xu(uvc.reg_read(2, n), 200)[0], "done")
                if case.startswith("stall_") or case == "timeout_stall":   # every EP0 request STALLs (ERROR_CODE too)
                    rig.vft.control = mock.Mock(side_effect=OSError(errno.EPIPE, "STALL"))
                drv.teardown()
                names = [o["name"] for o in rig.kernel.ops[m:] if o.get("name") in XU]
                self.assertEqual(names, ["MAGIC", "STREAM_OFF", "MAGIC"] + ["REG_WRITE"] * 11 + ["MAGIC"]
                                 if case in ("slow_one", "timeout_then_answer") else ["MAGIC"])
                self.assert_step_11(rig, log, drv, service=case not in ("timeout_then_answer", "timeout_stall"))

    def test_a_timeout_then_an_answer_keeps_the_recovery_prefix_and_block_c_exact(self):
        """REG_WRITE 4's (PWM 0x02) first poll times out (Block A ends after MAGIC #2, no stop), then EP0 answers
        again: Block C runs the prefix and steps 3-7b exactly as before, PWM 0 latches, step 11 goes out."""
        rig, drv, log, s, n, ident = self.session()

        def hook(kind, phase):
            if (kind, phase) == ("reg_write", "set") and rig.vft.magic_count == 2 and not hook.done:
                hook.done = rig.kernel.ops[-1]["reg"] == 0x02  # REG_WRITE 4 (PWM 0x02) on: its first poll
                if hook.done:
                    rig.kernel.silence_ep0(count=1)
        hook.done = False
        rig.vft.on_xu = hook
        with self.assertRaises(XuError):
            drv.activate(s, 0xFF, 0xB2, n)
        rig.vft.on_xu = None
        m = len(rig.kernel.ops)
        drv.teardown()
        names = [o["name"] for o in rig.kernel.ops[m:] if o.get("name") in XU]
        self.assertEqual(names, ["REG_WRITE"] * 11 + ["MAGIC", "MAGIC", "STREAM_OFF", "MAGIC"] + ["REG_WRITE"] * 11 +
                         ["MAGIC"])
        self.assertEqual(pwm(rig), [0, 0, 0])
        self.assert_step_11(rig, log, drv)

    def test_the_reaper_keeps_a_halt_while_ep0_is_silent_or_a_stop_is_pending(self):
        rig, drv, log, s, n, ident = self.session()
        self.stream(rig, drv, s, n, ident)
        rig.kernel.silence_ep0()
        halts = lambda: [o for o in rig.kernel.ops if o["kind"] == "clear_halt"]     # noqa: E731
        rig.kernel.force_status(-32, 2)         # the first CLEAR_HALT finds EP0 silent (5 s); the second is not sent
        while drv.stats["epipe"] < 2:
            drv.pump(0.01)
        self.assertEqual(([o.get("err") for o in halts()], drv.inflight), (["ETIMEDOUT"], 15))
        self.assertTrue(log.has("W usb: epipe not recovered why=ep0_timeout"))
        rig, drv, log, s, n, ident = self.session()
        self.stream(rig, drv, s, n, ident)
        drv.abort.set()                         # a stop pending, EP0 answering
        rig.kernel.note(stop=True)
        rig.kernel.force_status(-32)
        while drv.stats["epipe"] < 1:
            drv.pump(0.01)
        self.assertEqual((halts(), drv.inflight), ([], 15))
        self.assertTrue(log.has("W usb: epipe not recovered why=stopping"))
        drv.teardown()                          # step 11 is skipped with a stop pending too (why=stopping)
        self.assert_stop_skip(rig, log, drv)

    def test_an_epipe_after_the_stop_does_not_hold_up_the_teardown(self):
        rig, drv, log, s, n, ident = self.session()
        self.stream(rig, drv, s, n, ident)
        t0 = rig.clock.monotonic()
        rig.kernel.silence_ep0()
        drv.abort.set()
        rig.kernel.force_status(-32)            # reaped in step 2's 30 ms, before Block C finds EP0 silent
        drv.teardown()
        self.assertLess(rig.clock.monotonic() - t0, 0.3)     # 30 ms, Block C's first MAGIC (200 ms with a stop pending)
        self.assertEqual(drv.stats["epipe"], 1)
        self.assert_silent_teardown(rig, log, drv)

    def test_a_stop_during_a_setinterface_to_a_silent_ep0(self):
        """EP0 silent before the stop (residual: a 5 s request already waiting); the stop 1 ms into si_before's
        SETINTERFACE: no raw SET_INTERFACE, Block A sends nothing, Block C's first MAGIC, no step 11."""
        rig, drv, log, s, n, ident = self.session()
        rig.kernel.silence_ep0()
        t = self.stop_at(rig, drv, 0.001)
        with self.assertRaises(Aborted):
            drv.activate(s, 0xFF, 0xB2, n)
        rig.kernel.note(abort=True)
        drv.teardown()
        self.assertLess(rig.clock.monotonic() - t["stop"], 5.3)        # 5 s SETINTERFACE + Block C's MAGIC (0.2 s)
        self.assertEqual([o["kind"] + ":" + o.get("name", "") for o in from_timeout(rig.kernel.ops)
                          if o["kind"] in ("setif", "ctrl")], ["setif:", "ctrl:MAGIC"])
        self.assert_silent_teardown(rig, log, drv)

    def test_a_stop_during_block_b_to_a_silent_ep0(self):
        rig, drv, log, s, n, ident = self.session()
        drv.activate(s, 0xFF, 0xB2, n)
        rig.kernel.silence_ep0()
        t = self.stop_at(rig, drv, 0.0005)
        with self.assertRaises(ProbeReject):    # PROBE GET_CUR timed out; GET_DEF and SET_CUR are not sent
            drv.negotiate(s, 0xFF, 0xB2, n)
        drv.teardown()
        self.assertLess(rig.clock.monotonic() - t["stop"], 1.3)        # the GET_CUR sent before the stop (1 s), MAGIC
        self.assertEqual([o["name"] for o in from_timeout(rig.kernel.ops) if o["kind"] == "ctrl"],
                         ["PROBE_GET_CUR", "MAGIC"])
        self.assert_silent_teardown(rig, log, drv)

    def test_opening_with_a_stop_pending_sends_nothing_more_to_a_silent_ep0(self):
        rig = make_rig(VftModel())
        self.addCleanup(rig.close)
        node = rig.node()
        drv = VftDriver(UsbDevice(rig.kernel, node.devpath), node, rig.clock, None, DriverConfig(), inline=True)
        rig.kernel.silence_ep0()
        drv.abort.set()
        t0 = rig.clock.monotonic()
        with self.assertRaises(ActivationError) as cm:   # S5: SETCONFIGURATION (5 s), then no GET_INFO, no GET_LEN
            drv.open(set_config=True)
        self.assertLess(rig.clock.monotonic() - t0, 5.2)
        self.assertEqual((cm.exception.failure, [(o["kind"], o.get("err")) for o in rig.kernel.ops
                                                 if o["kind"] in ("setconfig", "ctrl")]),
                         (F.UNSUPPORTED, [("setconfig", "ETIMEDOUT")]))
        self.assertEqual((tail(rig.kernel.ops), rig.kernel.violations), (["release1", "release0", "close"], []))


class SilentEp0Trace(_Ep0Case):
    """check_trace (spec §11.3 invariant 9) mirrors step 11's rule: no CLEAR_HALT exactly while EP0 is silent."""

    def test_step_11_is_missing_exactly_while_ep0_is_silent(self):
        def variants(ops_):
            """(the trace without the CLEAR_HALT before RELEASE 1, the trace with one)."""
            i = max(j for j, o in enumerate(ops_) if o["kind"] == "release" and o["iface"] == 1)
            if ops_[i - 1]["kind"] == "clear_halt":
                return ops_[:i - 1] + ops_[i:], ops_
            halt = {"t": ops_[i]["t"], "kind": "clear_halt", "fd": ops_[i]["fd"], "ep": 0x81}
            return ops_, ops_[:i] + [halt] + ops_[i:]
        _, rig, _, _ = self.stop_silent("S0", ("reg_write", "set", 5, 0.0005))
        without, with_halt = variants(rig.kernel.ops)
        self.assertEqual(check_trace(without), [])
        self.assertEqual([p.split(") ", 1)[1] for p in check_trace(with_halt)],
                         ["CLEAR_HALT to a silent EP0 (an ETIMEDOUT and nothing answered since)"])
        rig, drv, log, s, n, ident = self.session()   # one timeout, then an answer: step 11 still required
        self.stream(rig, drv, s, n, ident)
        rig.kernel.silence_ep0(count=1)
        with self.assertRaises(XuError):
            drv.xu(uvc.reg_read(2, n), 200)
        drv.xu(uvc.reg_read(2, n), 200)
        drv.teardown()
        without, with_halt = variants(rig.kernel.ops)
        self.assertEqual(check_trace(with_halt, service=False), [])
        self.assertEqual([p.split(") ", 1)[1] for p in check_trace(without, service=False)],
                         ["teardown tail %s" % TAIL])

    def test_a_stop_before_magic_1_then_an_unanswered_block_c_is_not_an_activation(self):
        """Block A sent nothing (a stop before MAGIC #1); Block C's first MAGIC got no ack: that MAGIC is Block C's,
        not an activation's MAGIC #1 left without a Block C."""
        rig, drv, log, s, n, ident = self.session()
        drv.abort.set()
        with self.assertRaises(Aborted):
            drv.activate(s, 0xFF, 0xB2, n)
        rig.kernel.note(abort=True)
        rig.vft.m.ack_never = True
        drv.teardown()
        self.assertEqual([o["name"] for o in rig.kernel.ops if o.get("name") in XU], ["MAGIC"])
        self.assert_stop_skip(rig, log, drv)


class SilentEp0Service(SvcCase):
    """Service level: request_stop() as EP0 stops answering, then run() (inline, SimClock) returns STOPPED."""

    def stop_run(self, at):
        """`at`: a Block A position, or a state ("STARTING", "STREAMING", "OPENING"); seconds from request_stop() to
        run() returned, rig, service."""
        rig, s = self.service(cfg=Config(token="", teardown_magic3=True))
        seen, t, ev = collections.Counter(), {}, threading.Event()

        def stop():
            if "stop" not in t:
                t["stop"] = rig.clock.monotonic()
                rig.kernel.silence_ep0()
                s.request_stop()

        def hook(kind, phase):
            seen[kind, phase] += 1
            if not isinstance(at, str) and (kind, phase, seen[kind, phase]) == at[:3] and s.state == "ACTIVATING":
                rig.clock.call_later(at[3], stop)
        rig.vft.on_xu = hook
        self.addCleanup(setattr, rig.vft, "on_xu", None)
        if at == "OPENING":
            set_state = s._set_state
            s._set_state = lambda st, why="": set_state(st, why) or (st == "OPENING" and stop())
        elif isinstance(at, str):
            self.assertTrue(self.run_for(s, 30, (lambda s: s.state == at) if at == "STARTING" else
                                         lambda s: s.state == "STREAMING" and s.healthy >= 60))
            stop()
        guard = rig.clock.call_later(300.0, ev.set)
        s.run(ev)
        guard.cancel()
        return rig.clock.monotonic() - t["stop"], rig, s

    def test_a_silent_ep0_at_the_stop_stops_the_service_within_the_budget(self):
        worst = 0.0
        for at in POSITIONS[::2] + [("reg_write", "done", 11, 0.05)] + EARLY + ["STARTING", "STREAMING"]:
            with self.subTest(at=at if isinstance(at, str) else at[:3]):
                took, rig, s = self.stop_run(at)
                self.assertLessEqual(took, BUDGET_S)
                self.assertEqual((s.state, s.failures, s.store.load_runtime().dirty), ("STOPPED", [], False))
                self.assertEqual((tail(from_timeout(rig.kernel.ops)), skips(s.log)), (TAIL, 1))
                self.check(rig, s)
                worst = max(worst, took)
        self.assertLess(worst, 0.8)                 # measured 0.23-0.68 s (1 s timeouts: 1.03-3.08 s; step 11: 8.08 s)

    def test_a_stop_while_opening_sends_nothing_more_once_ep0_is_silent(self):
        took, rig, s = self.stop_run("OPENING")     # S0: GET_INFO times out (0.2 s); GET_LEN is not sent (was 3.1 s)
        self.assertLess(took, 0.4)
        self.assertEqual((s.state, s.failures), ("STOPPED", [F.UNSUPPORTED]))
        self.assertEqual([o["name"] for o in rig.kernel.ops if o["kind"] == "ctrl"], ["XU_GET_INFO"])
        self.check(rig, s)


if __name__ == "__main__":
    unittest.main()
