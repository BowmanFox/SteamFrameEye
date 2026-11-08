# SPDX-License-Identifier: MIT
"""The driver-matrix scenarios whose outcome depends on supervisor decisions, run through the real
Service (spec §11.3 test_ff_service, task T4): inline mode on SimClock, FakeKernel/FakeVft with the
matrix knobs, StubPreprocessor and StubJpegEncoder, a client connected and the headset worn. Each
case asserts the decisions, the final state and profile, kernel.violations == [] and the 13 trace
invariants. The scenario knobs and the helpers come from T2's matrix (test_ff_driver_matrix,
ffsup), so both supervisors face the same device models."""
import ffutil  # first: installs the port guard

import collections
import unittest
from unittest import mock

ffutil.setup_path()
from test_ff_driver_matrix import ACT16, LADDER6, TD15, XU_NAMES, actions, healthy, ops, rungs  # noqa: E402
from test_ff_service import OFF_SEQUENCE, SvcCase  # noqa: E402
from frameface.config import Config  # noqa: E402
from frameface.policy import DISPLAY, Decision, Failure as F, backoff_s  # noqa: E402


class ServiceMatrix(SvcCase):
    def test_hs_default(self):
        rig, s = self.service()
        self.assertTrue(self.run_for(s, 30, lambda s: s.saved_pair))
        self.assertEqual([(a["strategy"], a["n"], a["level"]) for a in s.activations], [("S0", 64, "L0")])
        self.assertEqual((s.failures, s.state, s.status()["display"]), ([], "STREAMING", "streaming"))
        p = self.profile(rig, s)
        self.assertEqual((p.strategy, p.xu_len, p.source, p.get_len, p.speed), ("S0", 64, "service", 64, 480))
        self.assertGreater(s.published, 250)                # worn + a client: ok frames are published
        self.assertGreaterEqual(s.activations[0]["t"] - 1000.0, 2.0)      # the enumeration settle
        self.done(rig, s)

    # -- the ladder

    def test_setif_after_required(self):
        rig, s = self.service(profile={"strategy": "S1"}, setif_required="after_commit")
        self.assertTrue(self.run_for(s, 80, lambda s: s.saved_pair))
        self.assertEqual((rungs(s), s.failures, actions(s)), (["S1", "S0"], [F.TEST_BARS], [("next_strategy", 12)]))
        self.assertGreaterEqual(s.counts["testbar"], 30)
        p = self.profile(rig, s)
        self.assertEqual((p.strategy, p.source), ("S0", "service"))
        self.done(rig, s)

    def test_xu_between_required(self):
        rig, s = self.service(xu_between_required=True)
        self.stream_ok(s, 120)
        self.assertEqual((rungs(s), s.failures), (["S0", "S1", "S2", "S3"], [F.NO_DATA] * 3))
        self.done(rig, s)

    def test_rhythm_required(self):
        rig, s = self.service(rhythm_required=True)
        self.stream_ok(s, 120)
        self.assertEqual((rungs(s), s.failures), (["S0", "S0", "S4"], [F.XU_TIMEOUT] * 2))
        self.assertEqual(actions(s), [("retry_same", 7), ("next_strategy", 8)])
        self.assertGreaterEqual(s.activations[1]["t"] - s.activations[0]["t"], 16.0)     # settle doubled once
        s.client_override = 0                   # an idle teardown; the client comes back at once
        self.assertTrue(self.run_for(s, 40, lambda s: s.state == "IDLE"))
        teardown_bt = s.store.load_runtime().last_teardown_bt
        s.client_override = 1
        self.stream_ok(s, 30)
        self.assertEqual((rungs(s)[-1], s.failures), ("S4", [F.XU_TIMEOUT] * 2))
        self.assertTrue(8.0 <= s.activations[-1]["t"] - teardown_bt < 9.0, s.activations[-1]["t"] - teardown_bt)
        self.done(rig, s)

    def test_ack_never_resets_then_faults_wedged(self):
        rig, s = self.service(ack_never=True)
        self.run_for(s, 300)
        self.assertEqual(rungs(s), ["S0", "S0", "S4", "S0"])
        self.assertEqual(actions(s), [("retry_same", 7), ("next_strategy", 8), ("reset_port", 13), ("fault", 15)])
        st = s.status()
        self.assertEqual((s.state, s.fault["kind"], st["display"], st["severity"]), ("FAULT", "wedged", "wedged",
                                                                                      "error"))
        self.assertEqual((s.counts["resets"], len(ops(s, "reset"))), (1, 1))
        tds = [t for st, t in s.states if st == "TEARDOWN"]       # activation start - its previous teardown
        gaps = [a["t"] - max(t for t in tds if t < a["t"]) for a in s.activations[1:]]
        self.assertGreaterEqual(gaps[0], 16.0)                      # row 7: settle_mult 2.0 ...
        self.assertTrue(all(g < 12.0 for g in gaps[1:]), gaps)     # ... once (rows 8, 13: settle_s + backoff)
        self.assertEqual(s.settle_mult, 1.0)
        self.check(rig, s)

    def test_programs_only_at_384(self):
        rig, s = self.service(programs_at_len=384)
        self.assertTrue(self.run_for(s, 240, lambda s: s.saved_pair))
        self.assertEqual((rungs(s), [a["n"] for a in s.activations]), (LADDER6, [64] * 5 + [384]))
        self.assertEqual((s.failures, [d.action for d in s.decisions]), ([F.DARK] * 5, ["next_strategy"] * 5))
        self.assertIn("IR light did not switch on; trying S1", s.decisions[0].hint)
        self.assertTrue(s.log.has("W stream: dark from start strategy=S0 xu_len=64 next=S1"))
        p = self.profile(rig, s)
        self.assertEqual((p.strategy, p.xu_len, p.source), ("S5", 384, "service"))
        s.stop()
        rig2, s2 = self.service(rig=rig, paths=s.paths)                 # the next service start
        self.stream_ok(s2)
        self.assertEqual(([(a["strategy"], a["n"]) for a in s2.activations], s2.failures), ([("S5", 384)], []))
        self.assertEqual(s2.status()["strategy"]["source"], "profile")
        self.done(rig, s2)

    def dark_ladder(self, s, final):
        self.assertEqual(rungs(s), LADDER6 + LADDER6 + ["S0"])
        self.assertEqual(actions(s), [("next_strategy", 12)] * 5 + [("reset_port", 13)] + [("next_strategy", 12)] * 5 +
                         [("ir_trial", 14)] + final)
        self.assertEqual([a["level"] for a in s.activations], ["L0"] * 12 + ["L1"])
        self.assertEqual(s.counts["resets"], 1)

    def test_regs_never_latch(self):
        rig, s = self.service(magic_needed=4, fps=15.0)
        seen, last = set(), [None]

        def watch(s):                           # the display of each BACKOFF/SETTLING period
            if s.state in ("BACKOFF", "SETTLING") and last[0] != (s.state, s.state_t):
                last[0] = (s.state, s.state_t)
                seen.add(s.status()["display"])
            return False
        self.run_for(s, 900, watch)
        self.dark_ladder(s, [("fault", 15)])
        self.assertIn("dark_start", seen)
        st = s.status()
        self.assertEqual((s.state, s.fault["kind"], st["display"], st["message"]),
                         ("FAULT", "no_ir", "no_ir", "Tracker IR light did not switch on"))
        self.assertIn("Every start sequence gave a black image", st["hint"])
        self.assertAlmostEqual(s.fault["until"] - s.fault["since"], 600.0)
        self.assertEqual(rig.vft.regs, {})
        self.check(rig, s)

    def test_dark_unexplained(self):
        rig, s = self.service(dark_from_s=20.0)
        self.assertTrue(self.run_for(s, 90, lambda s: len(s.decisions) == 2))
        self.assertEqual((s.failures, actions(s)), ([F.DARK, F.DARK], [("ir_trial", 4), ("suppress", 5)]))
        self.assertEqual([(a["strategy"], a["level"], a["trial"]) for a in s.activations],
                         [("S0", "L0", False), ("S0", "L1", True)])
        self.assertTrue(s.act.seen_ok)                  # inherited by the IR-trial re-activation
        self.run_for(s, 15)
        self.assertEqual((s.state, s.status()["display"], len(s.failures), len(s.activations)),
                         ("STREAMING", "dark", 2, 2))
        self.assertEqual(self.profile(rig, s).ir_level, "L0")
        self.done(rig, s)

    def test_dark_recovers_at_L1(self):
        rig, s = self.service(dark_below_current=0x96, dark_from_s=20.0)
        self.assertTrue(self.run_for(s, 90, lambda s: "ir_persisted" in s.events))
        self.assertEqual((s.failures, actions(s)), ([F.DARK], [("ir_trial", 4)]))
        p = self.profile(rig, s)
        self.assertEqual((p.ir_level, p.ir_reason, s.state), ("L1", "dark_recovered", "STREAMING"))
        self.done(rig, s)

    def test_dark_again_after_a_recovered_trial(self):
        rig, s = self.service(dark_below_current=0x96, dark_from_s=20.0)
        self.assertTrue(self.run_for(s, 90, lambda s: "ir_persisted" in s.events))
        rig.vft.m.dark_below_current = 0x78     # later even the L1 current gives a black image
        self.assertTrue(self.run_for(s, 90, lambda s: s.events.count("ir_persisted") == 2))
        self.assertEqual((s.failures, actions(s)), ([F.DARK] * 2, [("ir_trial", 4)] * 2))
        self.assertEqual([(a["level"], a["trial"]) for a in s.activations], [("L0", False), ("L1", True),
                                                                              ("L2", True)])
        p = self.profile(rig, s)
        self.assertEqual((p.ir_level, p.ir_reason, s.state), ("L2", "dark_recovered", "STREAMING"))
        self.done(rig, s)

    def test_dark_start_recovers_at_L1(self):
        rig, s = self.service(dark_below_current=0x96, fps=15.0)
        self.assertTrue(self.run_for(s, 900, lambda s: "ir_persisted" in s.events))
        self.dark_ladder(s, [])
        p = self.profile(rig, s)
        self.assertEqual((p.ir_level, p.ir_reason, s.state), ("L1", "dark_recovered", "STREAMING"))
        self.done(rig, s)

    # -- planes, settle and restarts

    def test_chroma80(self):
        rig, s = self.service(layout="h1_chroma80")
        self.assertTrue(self.run_for(s, 60, lambda s: s.phase == 0))
        self.assertTrue(s.log.has("W pixels: odd plane flat; using even bytes (not Baballonia parity)"))
        self.stream_ok(s, n=s.healthy + 60)
        p = self.profile(rig, s)
        self.assertEqual((p.y_phase, p.plane["verdict"]), ("even", "odd_flat"))
        self.assertGreater(s.counts["plane"], 30)
        self.assertEqual((s.failures, s.counts["testbar"], s.status()["stream"]["y_phase"]), ([], 0, "even"))
        self.done(rig, s)

    def test_settle_violation(self):
        rig, s1 = self.service()
        self.stream_ok(s1)
        s1.stop()
        teardown_bt = s1.store.load_runtime().last_teardown_bt
        rig, s2 = self.service(rig=rig, paths=s1.paths)
        self.stream_ok(s2)
        self.assertGreaterEqual(s2.activations[0]["t"], teardown_bt + 8.0)
        self.assertEqual((s2.failures, s2.dirty_start), ([], False))
        self.done(rig, s2)

    def test_dirty_restart(self):
        rig, s1 = self.service()
        self.stream_ok(s1)
        rig.kernel.note(crash=True)
        s1.driver.dev.close()                   # the process dies mid-stream: no Block C
        s1.driver = None
        self.assertTrue(rig.vft.wedged and s1.store.load_runtime().dirty)
        rig, s2 = self.service(rig=rig, paths=s1.paths)
        self.stream_ok(s2, 90)
        self.assertEqual((s2.dirty_start, s2.reset_allowance), (True, 0))
        self.assertEqual((s2.failures, actions(s2), rungs(s2)), ([F.XU_TIMEOUT], [("reset_port", 6)], ["S0", "S0"]))
        self.assertEqual((s2.counts["resets"], len(ops(s2, "reset"))), (1, 1))
        self.assertGreaterEqual(s2.activations[0]["t"] - s2.start_bt, 8.0)       # settle from process start
        self.done(rig, s2)

    def test_suspend_jump(self):
        rig, s = self.service()
        self.stream_ok(s)
        rig.clock.suspend(30.0)
        self.assertTrue(self.run_for(s, 60, lambda s: len(s.activations) == 2 and s.state == "STREAMING"))
        self.assertEqual((s.events.count("suspend"), s.failures, s.drivers[0].teardown_path), (1, [], "clean"))
        teardown_t = next(t for st, t in s.states if st == "TEARDOWN")
        self.assertGreaterEqual(s.activations[1]["t"] - teardown_t, 8.0)
        self.done(rig, s)

    def test_suspend_flush_with_the_tracker_attached(self):   # usb-2: Linux flushes every URB with -ESHUTDOWN
        for tick_first in (False, True):
            with self.subTest(tick_first=tick_first):
                rig, s = self.service()
                self.stream_ok(s)
                fd, t_susp = s.driver.dev.fd, rig.clock.monotonic()
                rig.kernel.system_suspend(30.0)         # the URBs flushed, the tracker attached, boottime +30 s
                if tick_first:
                    s._next_tick = rig.clock.monotonic()            # the tick sees the jump before any pump
                else:
                    s.driver.pump(0)                    # the reaper reaps the flushed URBs first
                self.assertTrue(self.run_for(s, 60, lambda s: len(s.activations) == 2 and s.state == "STREAMING"))
                self.assertEqual((s.events.count("suspend"), s.failures, s.counts["unplugs"], s.brown.total),
                                 (1, [], 0, 0))
                d, rt = s.drivers[0], s.store.load_runtime()
                self.assertEqual((d.teardown_path, d.stats["flushed"], rt.last_loss_bt), ("clean", 16, None))
                seq = [o["name"] if o["kind"] == "ctrl" else o["kind"] + str(o.get("iface", "")) for o in s.k.ops
                       if o.get("fd") == fd and o["t"] >= t_susp and (o["kind"] in ("discard", "clear_halt", "release",
                       "close") or o["kind"] == "ctrl" and o["name"] in XU_NAMES[:4])]
                self.assertEqual(seq[:14], OFF_SEQUENCE)                # Block C's 14 XU commands before any DISCARDURB
                self.assertEqual([k for k in seq[14:] if k != "discard"], ["clear_halt", "release1", "release0",
                                                                           "close"])
                close_t = next(o["t"] for o in s.k.ops if o["kind"] == "close" and o.get("fd") == fd)
                self.assertAlmostEqual(rt.last_teardown_bt, close_t + 30.0, delta=0.01)     # mark_clean_teardown
                self.assertGreaterEqual(s.activations[1]["t"] - close_t, s.cfg.settle_s)    # the settle from the close
                self.done(rig, s)

    def test_suspend_with_the_tracker_dropped_and_back_before_the_thaw(self):    # usb-2: both halves together
        rig, s = self.service()
        self.stream_ok(s)
        fd = s.driver.dev.fd
        rig.kernel.unplug(replug_after=0.05)    # off the bus and enumerated again while frozen: no absence is seen
        rig.clock.suspend(30.0)
        s._next_tick = rig.clock.monotonic()    # the tick sees the jump first: Block C meets ENODEV, the LOST path
        self.assertTrue(self.run_for(s, 60, lambda s: len(s.activations) == 2 and s.state == "STREAMING"))
        rt = s.store.load_runtime()
        close_t = next(o["t"] for o in s.k.ops if o["kind"] == "close" and o.get("fd") == fd)
        self.assertEqual((s.drivers[0].teardown_path, rt.last_teardown_bt, s.failures), ("lost", None, []))
        self.assertAlmostEqual(rt.last_loss_bt, close_t + 30.0, delta=0.01)
        self.assertGreaterEqual(s.activations[1]["t"] - close_t, s.cfg.settle_s)    # settle_s after a LOST too
        self.done(rig, s)

    # -- owners, plugs, power

    def test_other_usbfs_owner(self):
        rig, s = self.service(other_usbfs_owner=(812, "vftstream"))
        self.run_for(s, 30)
        st = s.status()
        self.assertEqual((s.state, s.fault["kind"], s.owner, st["display"]), ("FAULT", "busy", "vftstream pid 812",
                                                                             "busy"))
        self.assertAlmostEqual(s.fault["until"] - s.fault["since"], 30.0)
        self.assertEqual([(op["iface"], op["flags"], op["driver"], op.get("err")) for op in ops(s, "disconnect_claim")],
                         [(1, 2, "usbfs", "EBUSY")])
        self.assertIn("vftstream pid 812", st["hint"])
        self.assertTrue(s.log.has('W usb: busy interface=1 owner="vftstream pid 812"'))
        self.check(rig, s)

    def test_permission_ro(self):
        rig, s = self.service(node_mode="ro")
        self.run_for(s, 60)
        self.assertEqual((s.state, s.fault["kind"], actions(s)), ("FAULT", "permission", [("fault", 2)]))
        denied = [op["t"] - s.enum_bt for op in ops(s, "open") if op.get("err") == "EACCES"]
        self.assertEqual(len(denied), s.counts["eacces"] + 1)          # retried every 0.5 s for 5 s, then FAULT
        self.assertTrue(all(t < 5.0 for t in denied[:-1]) and denied[-1] >= 5.0, denied)
        self.assertTrue(all(0.45 <= b - a < 0.65 for a, b in zip(denied, denied[1:])), denied)
        self.assertEqual(s.status()["display"], "no_permission")
        self.assertTrue(s.log.has("E usb: permission denied node="))
        self.check(rig, s)

    def test_unplug_replug(self):
        rig, s = self.service(unplug_at_s=100.0, replug_after_s=3.0)     # a second unplug while streaming
        self.stream_ok(s)
        s.client_override = 0                   # the client leaves: idle teardown 30 s later
        self.assertTrue(self.run_for(s, 40, lambda s: s.state == "IDLE"))
        teardown_bt = s.store.load_runtime().last_teardown_bt
        rig.kernel.unplug(replug_after=3.0)
        self.run_for(s, 5)
        s.client_override = 1
        self.stream_ok(s)
        self.assertLess(s.activations[1]["t"], teardown_bt + 8.0)   # settle skipped: the device was absent
        self.assertTrue(self.run_for(s, 90, lambda s: len(s.activations) == 3 and s.state == "STREAMING"))
        self.assertEqual((s.counts["unplugs"], s.failures, s.brown.total), (1, [], 0))   # > 30 s after activation
        self.assertEqual(s.drivers[1].teardown_path, "lost")
        self.assertGreater(rig.kernel.devnum, 4)
        self.done(rig, s)

    def test_brownout(self):
        rig, s = self.service(brownout_current_above=0x96)
        self.assertTrue(self.run_for(s, 120, lambda s: s.activations and s.activations[-1]["level"] == "L1"
                                     and s.healthy > 60))
        p = self.profile(rig, s)
        self.assertEqual((p.ir_level, p.ir_reason), ("L1", "brownout"))
        self.assertEqual((s.failures, actions(s)), ([F.XFER_STORM] * 2, [("retry_same", 10)] * 2))
        self.assertEqual(([a["level"] for a in s.activations], s.brown.total, s.events), (["L0", "L0", "L1"], 2,
                                                                                          ["ir_change"]))
        self.assertTrue(s.log.has("W usb: ir level=L1 reason=brownout"))
        self.assertEqual(rig.kernel.devnum, 5)
        self.done(rig, s)

    def test_starve(self):
        rig, s = self.service()
        s.inline_lag = 0.04                     # a reaper 40 ms late on every wake
        self.stream_ok(s)
        self.assertEqual((s.events, s.n_urbs, s.driver.stats["urbs"]), (["urbs_grown"], 32, 32))
        self.assertGreater(s.driver.stats["starved_wakes"], 1)
        self.assertGreater(s.act.asm.stats["bad_hdr"], 0)          # bytes were lost before the growth
        rig.clock.sleep(0.3)                    # one long reaper stall: the device stops
        self.assertTrue(self.run_for(s, 60, lambda s: len(s.activations) == 2 and s.state == "STREAMING"
                                     and s.healthy > 30))
        self.assertEqual((s.failures, actions(s), s.events.count("urbs_grown")), ([F.STALL], [("retry_same", 10)], 1))
        self.assertEqual((s.driver.stats["urbs"], rig.vft.starve_stops), (32, 1))
        self.done(rig, s)

    def test_stop_during_activation(self):
        rig, s = self.service()

        def hook(kind, phase):
            if kind == "magic" and phase == "done" and rig.vft.magic_count == 2 and s.state == "ACTIVATING":
                s.stop()                        # e.g. SIGTERM: after MAGIC #2 Block A goes on to STREAM_ON (usb-1)
        rig.vft.on_xu = hook
        self.run_for(s, 30)
        self.assertEqual((s.state, len(s.activations), s.store.load_runtime().dirty), ("STOPPED", 1, False))
        names = [op["name"] for op in s.k.ops if op["kind"] == "ctrl" and op["name"] in XU_NAMES[:4]]
        block_a = ["MAGIC", "STREAM_OFF", "MAGIC"] + ["REG_WRITE"] * 11 + ["MAGIC", "STREAM_ON"]
        self.assertEqual(names, block_a + OFF_SEQUENCE)             # the stop waited for STREAM_ON's SET_CUR
        self.assertEqual((ops(s, "close")[-1]["inflight"], ops(s, "ctrl", "COMMIT_SET")), (0, []))
        self.check(rig, s)

    def test_xu_len_config_384(self):
        rig, s = self.service(cfg=Config(token="", xu_len=384), fps=15.0)
        self.run_for(s, 900)
        self.dark_ladder(s, [("fault", 15)])
        self.assertEqual({a["n"] for a in s.activations}, {384})
        self.assertEqual({op["wl"] for op in s.k.ops if op["kind"] == "ctrl" and op["name"] in XU_NAMES}, {384})
        self.check(rig, s)


