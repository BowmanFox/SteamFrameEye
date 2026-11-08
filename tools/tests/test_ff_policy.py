# SPDX-License-Identifier: MIT
"""Pure recovery and idle decisions (spec §4.7, §4.13, §4.14, §4.18; §11.3 test_ff_policy, task T1)."""
import ffutil  # first: installs the port guard

import math
import unittest

ffutil.setup_path()
from frameface import policy as P  # noqa: E402
from frameface.config import Config  # noqa: E402
from frameface.policy import Failure as F  # noqa: E402

ALL = ("S0", "S1", "S2", "S3", "S4", "S5")
XU = (F.XU_TIMEOUT, F.XU_ERROR)
NO_IR_HINT = ("Every start sequence gave a black image. Unplug the tracker for 10 s and plug it back in; if it "
              "stays black run install-frameface.ps1 -ProbeActive and report the bundle")


def ctx(**kw):
    base = dict(strategy="S0", tried={"S0"}, consecutive_xu=0, good_stream_s=0.0, soft_reopens_in_row=0,
                direct_port=True, allow_reset=True, last_reset_age_s=math.inf, reset_allowance=0, ladder_restarts=0)
    base.update(kw)
    return P.FailureContext(**base)


class Strategies(unittest.TestCase):
    def test_table(self):
        rows = {name: (s.set_config, s.si_before, s.xu_between, s.si_mid, s.si_after, s.rhythm, s.xu_len,
                       s.stream_ack_poll) for name, s in P.STRATEGIES.items()}
        self.assertEqual(rows, {
            "S0": (False, True, False, False, True, False, None, True),
            "S1": (False, True, False, False, False, False, None, True),
            "S2": (False, False, False, False, True, False, None, True),
            "S3": (False, True, True, False, True, False, None, True),
            "S4": (False, True, False, False, True, True, None, True),
            "S5": (True, False, False, True, False, False, 384, False)})
        self.assertEqual([s.name for s in P.STRATEGIES.values()], list(ALL))

    def test_ladder_order_and_dedup(self):
        self.assertEqual(P.ladder(None, None), list(ALL))
        self.assertEqual(P.ladder(None, "auto"), list(ALL))
        self.assertEqual(P.ladder("S0", None), list(ALL))
        self.assertEqual(P.ladder("S2", None), ["S2", "S0", "S1", "S3", "S4", "S5"])
        self.assertEqual(P.ladder("S2", "auto"), ["S2", "S0", "S1", "S3", "S4", "S5"])
        self.assertEqual(P.ladder("S5", None), ["S5", "S0", "S1", "S2", "S3", "S4"])     # known-good S5 first
        for known in ALL[:5]:
            self.assertEqual(P.ladder(known, None)[-1], "S5", "S5 stays the last rung")
            self.assertEqual(sorted(P.ladder(known, None)), list(ALL))
        self.assertEqual(P.ladder(None, "S3"), ["S3"])
        self.assertEqual(P.ladder("S1", "S5"), ["S5"])                                    # a pin wins
        self.assertEqual(P.ladder("S9", None), list(ALL))

    def test_xu_len_precedence(self):
        # config number > known-good pair > the strategy's own length (S5: 384) > GET_LEN
        self.assertEqual(P.xu_len_for("S5", 64, "S5", 384, 64), 64)
        self.assertEqual(P.xu_len_for("S0", 384, None, None, 64), 384)
        self.assertEqual(P.xu_len_for("S2", "384", "S0", 64, 64), 384)
        self.assertEqual(P.xu_len_for("S5", "auto", "S5", 64, 64), 64)         # the pair beats S5's 384
        self.assertEqual(P.xu_len_for("S0", "auto", "S0", 384, 64), 384)       # probe-proven 384 on S0
        self.assertEqual(P.xu_len_for("S1", "auto", "S0", 384, 64), 64)        # the pair is for its rung only
        self.assertEqual(P.xu_len_for("S5", "auto", "S0", 384, 64), 384)
        self.assertEqual(P.xu_len_for("S5", "auto", None, None, 64), 384)
        self.assertEqual(P.xu_len_for("S0", "auto", "S0", None, 64), 64)
        self.assertEqual(P.xu_len_for("S3", "auto", None, None, 128), 128)     # an odd GET_LEN is used as read
        self.assertEqual(P.xu_len_for("S4", "auto", None, None, 384), 384)


