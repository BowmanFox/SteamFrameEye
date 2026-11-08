# SPDX-License-Identifier: MIT
"""The stop when the tracker's control endpoint (EP0) stops answering during it, and the stop rules that bound it (spec
§4.6 stop budget, §4.12 step 11 and Silent EP0; the review findings of the EP0 wave, appendix K):

- step 11's CLEAR_HALT is skipped once a stop is pending (I usb: clear_halt skipped why=stopping), not only while EP0
  is silent: it waits the kernel's 5 s whenever EP0 stops answering after Block C's last answered command (before: up
  to 10.8 s with the production config); the next session's SETINTERFACE resets the endpoint;
- with a stop pending a control transfer times out after teardown_ack_ms (200 ms), and Block C's XU part ends at the
  first command that finds EP0 silent, also at the tolerated STREAM_OFF and at S4's GET_LEN after a SET_CUR (before:
  7.2-7.8 s with acks just under 200 ms); measured worst 6.42 s with EP0 silent at the worst moment of the longest
  Block C (acks after 0.199 s), 0.73 s (S4 1.13 s) with EP0 silent from the stop;
- a stop reaches a Block C already running (Service._closing) and the stop's own Block C (Service._shutdown);
- check_trace: step 11 absent exactly while EP0 is silent or after a noted stop, the trailing MAGIC counts only when it
  reached the device, Block A's STREAM_ON after MAGIC #1 or #3 is required; the probe's raw queries go through
  vft._ctrl; the fake traces Linux errno names on every platform, halts the endpoint on a halting STALL and reports a
  CONTROL without a timeout to a silent EP0.
SimClock: VftDriver (inline) on FakeKernel, and the inline Service."""
import ffutil  # first: installs the port guard

import collections
import errno
import threading
import unittest
from unittest import mock

ffutil.setup_path()
from test_ff_service import SvcCase  # noqa: E402
from test_ff_stop_budget import BUDGET_S, STOPPING, TAIL, XU, _Ep0Case, pwm, skips, tail  # noqa: E402
from frameface import uvc  # noqa: E402
from frameface.config import Config  # noqa: E402
from frameface.policy import STRATEGIES, Failure as F, xu_len_for  # noqa: E402
from frameface.probe_active import ActiveSteps  # noqa: E402
from frameface.sim import check_trace  # noqa: E402
from frameface.usbfs import UsbDevice  # noqa: E402
from frameface.vft import Aborted, DriverConfig, VftDriver, XuError  # noqa: E402

ACK = 0.199                             # acks just under teardown_ack_ms: the longest Block C (K1)
TD15 = ["MAGIC", "STREAM_OFF", "MAGIC"] + ["REG_WRITE"] * 11 + ["MAGIC"]


def xu_sets(ops_, since=0.0):
    return [o["name"] for o in ops_ if o["kind"] == "ctrl" and o["name"] in XU and o["t"] >= since]


def halts_after(ops_, t):
    return [o for o in ops_ if o["kind"] == "clear_halt" and o["t"] >= t]