class Cadence(SvcCase):
    """usb-1 through the Service with teardown_magic3: a stop (request_stop() from the clock, as the SIGTERM
    handler calls it) after REG_WRITE 6, in the 100 ms sleep or in MAGIC #3's ack poll, or REG_WRITE 8's lost
    ack, all between MAGIC #2 and STREAM_ON, end with the fake's PWM 0x02-0x04 at 0 and check_trace == []."""
    AT = {"write6": ("reg_write", "done", 6, 0.0001, (6, 2)), "sleep": ("reg_write", "done", 11, 0.05, (11, 2)),
          "magic3": ("magic", "set", 3, 0.0005, (11, 3)), "ack8": ("reg_write", "set", 8, None, None)}

    def first_activation(self, kind_, phase_, n_, delay):
        rig, s = self.service(cfg=Config(token="", teardown_magic3=True))
        seen, got = collections.Counter(), []

        def stop():                             # (REG_WRITE, MAGIC, STREAM_ON) SET_CURs sent when the stop lands
            got.append((seen["reg_write", "set"], seen["magic", "set"], seen["stream_on", "set"]))
            s.request_stop()

        def hook(kind, phase):
            seen[kind, phase] += 1
            hit = (kind, phase, seen[kind, phase]) == (kind_, phase_, n_)
            if hit and delay is not None:
                rig.clock.call_later(delay, stop)
            elif delay is None and phase == "set":
                rig.vft.m.ack_never = hit       # only REG_WRITE 8 is never acknowledged
        rig.vft.on_xu = hook
        self.addCleanup(setattr, rig.vft, "on_xu", None)
        self.assertTrue(self.run_for(s, 30, lambda s: s.state in ("STOPPED", "BACKOFF")))
        rig.vft.on_xu = None
        return rig, s, got

    def test_stop_or_ack_loss_between_magic2_and_stream_on_ends_with_pwm_0(self):
        for at, (kind_, phase_, n_, delay, where) in self.AT.items():
            with self.subTest(at=at):
                rig, s, got = self.first_activation(kind_, phase_, n_, delay)
                self.assertEqual([rig.vft.regs.get(a) for a in (2, 3, 4)], [0, 0, 0])
                self.assertEqual((s.drivers[0].teardown_path, s.store.load_runtime().dirty), ("clean", False))
                names = [o["name"] for o in s.k.ops if o["kind"] == "ctrl" and o["name"] in XU_NAMES[:4]]
                if delay is None:               # 8 ON writes; Block C: 11 OFF writes and one MAGIC first
                    self.assertEqual((s.failures, actions(s)), ([F.XU_TIMEOUT], [("retry_same", 7)]))
                    self.assertEqual(names, ACT16[:11] + ["REG_WRITE"] * 11 + ["MAGIC"] + TD15)
                else:                           # the stop waited for STREAM_ON's SET_CUR
                    self.assertEqual((got, s.state, s.failures, names), ([where + (0,)], "STOPPED", [], ACT16 + TD15))
                self.done(rig, s)