class OnFailureRows(unittest.TestCase):
    def decide(self, f, **kw):
        return P.on_failure(f, ctx(**kw))

    def expect(self, d, action, row, **fields):
        self.assertEqual((d.action, d.row), (action, row), d)
        for name, value in fields.items():
            self.assertEqual(getattr(d, name), value, name)

    def test_row_1_lost(self):
        self.expect(self.decide(F.LOST), "wait_device", 1)
        self.expect(self.decide(F.LOST, reset_allowance=1, seen_ok=True, ir_trial_possible=True), "wait_device", 1)

    def test_row_2_permission_busy(self):
        self.expect(self.decide(F.PERMISSION), "fault", 2, fault="permission", delay_s=30.0)
        self.expect(self.decide(F.BUSY, reset_allowance=1), "fault", 2, fault="busy", delay_s=30.0,
                    hint="Stop the other program (vftstream, Babble Frame Runtime, vive-face-init)")

    def test_row_3_unsupported_library(self):
        self.expect(self.decide(F.UNSUPPORTED), "fault", 3, fault="unsupported", delay_s=60.0)
        d = self.decide(F.LIBRARY)
        self.expect(d, "fault", 3, fault="library", hint="Update SteamOS, then run install.sh again")
        self.assertEqual(d.delay_s, math.inf, "library: no retry")

    def test_row_4_dark_after_ok_frames_is_an_ir_trial(self):
        self.expect(self.decide(F.DARK, strategy="S2", tried={"S0", "S1", "S2"}, seen_ok=True, ir_trial_possible=True),
                    "ir_trial", 4, strategy="S2")
        self.expect(self.decide(F.DARK, brownout_pending=True, ir_trial_possible=True, reset_allowance=1),
                    "ir_trial", 4, strategy="S0")

    def test_row_5_suppress(self):
        self.expect(self.decide(F.DARK, seen_ok=True), "suppress", 5, strategy="", fault="")
        self.expect(self.decide(F.DARK, brownout_pending=True, reset_allowance=1), "suppress", 5)

    def test_row_6_reset_allowance(self):
        for f in (F.XU_TIMEOUT, F.XU_ERROR, F.NO_DATA, F.NO_FRAMES, F.TEST_BARS, F.DARK):
            with self.subTest(f=f):
                self.expect(self.decide(f, strategy="S1", tried={"S0", "S1"}, consecutive_xu=1, reset_allowance=1),
                            "reset_port", 6, strategy="S1")                 # ladder unchanged
        self.assertEqual(self.decide(F.STALL, reset_allowance=1).row, 10)
        self.assertEqual(self.decide(F.PROBE_REJECT, reset_allowance=1).row, 12)
        self.assertEqual(self.decide(F.NO_FRAMES, reset_allowance=1, direct_port=False).row, 12)     # hub
        self.assertEqual(self.decide(F.NO_FRAMES, reset_allowance=1, allow_reset=False).row, 12)
        for f, row in ((F.XU_TIMEOUT, 7), (F.NO_DATA, 12), (F.TEST_BARS, 12), (F.DARK, 12)):   # §4.12: no reset
            self.assertEqual(self.decide(f, consecutive_xu=1, reset_allowance=1, last_reset_age_s=5.0).row, row, f)
        self.assertEqual(self.decide(F.NO_DATA, reset_allowance=1, last_reset_age_s=600.0).row, 6)  # within 600 s

    def test_row_7_xu_retry_with_doubled_settle(self):
        for f in XU:
            self.expect(self.decide(f, consecutive_xu=1), "retry_same", 7, settle_mult=2.0, strategy="S0")

    def test_row_8_prefers_s4(self):
        self.expect(self.decide(F.XU_TIMEOUT, consecutive_xu=2), "next_strategy", 8, strategy="S4")
        self.expect(self.decide(F.XU_ERROR, strategy="S2", tried={"S0", "S1", "S2"}, consecutive_xu=2),
                    "next_strategy", 8, strategy="S4")
        self.expect(self.decide(F.XU_ERROR, strategy="S4", tried={"S0", "S4"}, consecutive_xu=2),
                    "next_strategy", 8, strategy="S1")                       # S4 tried: the next untried rung
        self.expect(self.decide(F.XU_TIMEOUT, strategy="S5", tried=set(ALL), consecutive_xu=2),
                    "reset_port", 13, strategy="S0")                         # nothing left: escalate

    def test_row_9_escalates_to_row_13(self):
        self.expect(self.decide(F.XU_TIMEOUT, consecutive_xu=3), "reset_port", 13, strategy="S0")
        self.expect(self.decide(F.XU_ERROR, consecutive_xu=5, ladder_restarts=1), "fault", 15, fault="wedged")

    def test_row_10_soft_reopen(self):
        for f in (F.STALL, F.FROZEN, F.EPIPE_STORM, F.XFER_STORM):
            for n in range(3):
                self.expect(self.decide(f, soft_reopens_in_row=n), "retry_same", 10, strategy="S0")

    def test_row_11_long_healthy_stream_does_not_move_the_ladder(self):
        for f in (F.NO_DATA, F.NO_FRAMES, F.TEST_BARS, F.PROBE_REJECT, F.DARK):
            self.expect(self.decide(f, good_stream_s=60.0), "retry_same", 11, strategy="S0")
        self.expect(self.decide(F.STALL, soft_reopens_in_row=3, good_stream_s=61.0), "retry_same", 11)
        self.assertEqual(self.decide(F.NO_FRAMES, good_stream_s=59.9).row, 12)

    def test_row_12_next_strategy(self):
        for f in (F.NO_DATA, F.NO_FRAMES, F.TEST_BARS, F.PROBE_REJECT):
            self.expect(self.decide(f), "next_strategy", 12, strategy="S1", hint="")
        self.expect(self.decide(F.DARK), "next_strategy", 12, strategy="S1",
                    hint="the tracker's IR light did not switch on; trying S1")
        self.expect(self.decide(F.FROZEN, soft_reopens_in_row=3), "next_strategy", 12, strategy="S1")
        self.expect(self.decide(F.NO_FRAMES, strategy="S3", tried={"S2", "S0", "S1", "S3"},
                                ladder=("S2", "S0", "S1", "S3", "S4", "S5")), "next_strategy", 12, strategy="S4")

    def test_row_13_reset_and_restart(self):
        self.expect(self.decide(F.NO_FRAMES, strategy="S5", tried=set(ALL), last_reset_age_s=600.0),
                    "reset_port", 13, strategy="S0")
        self.expect(self.decide(F.TEST_BARS, strategy="S5", tried=set(ALL), ladder=P.ladder("S3", None)),
                    "reset_port", 13, strategy="S3")                         # restarts at rung 0 of this ladder
        self.assertEqual(self.decide(F.NO_FRAMES, tried=set(ALL), last_reset_age_s=599.0).row, 15)
        self.assertEqual(self.decide(F.NO_FRAMES, tried=set(ALL), ladder_restarts=1).row, 15)

    def test_row_14_dark_start_ir_trial(self):
        self.expect(self.decide(F.DARK, strategy="S5", tried=set(ALL), ladder_restarts=1, ir_trial_possible=True),
                    "ir_trial", 14, strategy="S0")
        self.expect(self.decide(F.DARK, strategy="S0", tried={"S0"}, ladder=("S0",), ladder_restarts=1,
                                ir_trial_possible=True), "ir_trial", 14, strategy="S0")

    def test_row_15_fault(self):
        self.expect(self.decide(F.DARK, tried=set(ALL), ladder_restarts=1), "fault", 15, fault="no_ir",
                    delay_s=600.0, hint=NO_IR_HINT)
        self.expect(self.decide(F.NO_FRAMES, tried=set(ALL), ladder_restarts=1), "fault", 15, fault="wedged",
                    delay_s=60.0, hint="Unplug the tracker for 10 s and plug it back in")
        self.expect(self.decide(F.STALL, soft_reopens_in_row=3, tried=set(ALL), direct_port=False), "fault", 15,
                    fault="wedged")                                          # hub: never a reset
        self.expect(self.decide(F.PROBE_REJECT, tried=set(ALL), allow_reset=False), "fault", 15, fault="wedged")

    def test_pinned_ladder_has_no_other_rungs(self):
        self.expect(self.decide(F.NO_FRAMES, strategy="S3", tried={"S3"}, ladder=("S3",)), "reset_port", 13,
                    strategy="S3")
        self.expect(self.decide(F.XU_TIMEOUT, strategy="S3", tried={"S3"}, ladder=("S3",), consecutive_xu=2),
                    "reset_port", 13, strategy="S3")

    def test_failure_values(self):
        self.assertEqual([f.value for f in F], [
            "xu_timeout", "xu_error", "probe_reject", "no_data", "no_frames", "test_bars", "dark", "stall", "frozen",
            "epipe_storm", "xfer_storm", "lost", "permission", "busy", "unsupported", "library"])
        self.assertEqual(P.on_failure("lost", ctx()).action, "wait_device")
        self.assertEqual((str(F.DARK), "%s" % F.XU_TIMEOUT, "{}".format(F.LOST)), ("dark", "xu_timeout", "lost"))
        self.assertEqual(P.FAULT_RETRY_S, {"permission": 30.0, "busy": 30.0, "unsupported": 60.0,
                                           "library": math.inf, "wedged": 60.0, "no_ir": 600.0, "port": 30.0})