class LateSilence(_Ep0Case):
    """EP0 stops answering after the stop, once Block C has begun (SP-1, SP-2, TS-1)."""

    def late(self, strategy, magic3, where, T=None):
        """A stop in the cadence window (at MAGIC #2's SET_CUR: the window's limit, then Block C with the recovery
        prefix) or while streaming, acks after ACK s; EP0 silent T s after the stop (never: None). (seconds from the
        stop to the end of the teardown, rig, log, times)."""
        rig, drv, log, s, n, ident = self.session(strategy, magic3, ack_delay_s=ACK, ack_busy_polls=0)
        t, seen = {}, collections.Counter()

        def stop():                             # request_stop(): the aborts, then the note
            t["stop"] = rig.clock.monotonic()
            if T is not None:
                rig.kernel.silence_ep0(at=t["stop"] + T)
            drv.abort.set()
            rig.kernel.note(stop=True)
        if where == "streaming":
            self.stream(rig, drv, s, n, ident)
            stop()
        else:
            def hook(kind, phase):
                seen[kind, phase] += 1
                if (kind, phase, seen[kind, phase]) == ("magic", "set", 2):
                    rig.clock.call_later(0.0005, stop)
            rig.vft.on_xu = hook
            with self.assertRaises(Aborted):
                drv.activate(s, 0xFF, 0xB2, n)
            rig.vft.on_xu = None
            rig.kernel.note(abort=True)
        t["c"] = rig.clock.monotonic()
        drv.teardown()
        self.assertEqual((drv.teardown_path, rig.kernel.violations), ("clean", []))
        return rig.clock.monotonic() - t["stop"], rig, log, t

    @staticmethod
    def positions(rig, t, magic3):
        """Silence times (s after the stop) from a run with EP0 answering: just after Block C's last acknowledged poll
        (step 11 is next: SP-1, TS-1), just before it (that poll times out: SP-2), inside the 200 ms sleep after the
        last OFF write (SP-1), just before STREAM_OFF's first transfer and before 7b's (S4: their GET_LEN, SP-2)."""
        ops_ = [o for o in rig.kernel.ops if o["kind"] == "ctrl" and o["t"] >= t["c"]]
        acks = [i for i, o in enumerate(ops_) if o["name"] == "XU_GET_CUR" and (o.get("in") or "").startswith("56")]

        def first(i):                           # a command's first transfer: S4's GET_LEN before its SET_CUR
            return i - 1 if i and ops_[i - 1]["name"] == "XU_GET_LEN" else i
        last_write = max(i for i, o in enumerate(ops_) if o["name"] == "REG_WRITE")
        pos = {"after_last_ack": ops_[acks[-1]]["t"] + 5e-5, "before_last_ack": ops_[acks[-1]]["t"] - 5e-5,
               "last_sleep": ops_[next(i for i in acks if i > last_write)]["t"] + 0.1,
               "stream_off": ops_[first(next(i for i, o in enumerate(ops_) if o["name"] == "STREAM_OFF"))]["t"] - 5e-5}
        if magic3:
            pos["before_7b"] = ops_[first(max(i for i, o in enumerate(ops_) if o["name"] == "MAGIC"))]["t"] - 5e-5
        return {k: round(v - t["stop"], 6) for k, v in pos.items()}

    def test_ep0_silent_during_the_stop_ends_within_the_budget_without_step_11(self):
        worst = 0.0
        for strategy in ("S0", "S4"):
            for magic3 in (False, True):        # the production config, and 7b
                for where in ("window", "streaming"):
                    base, rig, log, t = self.late(strategy, magic3, where)
                    self.assertEqual(halts_after(rig.kernel.ops, t["stop"]), [])     # a stop: step 11 skipped
                    for name, T in self.positions(rig, t, magic3).items():
                        with self.subTest(strategy=strategy, magic3=magic3, where=where, at=name):
                            took, rig2, log2, t2 = self.late(strategy, magic3, where, T)
                            self.assertLessEqual(took, BUDGET_S)
                            self.assertLessEqual(took, base + 0.21, base)  # at most one stop-path transfer (200 ms)
                            self.assertEqual((halts_after(rig2.kernel.ops, t2["stop"]), tail(rig2.kernel.ops)[-3:],
                                              sum("clear_halt skipped" in x for x in log2.lines)), ([], TAIL, 1))
                            self.assertEqual(check_trace(rig2.kernel.ops), [])
                            worst = max(worst, took)
        self.assertLess(worst, 6.5, worst)      # measured 6.42 s (before: 7.21 s, S4 7.80 s, magic3 off 10.8 s)