class Losses(SvcCase):
    """LOST, its settle and backoff, and the 600 s reset rule (review findings usb-2, usb-4, usb-6)."""

    def test_lost_without_an_absence_settles(self):
        rig, s = self.service(wedge_on_dirty_close=False)       # the fake enforces only its 8 s window
        for how in ("enodev", "glitch", "unplug"):
            self.assertTrue(self.run_for(s, 90, lambda s: s.state == "STREAMING" and
                                         s.clock.monotonic() - s.act.t > 31.0), how)     # past the B1 window
            n = len(s.activations)
            if how == "enodev":
                rig.kernel.force_status(-19)    # an ENODEV completion: the LOST path, the node stays present
            else:                               # re-enumerated after 0.4 s (one missing scan at most) or 3 s
                rig.kernel.unplug(replug_after=0.4 if how == "glitch" else 3.0)
            self.assertTrue(self.run_for(s, 30, lambda s: len(s.activations) > n), how)
            gap = s.activations[-1]["t"] - s.store.load_runtime().last_loss_bt
            if how == "unplug":
                self.assertLess(gap, 8.0, how)                  # a real absence still skips the settle
            else:
                self.assertGreaterEqual(gap, 8.0, how)          # no absence: settle_s after the loss
        self.stream_ok(s)
        self.assertEqual((s.failures, s.counts["unplugs"], s.brown.total), ([], 3, 0))
        self.assertEqual([d.teardown_path for d in s.drivers], ["lost"] * 3)
        self.done(rig, s)

    def test_lost_after_every_activation_backs_off(self):
        rig, s = self.service()

        def hook(kind, phase):                  # deep brown-out: off the bus 1 s after STREAM_ON, back 0.5 s later
            if kind == "stream_on" and phase == "done":
                rig.clock.call_at(rig.clock.monotonic() + 1.0, lambda: rig.kernel.unplug(0.5))
        rig.vft.on_xu = hook
        shown, l3 = [], []

        def watch(s):                           # the display of every SETTLING period
            if s.events.count("ir_change") == 3 and not l3:
                l3.append(s.clock.monotonic())  # lowered to L3
            if s.state == "SETTLING" and (not shown or shown[-1][0] != s.state_t):
                st = s.status()
                shown.append((s.state_t, st["display"], st["message"], st["hint"]))
            return False
        self.run_for(s, 600, watch)
        losses = [t for st, t in s.states if st == "LOST"]
        waits = [a["t"] - t for t, a in zip(losses, s.activations[1:])]       # a loss -> the next activation
        self.assertLess(len(s.activations), 20)
        self.assertTrue(all(w >= backoff_s(i) for i, w in enumerate(waits, 1)), waits)
        self.assertGreaterEqual(min(waits[-3:]), 60.0, waits)
        self.assertEqual((s.activations[-1]["level"], s.state, s.fault), ("L3", "SETTLING", None))
        late = {row[1:] for row in shown if l3 and row[0] > l3[0] + 60.0}    # the lowering's overlay is over
        self.assertIn(("brownout", "Tracker lost power, IR lowered to L3", DISPLAY["brownout"][2]), late)
        self.assertTrue(s.log.has("W usb: brownout at the lowest IR level level=L3"))
        self.check(rig, s)

    def test_dirty_start_within_600_s_of_a_reset_does_not_reset_again(self):
        rig, s1 = self.service(ack_never=True)

        class Crash(BaseException):
            pass

        def hook(kind, phase):                  # the process dies (SIGKILL) in the activation after the reset
            if kind == "magic" and phase == "set" and s1.counts["resets"] == 1 and s1.state == "ACTIVATING":
                raise Crash()
        rig.vft.on_xu = hook
        with self.assertRaises(Crash):
            self.run_for(s1, 300)
        rig.vft.on_xu = None
        self.assertEqual(actions(s1), [("retry_same", 7), ("next_strategy", 8), ("reset_port", 13)])
        rig.kernel.note(crash=True)
        s1.driver.dev.close()                   # the kernel closes the dead process's fd: no Block C
        s1.driver, s1._shut = None, True
        rig, s2 = self.service(rig=rig, paths=s1.paths)
        self.run_for(s2, 120)
        self.assertEqual((s2.dirty_start, s2.reset_allowance, s2.state), (True, 1, "FAULT"))   # allowance kept
        self.assertEqual(actions(s2), [("retry_same", 7), ("next_strategy", 8), ("fault", 15)])
        self.assertEqual(len(ops(s2, "reset")), 1)                     # only service 1's, 600 s apart at least
        self.assertEqual(rig.kernel.violations, [])

    def test_a_reset_within_600_s_of_the_last_is_never_sent(self):
        rig, s = self.service()
        self.stream_ok(s)
        s.store.mark_reset()                    # e.g. a reset of the previous process just now
        s.act.request = (F.NO_FRAMES, {})
        with mock.patch("frameface.supervisor.on_failure", return_value=Decision("reset_port", strategy="S0", row=13)):
            self.assertTrue(self.run_for(s, 1, lambda s: s.state == "RESETTING"))
        self.assertEqual((ops(s, "reset"), s.counts["resets"]), ([], 0))
        self.assertTrue(s.log.has("W usb: reset skipped reason=no_frames"))
        self.stream_ok(s)
        self.done(rig, s)