def route(failure, start="S0", **kw):
    """Apply decisions the way the service does (§4.13 flows) until a fault; [(action, strategy|fault)]."""
    state = dict(strategy=start, tried={start}, ladder_restarts=0, last_reset_age_s=math.inf, consecutive_xu=0)
    state.update(kw)
    steps = []
    for _ in range(64):
        if failure in XU:
            state["consecutive_xu"] += 1
        d = P.on_failure(failure, ctx(**state))
        steps.append((d.action, d.fault or d.strategy))
        if d.action == "fault":
            return steps
        if d.action == "next_strategy":
            state["tried"] = state["tried"] | {d.strategy}
            state["strategy"] = d.strategy
        elif d.action == "reset_port":
            if d.row == 13:
                state.update(tried={d.strategy}, ladder_restarts=1)
            else:
                state["reset_allowance"] -= 1
            state.update(strategy=d.strategy, last_reset_age_s=0.0)
        elif d.action == "ir_trial":                                         # the trial stays dark
            state.update(strategy=d.strategy, ir_trial_possible=False)
    raise AssertionError("no fault after 64 decisions: %r" % steps)


class RequiredDarkCases(unittest.TestCase):
    def test_seen_ok_with_trial_possible(self):
        self.assertEqual(P.on_failure(F.DARK, ctx(seen_ok=True, ir_trial_possible=True)).action, "ir_trial")

    def test_dark_start_moves_the_ladder(self):
        d = P.on_failure(F.DARK, ctx(seen_ok=False, brownout_pending=False, tried={"S0"}))
        self.assertEqual((d.action, d.strategy), ("next_strategy", "S1"))

    def test_brownout_pending_with_trial_possible(self):
        self.assertEqual(P.on_failure(F.DARK, ctx(brownout_pending=True, ir_trial_possible=True)).action, "ir_trial")

    def test_seen_ok_without_trial(self):
        self.assertEqual(P.on_failure(F.DARK, ctx(seen_ok=True, ir_trial_possible=False)).action, "suppress")

    def test_dark_start_route(self):
        rungs = [("next_strategy", s) for s in ALL[1:]]
        self.assertEqual(route(F.DARK, ir_trial_possible=True),
                         rungs + [("reset_port", "S0")] + rungs + [("ir_trial", "S0"), ("fault", "no_ir")])