class StopRules(_Ep0Case):
    def test_a_stop_skips_step_11_and_the_next_session_streams_again(self):
        """With a stop pending step 11 is skipped although EP0 answers, and a STALL after the stop leaves the endpoint
        halted (the reaper keeps it); the next session's SETINTERFACE (S0: si_before, S2: si_after) clears it."""
        for strategy in ("S0", "S2"):
            with self.subTest(strategy=strategy):
                rig, drv, log, s, n, ident = self.session(strategy)
                self.stream(rig, drv, s, n, ident)
                drv.abort.set()
                rig.kernel.note(stop=True)
                rig.kernel.force_status(-32, halt=True)
                while drv.stats["epipe"] < 1:
                    drv.pump(0.01)
                drv.teardown()
                self.assert_stop_skip(rig, log, drv)
                self.assertTrue(rig.vft.halted)
                rig.clock.advance(8.5)          # the settle before a re-activation (VftModel.settle_required_s)
                node = rig.node()
                drv2 = VftDriver(UsbDevice(rig.kernel, node.devpath), node, rig.clock, None, DriverConfig(),
                                 inline=True)
                ident2 = drv2.open()
                rig.kernel.note(strategy=strategy, n=xu_len_for(strategy, "auto", None, None, ident2.get_len),
                                set_config=False)
                self.stream(rig, drv2, s, n, ident2)
                self.assertEqual((rig.vft.halted, drv2.stats["epipe"]), (False, 0))
                self.assertGreater(drv2.stats["urbs_ok"], 50)
                drv2.teardown()
                self.assertEqual((rig.kernel.violations, check_trace(rig.kernel.ops)), ([], []))

    def test_a_transfer_of_the_stop_path_waits_at_most_teardown_ack_ms(self):
        for stop in (True, False):              # the control: an idle teardown waits ctrl_timeout_ms (1 s)
            with self.subTest(stop=stop):
                rig, drv, log, s, n, ident = self.session()
                self.stream(rig, drv, s, n, ident)
                rig.kernel.silence_ep0()
                if stop:
                    drv.abort.set()
                    rig.kernel.note(stop=True)
                t0 = rig.clock.monotonic()
                drv.teardown()                  # 30 ms, Block C's first MAGIC (its ETIMEDOUT), no step 11
                self.assertAlmostEqual(rig.clock.monotonic() - t0, 0.03 + (0.2 if stop else 1.0), delta=0.01)
                self.assertEqual((xu_sets(rig.kernel.ops, t0), skips(log)), (["MAGIC"], 1))

    def test_block_c_ends_at_the_first_command_that_finds_ep0_silent_once_a_stop_is_pending(self):
        """EP0 silent just after Block C's first MAGIC was acknowledged (S0, S4: STREAM_OFF and its GET_LEN are next),
        or between S4's SET_CUR and its GET_LEN: with a stop pending nothing more goes out; without one (the control)
        Block C goes on after the tolerated STREAM_OFF (IR off)."""
        for strategy, at, stop in (("S0", "done", True), ("S4", "done", True), ("S4", "set", True),
                                   ("S0", "done", False), ("S4", "done", False)):
            with self.subTest(strategy=strategy, at=at, stop=stop):
                rig, drv, log, s, n, ident = self.session(strategy)
                self.stream(rig, drv, s, n, ident)
                if stop:
                    drv.abort.set()
                    rig.kernel.note(stop=True)

                def hook(kind, phase):
                    if (kind, phase) == ("magic", at):
                        rig.vft.on_xu = None
                        rig.kernel.silence_ep0()
                rig.vft.on_xu = hook
                t0 = rig.clock.monotonic()
                drv.teardown()
                c = [o for o in rig.kernel.ops if o["kind"] == "ctrl" and o["t"] >= t0]
                if at == "set":                 # the MAGIC answered, its GET_LEN after timed out: no poll after it
                    self.assertEqual([(o["name"], o.get("err")) for o in c][-2:], [("MAGIC", None),
                                                                                  ("XU_GET_LEN", "ETIMEDOUT")])
                else:
                    self.assertEqual(xu_sets(c), ["MAGIC", "STREAM_OFF"] + ([] if stop else ["MAGIC"]))
                    self.assertEqual([o["err"] for o in c if o["name"] == "STREAM_OFF"], ["ETIMEDOUT"])
                self.assertLess(rig.clock.monotonic() - t0, (0.5 if stop else 4.5))
                self.assertEqual((tail(rig.kernel.ops)[-3:], skips(log), drv.teardown_path), (TAIL, 1, "clean"))
                self.assertEqual((rig.kernel.violations, check_trace(rig.kernel.ops)), ([], []))

    def test_a_halted_endpoint_and_a_silent_ep0_with_urbs_in_flight(self):
        """TS-6: the bulk endpoint STALLs and stays halted (no data until a CLEAR_HALT) while EP0 is silent, 16 URBs in
        flight: the reaper keeps the halt, nothing completes, and the teardown discards and reaps all of them."""
        rig, drv, log, s, n, ident = self.session()
        self.stream(rig, drv, s, n, ident)
        rig.kernel.silence_ep0()
        drv.abort.set()
        rig.kernel.note(stop=True)
        rig.kernel.force_status(-32, halt=True)
        while drv.stats["epipe"] < 1:
            drv.pump(0.01)
        ok = drv.stats["urbs_ok"]
        for _ in range(50):                     # 0.5 s: the halted endpoint completes nothing
            drv.pump(0.01)
        self.assertEqual((drv.stats["urbs_ok"], drv.inflight, halts_after(rig.kernel.ops, 0)), (ok, 15, []))
        self.assertTrue(log.has("W usb: epipe not recovered why=stopping"))
        t0 = rig.clock.monotonic()
        drv.teardown()
        self.assertLess(rig.clock.monotonic() - t0, 0.3)       # 30 ms, Block C's first MAGIC (200 ms)
        close = [o for o in rig.kernel.ops if o["kind"] == "close"][-1]
        self.assertEqual((tail(rig.kernel.ops), close["inflight"] + close["unreaped"], skips(log)), (TAIL, 0, 1))
        self.assertEqual((drv.teardown_path, rig.kernel.violations, check_trace(rig.kernel.ops)), ("clean", [], []))

    def test_the_probe_raw_queries_go_through_ctrl(self):
        """SP-4, TS-5: ActiveSteps._q's raw reads (A1, A3) track EP0 and, with a stop pending, send nothing to a silent
        EP0, like every other driver control transfer."""
        rig, drv, log, s, n, ident = self.session()
        drv.activate(s, 0xFF, 0xB2, n)
        rig.kernel.silence_ep0()
        setup = uvc.xu_setup(uvc.GET_INFO, 1, ident.xu_unit, 1)
        t0 = rig.clock.monotonic()
        self.assertTrue(ActiveSteps._q(drv, setup).startswith("error:"))
        self.assertEqual((round(rig.clock.monotonic() - t0, 3), drv.ep0_silent), (1.0, True))
        drv.abort.set()
        m, t0 = len(rig.kernel.ops), rig.clock.monotonic()
        self.assertTrue(ActiveSteps._q(drv, setup).startswith("error:"))
        self.assertEqual((rig.clock.monotonic() - t0, rig.kernel.ops[m:]), (0.0, []))