class LadderPass(SvcCase):
    """A rung that streamed healthily for 60 s ends the ladder pass (decision 7; review finding runtime-2)."""

    def proven_then_transient(self, rig, s, healthy_s):
        """healthy_s of streaming, an idle teardown, then one activation shows test bars (stale XU state,
        §b.15); the image is normal again from the next activation on."""
        self.assertTrue(self.run_for(s, 120, healthy(30), stop_at=()))
        self.run_for(s, healthy_s, stop_at=())
        s.client_override = 0
        self.assertTrue(self.run_for(s, 40, lambda s: s.state == "IDLE", stop_at=()))
        n = len(s.failures)
        rig.vft.m.scene, s.client_override = "bars", 1
        self.assertTrue(self.run_for(s, 120, lambda s: len(s.failures) > n or s.state == "FAULT", stop_at=()))
        rig.vft.m.scene = "mouth"

    def test_a_transient_after_a_proven_stream_retries_the_rung(self):
        rig, s = self.service(fps=15.0)
        self.proven_then_transient(rig, s, 300.0)
        self.assertTrue(self.run_for(s, 60, healthy(30)))
        self.assertEqual((rungs(s), actions(s)), (["S0", "S0", "S0"], [("retry_same", 11)]))
        p = self.profile(rig, s)
        self.assertEqual((p.strategy, p.xu_len, p.source), ("S0", 64, "service"))
        self.done(rig, s)

    def test_rare_transients_never_reset_or_fault(self):
        rig, s = self.service(fps=15.0)
        for _ in range(12):                     # one transient per idle cycle, > 60 s healthy in between
            self.proven_then_transient(rig, s, 70.0)
        self.assertEqual((actions(s), s.counts["resets"], set(rungs(s))), ([("retry_same", 11)] * 12, 0, {"S0"}))
        self.assertTrue(self.run_for(s, 60, healthy(30)))
        self.done(rig, s)

    def test_the_proven_rung_stays_first_when_the_profile_cannot_be_written(self):     # review finding A-2
        rig, s = self.service(fps=15.0, profile={"strategy": "S1", "xu_len": 64, "source": "service"},
                              setif_required="after_commit")   # the stale known-good S1 shows test bars, S0 streams

        def full(p):
            raise OSError(28, "No space left on device")
        s.store.save_profile = full             # profile.json cannot be written: S1 stays its known-good rung
        self.proven_then_transient(rig, s, 70.0)
        self.assertTrue(self.run_for(s, 60, healthy(30)))
        self.assertEqual((rungs(s), actions(s)), (["S1", "S0", "S0", "S0"], [("next_strategy", 12),
                                                                             ("retry_same", 11)]))
        self.assertTrue(s.log.has("W state: profile write failed"))
        self.done(rig, s)

    def test_a_proven_rung_that_breaks_is_retried_once(self):
        rig, s = self.service(fps=15.0)
        self.assertTrue(self.run_for(s, 120, healthy(30)))
        self.run_for(s, 70.0)
        s.client_override = 0
        self.assertTrue(self.run_for(s, 40, lambda s: s.state == "IDLE"))
        rig.vft.m.xu_between_required, s.client_override = True, 1      # from now on only S3 streams
        self.stream_ok(s, 180)
        self.assertEqual(rungs(s), ["S0", "S0", "S0", "S1", "S2", "S3"])
        self.assertEqual(actions(s), [("retry_same", 11)] + [("next_strategy", 12)] * 3)
        self.done(rig, s)


class Retired(SvcCase):
    def test_retired_drivers_keep_no_frame_buffers(self):              # review finding runtime-5
        rig, s = self.service(fps=15.0)
        for _ in range(4):
            self.stream_ok(s, 60, n=5)
            s.client_override = 0
            self.assertTrue(self.run_for(s, 40, lambda s: s.state == "IDLE"))
            s.client_override = 1
        pools = {id(d.sink.asm.pool) for d in s.drivers if d.sink is not None}
        self.assertEqual((len(s.drivers), len(pools)), (4, 1))        # only the newest keeps its stream sink
        self.assertTrue(all(d.urbs.sink is None for d in s.drivers[:-1]))
        self.assertEqual((s.drivers[-1].teardown_path, s.drivers[-1].stats["urbs"]), ("clean", 16))
        self.done(rig, s)


if __name__ == "__main__":
    unittest.main()