class Routes(unittest.TestCase):
    def test_ladder_exhausted_reset_restart_once_fault(self):
        rungs = [("next_strategy", s) for s in ALL[1:]]
        for f in (F.NO_FRAMES, F.NO_DATA, F.TEST_BARS, F.PROBE_REJECT):
            self.assertEqual(route(f), rungs + [("reset_port", "S0")] + rungs + [("fault", "wedged")], f)

    def test_hub_never_resets(self):
        rungs = [("next_strategy", s) for s in ALL[1:]]
        self.assertEqual(route(F.NO_FRAMES, direct_port=False, reset_allowance=1), rungs + [("fault", "wedged")])
        self.assertEqual(route(F.DARK, direct_port=False, ir_trial_possible=True),
                         rungs + [("ir_trial", "S0"), ("fault", "no_ir")])

    def test_dirty_start_allowance_then_ladder(self):
        # row 6 uses the dirty-start allowance on the same rung; the ladder pass that follows ends within
        # 600 s of that reset, so row 13 may not reset again and the pass ends in a fault
        rungs = [("next_strategy", s) for s in ALL[1:]]
        self.assertEqual(route(F.NO_DATA, reset_allowance=1), [("reset_port", "S0")] + rungs + [("fault", "wedged")])

    def test_xu_route(self):
        self.assertEqual(route(F.XU_TIMEOUT), [("retry_same", "S0"), ("next_strategy", "S4"), ("reset_port", "S0"),
                                               ("fault", "wedged")])


class BackoffAndSettle(unittest.TestCase):
    def test_backoff_sequence(self):
        self.assertEqual([P.backoff_s(n) for n in range(1, 13)], [1, 1.5, 2, 3, 4, 5, 10, 30, 60, 60, 60, 60])
        self.assertEqual(P.backoff_s(0), 1.0)

    BASE = dict(same_boot=True, enum_bt=100.0, start_bt=50.0, dirty_start=False, last_teardown_bt=None,
                last_reset_bt=None, absent_since_teardown=False, settle_s=8.0, enum_settle_s=2.0, settle_mult=1.0,
                backoff_until_bt=0.0)

    def sd(self, **kw):
        return P.settle_deadline(200.0, **dict(self.BASE, **kw))

    def test_enumeration_and_backoff(self):
        self.assertEqual(self.sd(), 102.0)
        self.assertEqual(self.sd(backoff_until_bt=150.0), 150.0)

    def test_fresh_boot(self):
        self.assertEqual(self.sd(same_boot=False, last_teardown_bt=190.0, last_reset_bt=195.0, dirty_start=True,
                                 start_bt=99.0), 102.0)

    def test_same_boot_teardown_and_mult_2(self):
        self.assertEqual(self.sd(last_teardown_bt=190.0), 198.0)
        self.assertEqual(self.sd(last_teardown_bt=190.0, settle_mult=2.0), 206.0)

    def test_absence_skips_the_teardown_settle(self):
        self.assertEqual(self.sd(last_teardown_bt=190.0, absent_since_teardown=True), 102.0)

    def test_reset_is_never_skipped(self):
        self.assertEqual(self.sd(last_reset_bt=195.0, last_teardown_bt=190.0, absent_since_teardown=True), 203.0)
        self.assertEqual(self.sd(last_reset_bt=195.0), 203.0)

    def test_dirty_start(self):
        self.assertEqual(self.sd(dirty_start=True, start_bt=99.0), 107.0)
        self.assertEqual(self.sd(dirty_start=True, start_bt=50.0), 102.0)

    def test_absence_tracker(self):
        t = P.AbsenceTracker()
        for now, present in ((101, True), (102, False), (103, True)):        # one missing scan: no absence
            t.scan(now, present)
        self.assertFalse(t.absent_since(100.0, None))
        t.scan(104, False)
        t.scan(105, False)
        self.assertTrue(t.absent_since(100.0, None))
        self.assertFalse(t.absent_since(104.5, None), "an absence before the teardown does not count")
        t.scan(106, True)
        for now in (201, 202, 203):                                          # our reset at 200 re-enumerates
            t.scan(now, False)
        t.scan(204, True)
        self.assertFalse(t.absent_since(150.0, 200.0), "an absence within 30 s after a reset does not skip")
        teardown_term = self.sd(last_teardown_bt=150.0, last_reset_bt=None,
                                absent_since_teardown=t.absent_since(150.0, 200.0))
        self.assertEqual(teardown_term, 158.0)
        t.scan(240, False)
        t.scan(241, False)                                                   # an unplug 40 s after the reset
        self.assertTrue(t.absent_since(150.0, 200.0))
        t = P.AbsenceTracker()          # the scan saw the unplug a moment before the LOST path ran (at 300.5)
        for now in (300, 301, 302):
            t.scan(now, False)
        self.assertEqual((t.absent_since(300.5, None), t.absent_since(301.5, None)), (True, False))