class Ep0Trace(_Ep0Case):
    """check_trace (§11.3 invariant 9) and the fake (§7.3) around a silent EP0."""

    def test_an_unanswered_trailing_magic_is_no_cadence_misalignment(self):
        """SP-3: EP0 silent once Block C's 11th OFF write is acknowledged (no stop): the 7b MAGIC never reached the
        device, so the IR left on is no misaligned cadence."""
        rig, drv, log, s, n, ident = self.session()
        self.stream(rig, drv, s, n, ident)
        seen = collections.Counter()

        def hook(kind, phase):
            seen[kind, phase] += 1
            if (kind, phase) == ("reg_write", "done") and drv._in_teardown and seen[kind, phase] == 11:
                rig.kernel.silence_ep0()
        rig.vft.on_xu = hook
        t0 = rig.clock.monotonic()
        drv.teardown()
        self.assertEqual(([(o["name"], o.get("err")) for o in rig.kernel.ops if o["kind"] == "ctrl" and
                           o["name"] in XU and o["t"] >= t0][-1], pwm(rig)), (("MAGIC", "ETIMEDOUT"), [0xFF] * 3))
        self.assertEqual((rig.kernel.violations, check_trace(rig.kernel.ops)), ([], []))

    def ended_early(self, at, count):
        """A stop at `at` with EP0 silent for `count` requests from then on (None: all): Block A's end, Block C."""
        rig, drv, log, s, n, ident = self.session()
        seen = collections.Counter()

        def stop():
            rig.kernel.silence_ep0(count=count)
            drv.abort.set()
            rig.kernel.note(stop=True)

        def hook(kind, phase):
            seen[kind, phase] += 1
            if (kind, phase, seen[kind, phase]) == at[:3]:
                rig.clock.call_later(at[3], stop)
        rig.vft.on_xu = hook
        with self.assertRaises(Aborted):
            drv.activate(s, 0xFF, 0xB2, n)
        rig.vft.on_xu = None
        rig.kernel.note(abort=True)
        drv.teardown()
        return rig, log, drv

    def test_block_a_sends_stream_on_after_magic_1_or_3_and_check_trace_requires_it(self):
        """TS-3: a stop with one request unanswered, MAGIC #3's SET_CUR (the stop in the 100 ms sleep before it) or
        MAGIC #1's poll: Block A's STREAM_ON still goes out, is answered, and Block C runs exactly (PWM 0)."""
        for at, block_a in ((("reg_write", "done", 11, 0.05), ["REG_WRITE", "MAGIC", "STREAM_ON"]),
                            (("magic", "set", 1, 0.0005), ["MAGIC", "STREAM_ON"])):
            with self.subTest(at=at[:3]):
                rig, log, drv = self.ended_early(at, 1)
                names = xu_sets(rig.kernel.ops)
                self.assertEqual((names[-15 - len(block_a):-15], names[-15:], pwm(rig)), (block_a, TD15, [0, 0, 0]))
                self.assert_stop_skip(rig, log, drv)
        rig, log, drv = self.ended_early(("magic", "set", 1, 0.0005), None)      # EP0 stays silent
        ops_ = rig.kernel.ops
        self.assertEqual([(o["name"], o.get("err")) for o in ops_ if o["kind"] == "ctrl" and o["name"] in XU],
                         [("MAGIC", None), ("STREAM_ON", "ETIMEDOUT"), ("MAGIC", "ETIMEDOUT")])
        self.assertEqual(check_trace(ops_), [])
        hole = [o for o in ops_ if not (o["kind"] == "ctrl" and o["name"] == "STREAM_ON")]    # the STREAM_ON missing
        self.assertEqual([p.split(") ", 1)[1] for p in check_trace(hole)],
                         ["Block C's MAGIC cadence misaligned: Block C starts at MAGIC count 1"])

    def test_the_step_11_rule_in_both_directions(self):
        """TS-4 and the stop rule: a missing CLEAR_HALT is accepted only while EP0 is silent or after a stop noted
        before Block C (a stop noted during Block C: either); a CLEAR_HALT to a silent EP0 or with a stop pending is
        reported."""
        def without(ops_):
            i = max(j for j, o in enumerate(ops_) if o["kind"] == "release" and o["iface"] == 1)
            return ops_[:i - 1] + ops_[i:] if ops_[i - 1]["kind"] == "clear_halt" else ops_

        def problems(ops_):
            return [p.split(") ", 1)[1] for p in check_trace(ops_, service=False)]
        rig, drv, log, s, n, ident = self.session()          # (C) a timeout, then every request STALLs: answered
        self.stream(rig, drv, s, n, ident)
        rig.kernel.silence_ep0(count=1)
        with self.assertRaises(XuError):
            drv.xu(uvc.reg_read(2, n), 200)
        rig.vft.control = mock.Mock(side_effect=OSError(errno.EPIPE, "STALL"))
        drv.teardown()
        self.assertEqual((problems(rig.kernel.ops), problems(without(rig.kernel.ops))),
                         ([], ["teardown tail %s" % TAIL]))
        rig, drv, log, s, n, ident = self.session()          # (F3) the reaper's CLEAR_HALT timed out after Block C
        self.stream(rig, drv, s, n, ident)
        t0 = rig.clock.monotonic()
        drv.teardown()
        ops_ = rig.kernel.ops
        k = max(i for i, o in enumerate(ops_) if o["kind"] == "ctrl" and o["name"] == "XU_GET_CUR")
        halt = {"t": ops_[k]["t"], "kind": "clear_halt", "fd": ops_[k]["fd"], "ep": 0x81, "err": "ETIMEDOUT"}
        f3 = ops_[:k + 1] + [halt] + ops_[k + 1:]
        self.assertEqual((problems(ops_), problems(f3), problems(without(f3))),
                         ([], ["CLEAR_HALT to a silent EP0 (an ETIMEDOUT and nothing answered since)"], []))
        c0 = next(i for i, o in enumerate(ops_) if o["kind"] == "ctrl" and o["name"] == "MAGIC" and o["t"] >= t0)
        late = ops_[:c0 + 1] + [{"t": ops_[c0]["t"], "kind": "note", "fd": None, "stop": True}] + ops_[c0 + 1:]
        early = ops_[:c0] + [{"t": ops_[c0]["t"], "kind": "note", "fd": None, "stop": True}] + ops_[c0:]
        self.assertEqual((problems(late), problems(without(late)), problems(without(early)), problems(early)),
                         ([], [], [], ["CLEAR_HALT with a stop pending"]))   # noted during Block C: either
        rig, drv, log, s, n, ident = self.session()          # (J) a stop before MAGIC #1: Block C, no activation
        drv.abort.set()
        rig.kernel.note(stop=True)
        with self.assertRaises(Aborted):
            drv.activate(s, 0xFF, 0xB2, n)
        drv.teardown()
        ops_ = rig.kernel.ops
        r1 = max(j for j, o in enumerate(ops_) if o["kind"] == "release" and o["iface"] == 1)
        halt = ops_[:r1] + [dict(ops_[r1], kind="clear_halt", ep=0x81)] + ops_[r1:]
        self.assertEqual((xu_sets(ops_), problems(ops_), problems(halt)),
                         (TD15, [], ["CLEAR_HALT with a stop pending"]))

    def test_errors_are_traced_by_their_linux_names(self):
        """TS-7: a device-model ETIMEDOUT is traced as ETIMEDOUT on Windows too (errno 10060 is WSAETIMEDOUT there), so
        check_trace's step-11 rule reads the trace as the driver read the errno."""
        rig, drv, log, s, n, ident = self.session()
        self.stream(rig, drv, s, n, ident)
        rig.vft.control = mock.Mock(side_effect=OSError(errno.ETIMEDOUT, "timed out"))
        drv.teardown()
        self.assertEqual([o.get("err") for o in rig.kernel.ops if o["kind"] == "ctrl"][-1], "ETIMEDOUT")
        self.assertEqual((skips(log), check_trace(rig.kernel.ops)), (1, []))

    def test_a_control_without_timeout_to_a_silent_ep0_is_a_violation(self):
        """TS-9: usbfs waits for ever on a CONTROL with timeout 0; the fake cannot, so it reports it."""
        rig, drv, log, s, n, ident = self.session()
        rig.kernel.silence_ep0()
        with self.assertRaises(OSError):
            drv.dev.control_in(uvc.xu_setup(uvc.GET_LEN, 2, ident.xu_unit), 0)
        self.assertEqual([v.split(":")[0] for v in rig.kernel.violations], ["ctrl_without_timeout"])


class StopReach(SvcCase):
    """The Service: a stop reaches the driver of a Block C already running (`_closing`), and the stop's own Block C runs
    with the stop rules even when run() ended on its stop Event before request_stop() (the CLI's signal thread sets the
    Event first)."""

    def test_a_stop_during_a_failure_teardown_reaches_its_driver(self):
        for strategy, at, n_ in (("S0", ("reg_write", "done"), 11), ("S4", ("magic", "done"), 1)):
            with self.subTest(strategy=strategy, at=at):
                rig, s = self.service(cfg=Config(token="", strategy=strategy))
                self.stream_ok(s)
                seen, t, ev = collections.Counter(), {}, threading.Event()

                def stop():
                    t["stop"] = rig.clock.monotonic()
                    rig.kernel.silence_ep0()
                    s.request_stop()

                def hook(kind, phase):          # the failure's Block C: its last OFF write, or its first MAGIC (S4)
                    if s.state == "TEARDOWN" and (kind, phase) == at and "stop" not in t:
                        seen[at] += 1
                        if seen[at] == n_:
                            rig.clock.call_later(0.0, stop)
                rig.vft.on_xu = hook
                self.addCleanup(setattr, rig.vft, "on_xu", None)
                s.act.request, s._next_tick = (F.NO_FRAMES, {}), s.clock.monotonic()
                guard = rig.clock.call_later(60.0, ev.set)
                s.run(ev)
                guard.cancel()
                self.assertLess(rig.clock.monotonic() - t["stop"], 0.5)     # before: 5.2 s (S4: 3.1 s)
                self.assertEqual((s.state, halts_after(rig.kernel.ops, t["stop"])), ("STOPPED", []))
                self.assertTrue(s.log.has("usb: clear_halt skipped why="))      # W ep0_timeout or I stopping
                self.check(rig, s)

    def test_a_run_ended_by_its_stop_event_still_skips_step_11(self):
        rig, s = self.service()
        self.stream_ok(s)
        ev = threading.Event()
        rig.clock.call_later(0.05, ev.set)      # run()'s stop Event, no request_stop()
        s.run(ev)
        self.assertEqual((s.state, s.drivers[-1].teardown_path, tail(rig.kernel.ops)[-4:-3]), ("STOPPED", "clean", []))
        self.assertTrue(s.log.has(STOPPING))
        self.check(rig, s)


if __name__ == "__main__":
    unittest.main()