def inputs(streaming=False, clients=1, worn=True, **kw):
    now = 1000.0
    base = dict(enabled=True, present=True, battery_low=False, worn=worn, clients=clients, streaming=streaming,
                now=now, last_client_t=now if clients else now - 100.0, unworn_since=now - 5.0 if worn is False
                else None, settle_ok=True)
    base.update(kw)
    return P.RunInputs(**base)


class RunDecisionTable(unittest.TestCase):
    def check(self, i, usb, publish, why, **cfg):
        self.assertEqual(P.run_decision(i, Config(**cfg)), P.RunDecision(usb, publish, why))

    def test_rows(self):
        now = 1000.0
        self.check(inputs(clients=0), "idle", False, "no_clients")                                   # 1
        self.check(inputs(), "start", False, "")                                                     # 2
        self.check(inputs(settle_ok=False), "idle", False, "settling")                               # 3
        self.check(inputs(worn=False), "idle", False, "not_worn")                                    # 4
        self.check(inputs(worn=None), "start", False, "")                                            # 5
        self.check(inputs(streaming=True), "keep", True, "")                                         # 6
        self.check(inputs(streaming=True, worn=False, unworn_since=now - 10), "keep", False, "not_worn")  # 7
        self.check(inputs(streaming=True, worn=False, unworn_since=now - 60), "stop", False, "not_worn")  # 8
        self.check(inputs(streaming=True, clients=0, last_client_t=now - 10), "keep", False, "")     # 9
        self.check(inputs(streaming=True, clients=0, last_client_t=now - 31), "stop", False, "no_clients")  # 10
        self.check(inputs(streaming=True, worn=False), "keep", True, "", gate_on_worn=False)         # 11
        self.check(inputs(clients=0, worn=False), "start", False, "not_worn", power_policy="always")  # 12
        self.check(inputs(streaming=True, clients=0, last_client_t=None), "keep", False, "no_clients",
                   power_policy="always")                                                            # 13
        self.check(inputs(streaming=True, clients=0, last_client_t=-math.inf), "keep", False, "no_clients",
                   power_policy="always")
        for streaming, usb in ((True, "stop"), (False, "idle")):
            self.check(inputs(streaming=streaming, enabled=False), usb, False, "disabled")           # 14
            self.check(inputs(streaming=streaming, battery_low=True), usb, False, "battery")         # 15
        self.check(inputs(present=False), "idle", False, "no_device")                                # 16

    def simulate(self, connected, gone, seconds=300.0, dt=0.1):
        cfg, streaming, last_client_t = Config(), False, None
        starts = stops = 0
        publish_ok = True
        for k in range(int(seconds / dt)):
            now = k * dt
            clients = 1 if (now % (connected + gone)) < connected else 0
            if clients:
                last_client_t = now
            d = P.run_decision(P.RunInputs(True, True, False, True, clients, streaming, now, last_client_t, None,
                                           True), cfg)
            publish_ok &= d.publish == (streaming and clients > 0)
            if d.usb == "start":
                starts += 1
                streaming = True
            elif d.usb == "stop":
                stops += 1
                streaming = False
        return starts, stops, publish_ok

    def test_reconnect_every_7_s_for_5_minutes_never_stops(self):
        self.assertEqual(self.simulate(connected=5.0, gone=2.0), (1, 0, True))
        self.assertEqual(self.simulate(connected=1.0, gone=6.0), (1, 0, True))
        self.assertEqual(self.simulate(connected=0.2, gone=6.8), (1, 0, True))

    def test_a_client_that_leaves_stops_the_stream(self):
        starts, stops, _ = self.simulate(connected=10.0, gone=290.0)        # gone after 10 s: stop at 40 s
        self.assertEqual((starts, stops), (1, 1))


class BrownoutAndIr(unittest.TestCase):
    def test_brownout_window_and_pending(self):
        b = P.BrownoutTracker()
        self.assertFalse(b.pending(0.0))
        self.assertFalse(b.suspect(100.0, "lost_after_activation"))
        self.assertTrue(b.pending(100.0) and b.pending(700.0))
        self.assertFalse(b.pending(700.5))
        self.assertTrue(b.suspect(650.0, "xfer_storm"))                       # second within 600 s
        # brownout_pending = >= 1 suspect within the last 600 s (§4.13), also right after the two
        # that lowered the level; only the count towards the next "two within 600 s" starts over
        self.assertTrue(b.pending(650.0) and b.pending(1250.0))
        self.assertFalse(b.pending(1250.5))
        self.assertFalse(b.suspect(1000.0, "xfer_storm"), "the count started over at 650 s")
        self.assertTrue(b.suspect(1100.0, "lost_after_activation"))
        self.assertFalse(b.suspect(2000.0, "xfer_storm"))
        self.assertFalse(b.suspect(2601.0, "lost_after_activation"))          # 601 s apart
        self.assertTrue(b.pending(2601.0))
        self.assertTrue(b.suspect(3000.0, "xfer_storm"))
        self.assertEqual((b.total, b.last_reason), (7, "xfer_storm"))

    def test_ir_levels(self):
        self.assertEqual(P.IR_LEVELS, {"L0": 0xB2, "L1": 0x96, "L2": 0x78, "L3": 0x5A})
        self.assertEqual([P.ir_next(level) for level in ("L0", "L1", "L2", "L3", "L9")], ["L1", "L2", "L3", None, None])


class Bucket(unittest.TestCase):
    def offered(self, bucket, size, fps=60, seconds=200):
        sent = 0
        for i in range(fps * seconds):
            if bucket.allow(i / fps, size):
                sent += size
        return sent / seconds

    def test_long_run_rate_within_2_percent(self):
        rate = 5000 * 125                                                     # stream_max_kbps 5000
        for size in (11000, 12000, 20000, 30000, 50000):                     # offered 660 KB/s .. 3 MB/s
            with self.subTest(size=size):
                measured = self.offered(P.TokenBucket(rate, 65536), size)
                self.assertLess(abs(measured - rate) / rate, 0.02, measured)
        self.assertEqual(self.offered(P.TokenBucket(rate, 65536), 8000), 8000 * 60)   # below the cap: all pass

    def test_burst_and_large_items(self):
        b = P.TokenBucket(1000, 65536)
        self.assertTrue(b.allow(0.0, 65536))
        self.assertFalse(b.allow(0.0, 1))
        self.assertFalse(b.allow(65.0, 65536))
        self.assertTrue(b.allow(65.536, 65536))
        self.assertFalse(b.allow(60.0, 1), "time going backwards adds no tokens")
        for size in (60000, 100000):                                         # near or above the burst: never above
            sent = self.offered(P.TokenBucket(625000, 65536), size)           # the cap (the cap may cost tokens)
            self.assertGreater(sent, 625000 * 0.85)
            self.assertLessEqual(sent, 625000 + 65536 / 200)
        with self.assertRaises(ValueError):
            P.TokenBucket(0, 1)


class Cpus(unittest.TestCase):
    def test_parse(self):
        self.assertIsNone(P.parse_cpus("auto"))
        self.assertEqual(P.parse_cpus("0-1,4"), {0, 1, 4})
        self.assertEqual(P.parse_cpus("2-3"), {2, 3})
        self.assertEqual(P.parse_cpus("7"), {7})
        self.assertEqual(P.parse_cpus(" 0-1 , 6 "), {0, 1, 6})
        for bad in ("", "a", "1-", "-1", "3-1", "1,,2", "1.5", "0-5000", "+1", None, 5, ["0"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                P.parse_cpus(bad)


STATES = ("INIT", "NO_DEVICE", "IDLE", "SETTLING", "OPENING", "ACTIVATING", "NEGOTIATING", "STARTING", "STREAMING",
          "TEARDOWN", "LOST", "BACKOFF", "RESETTING", "FAULT", "DISABLED")


SPEC_DISPLAY = {   # §4.18, written out from the spec (`<x>` placeholders without their backticks)
    "disabled": ("info", "Face tracking is off",
                 "Run `python3 -I ~/frameface/frameface_main.py on` on the headset"),
    "no_device": ("info", "Tracker not plugged in",
                  "Plug the VIVE Facial Tracker straight into the Frame's USB-C port (no hub, no charger)"),
    "port_device_mode": ("warn", "USB-C port is in charging/PC mode", "Unplug the charger or PC cable, then plug in "
                         "the tracker; it cannot run while the headset charges"),
    "no_permission": ("error", "No permission to use the tracker",
                      'See "udev fallback" in headset/frameface/README.md (one sudo step)'),
    "busy": ("error", "Tracker used by another program", "Stop <owner> (vftstream, Babble Frame Runtime, vive-face-init)"),
    "standby": ("info", "Paused: headset not worn", "Starts again when you put the headset on. \"Worn\" means the eye "
                "tracker's sample counter is moving; if you wear it and eye tracking is off, set "
                "`\"gate_on_worn\": false` in `~/.config/frameface/config.json`"),
    "waiting_client": ("info", "Ready, waiting for Baballonia",
                       'Face Camera Address <url>, "This is a..." = Wireless/IP Camera'),
    "settling": ("info", "Tracker settling (<n> s)", ""),
    "starting": ("info", "Starting the tracker", ""),
    "streaming": ("ok", "Streaming <f> fps to <n> client(s)", ""),
    "recovering": ("warn", "Tracker stalled, restarting", ""),
    "test_pattern": ("warn", "Tracker shows test bars, trying <S>", "If it persists, unplug the tracker for 10 s"),
    "dark_start": ("warn", "Tracker image black, trying <S>", "The IR light did not switch on with the last start "
                   "sequence; FrameFace tries the next one by itself"),
    "dark": ("warn", "Tracker image is black", "It was bright before: is the tracker facing your mouth, or is the lens "
             "covered? If it persists, unplug it for 10 s"),
    "brownout": ("warn", "Tracker lost power, IR lowered to <L>", "Use a direct C-to-C connection; if it keeps "
                 "happening unplug it for 5-10 min or reboot the headset"),
    "wedged": ("error", "Tracker does not respond", "Unplug the tracker for 10 s and plug it back in"),
    "no_ir": ("error", "Tracker IR light did not switch on", NO_IR_HINT),
    "port_in_use": ("error", "Face stream port <p> is in use", "Stop <owner> or set another `http_port` in "
                    "`~/.config/frameface/config.json`, then `python3 -I ~/frameface/frameface_main.py restart`; "
                    "Baballonia's address changes with it"),
    "unsupported": ("error", "Unsupported tracker or system", "Run install-frameface.ps1 -Probe and report the bundle"),
    "library": ("error", "libturbojpeg is missing", "Update SteamOS, then run install.sh again"),
}


class Display(unittest.TestCase):
    def test_every_row_is_the_spec_text(self):
        self.assertEqual(P.DISPLAY, SPEC_DISPLAY)

    def test_table(self):
        self.assertEqual({name: row[0] for name, row in P.DISPLAY.items()}, {
            "disabled": "info", "no_device": "info", "port_device_mode": "warn", "no_permission": "error",
            "busy": "error", "standby": "info", "waiting_client": "info", "settling": "info", "starting": "info",
            "streaming": "ok", "recovering": "warn", "test_pattern": "warn", "dark_start": "warn", "dark": "warn",
            "brownout": "warn", "wedged": "error", "no_ir": "error", "port_in_use": "error", "unsupported": "error",
            "library": "error"})
        worst = dict(fps=60, clients=4, settle_left_s=30, next_rung="S5", ir_level="L3", port=65535)
        for name, (severity, message, hint) in P.DISPLAY.items():
            filled = P._fill(name, message, worst)
            self.assertLessEqual(len(filled), 48, filled)
            self.assertNotIn("<", filled + P._fill(name, hint, {}))

    def test_every_machine_state(self):
        expected = {"INIT": "starting", "NO_DEVICE": "no_device", "IDLE": "waiting_client", "SETTLING": "settling",
                    "OPENING": "starting", "ACTIVATING": "starting", "NEGOTIATING": "starting", "STARTING": "starting",
                    "STREAMING": "streaming", "TEARDOWN": "waiting_client", "LOST": "no_device", "BACKOFF": "settling",
                    "RESETTING": "recovering", "FAULT": "wedged", "DISABLED": "disabled"}
        for state in STATES:
            name, severity, message, hint = P.display(state, {})
            self.assertEqual(name, expected[state], state)
            self.assertEqual(severity, P.DISPLAY[name][0])
            self.assertLessEqual(len(message), 48)
            self.assertEqual(P.display(state.lower(), {})[0], name)

    def test_no_device_idle_and_settling(self):
        self.assertEqual(P.display("NO_DEVICE", {"data_role": "device"})[0:3],
                         ("port_device_mode", "warn", "USB-C port is in charging/PC mode"))
        name, _, message, hint = P.display("IDLE", {"why": "not_worn"})
        self.assertEqual((name, message), ("standby", "Paused: headset not worn"))
        self.assertIn('`"gate_on_worn": false`', hint)
        name, _, _, hint = P.display("IDLE", {"why": "no_clients", "url": "http://10.35.78.1:9023/stream/<token>"})
        self.assertEqual(hint, 'Face Camera Address http://10.35.78.1:9023/stream/<token>, "This is a..." = '
                               'Wireless/IP Camera')
        self.assertEqual(P.display("IDLE", {"why": "no_clients"})[3], 'Face Camera Address from `python3 -I '
                         '~/frameface/frameface_main.py url`, "This is a..." = Wireless/IP Camera')     # no URL known
        self.assertEqual(P.display("SETTLING", {"settle_left_s": 3.2})[2], "Tracker settling (4 s)")
        self.assertEqual(P.display("BACKOFF", {"last_failure": "stall"})[0:3],
                         ("recovering", "warn", "Tracker stalled, restarting"))

    def test_test_pattern_and_dark_start(self):
        for state in ("SETTLING", "BACKOFF", "OPENING", "ACTIVATING", "STARTING"):
            self.assertEqual(P.display(state, {"last_failure": "test_bars", "next_rung": "S1"})[0:4],
                             ("test_pattern", "warn", "Tracker shows test bars, trying S1",
                              "If it persists, unplug the tracker for 10 s"))
            self.assertEqual(P.display(state, {"last_failure": "dark_start", "next_rung": "S2"}),
                             ("dark_start", "warn", "Tracker image black, trying S2",
                              "The IR light did not switch on with the last start sequence; FrameFace tries the next "
                              "one by itself"))
        self.assertEqual(P.display("OPENING", {"last_failure": "stall"})[0], "starting")

    def test_streaming(self):
        self.assertEqual(P.display("STREAMING", {"fps": 59.6, "clients": 1}),
                         ("streaming", "ok", "Streaming 60 fps to 1 client", ""))
        self.assertEqual(P.display("STREAMING", {"fps": 27.2, "clients": 2, "hub": True})[2:],
                         ("Streaming 27 fps to 2 clients", "connected through a hub; connect it directly if it drops out"))
        self.assertEqual(P.display("STREAMING", {"overlay": "dark"})[0:3], ("dark", "warn", "Tracker image is black"))
        self.assertEqual(P.display("STREAMING", {"overlay": "brownout", "ir_level": "L1"})[0:3],
                         ("brownout", "warn", "Tracker lost power, IR lowered to L1"))
        for state in ("SETTLING", "BACKOFF"):   # the brown-out hint also while waiting (a reconnect loop, usb-4)
            brown = {"overlay": "brownout", "ir_level": "L3", "last_failure": "stall"}
            self.assertEqual(P.display(state, brown)[0:3], ("brownout", "warn", "Tracker lost power, IR lowered to L3"))
            self.assertEqual(P.display(state, {"overlay": "dark", "last_failure": "stall"})[0], "recovering")
        self.assertEqual(P.display("STREAMING", {"overlay": "test_pattern", "next_rung": "S3"})[2],
                         "Tracker shows test bars, trying S3")

    def test_faults_incl_no_ir_and_port_in_use(self):
        kinds = {"permission": "no_permission", "busy": "busy", "unsupported": "unsupported", "library": "library",
                 "wedged": "wedged", "no_ir": "no_ir", "port": "port_in_use"}
        for kind, name in kinds.items():
            self.assertEqual(P.display("FAULT", {"fault": kind})[0:2], (name, "error"))
        self.assertEqual(P.display("FAULT", {"fault": "no_ir"}),
                         ("no_ir", "error", "Tracker IR light did not switch on", NO_IR_HINT))
        name, severity, message, hint = P.display("FAULT", {"fault": "port", "port": 9023, "owner": "python3 pid 77"})
        self.assertEqual((name, severity, message), ("port_in_use", "error", "Face stream port 9023 is in use"))
        self.assertEqual(hint, "Stop python3 pid 77 or set another `http_port` in `~/.config/frameface/config.json`, "
                               "then `python3 -I ~/frameface/frameface_main.py restart`; Baballonia's address "
                               "changes with it")
        self.assertEqual(P.display("FAULT", {"fault": "busy", "owner": "vftstream pid 812"})[3],
                         "Stop vftstream pid 812 (vftstream, Babble Frame Runtime, vive-face-init)")
        self.assertEqual(P.display("FAULT", {"fault": "port", "port": 19023})[2], "Face stream port 19023 is in use")
        self.assertEqual(P.display("FAULT", {"fault": "port"})[3][:24], "Stop the other program o")

    def test_teardown_and_swar_hint(self):
        self.assertEqual(P.display("TEARDOWN", {"fault": "no_ir"})[0], "no_ir")
        self.assertEqual(P.display("TEARDOWN", {"why": "not_worn"})[0], "standby")
        self.assertEqual(P.display("TEARDOWN", {"why": "disabled"})[0], "disabled")
        self.assertEqual(P.display("TEARDOWN", {"last_failure": "dark_start", "next_rung": "S4"})[0], "dark_start")
        swar = "reduced frame rate (python pixels): run install.sh again"
        self.assertEqual(P.display("STREAMING", {"fps": 30, "clients": 1, "pixels": "swar"})[3], swar)
        self.assertEqual(P.display("DISABLED", {"pixels": "swar"})[3],
                         "Run `python3 -I ~/frameface/frameface_main.py on` on the headset; " + swar)
        self.assertEqual(P.display("DISABLED", {"pixels": "c"})[3],
                         "Run `python3 -I ~/frameface/frameface_main.py on` on the headset")


if __name__ == "__main__":
    unittest.main()
