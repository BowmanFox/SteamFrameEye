# SPDX-License-Identifier: MIT
"""The driver matrix (spec §11.3 test_ff_driver_matrix, task T2): 46 cases on SimClock. Each case
asserts the final state, counters and profile, kernel.violations == [] and the 13 trace invariants
(sim.check_trace). The supervisor `Sup` and the RigCase base live in ffsup (shared)."""
import ffutil  # first: installs the port guard

import collections
import platform
import time
import unittest

ffutil.setup_path()
from ffsup import RigCase, Sup  # noqa: E402
from frameface import uvc  # noqa: E402
from frameface.config import Config  # noqa: E402
from frameface.policy import STRATEGIES, Failure as F  # noqa: E402
from frameface.sim import check_trace  # noqa: E402
from frameface.sysfs import interface_drivers  # noqa: E402
from frameface.usbfs import UsbDevice  # noqa: E402
from frameface.vft import Aborted, DriverConfig, VftDriver, XuTimeout  # noqa: E402

LADDER6 = ["S0", "S1", "S2", "S3", "S4", "S5"]
XU_NAMES = ("MAGIC", "STREAM_OFF", "STREAM_ON", "REG_WRITE", "XU_GET_CUR")
ACT16 = ["MAGIC", "STREAM_OFF", "MAGIC"] + ["REG_WRITE"] * 11 + ["MAGIC", "STREAM_ON"]      # Block A's SET_CURs
TD15 = ["MAGIC", "STREAM_OFF", "MAGIC"] + ["REG_WRITE"] * 11 + ["MAGIC"]                   # Block C's, with 7b


def rungs(s):
    return [a["strategy"] for a in s.activations]


def actions(s):
    return [(d.action, d.row) for d in s.decisions]


def ops(s, kind, name=None):
    return [op for op in s.k.ops if op["kind"] == kind and (name is None or op.get("name") == name)]


def healthy(n):
    return lambda s: s.state == "STREAMING" and s.healthy >= n


class Matrix(RigCase):
    elapsed = 0.0

    @classmethod
    def setUpClass(cls):
        cls.t0 = time.perf_counter()

    @classmethod
    def tearDownClass(cls):
        Matrix.elapsed = time.perf_counter() - cls.t0

    def stream_ok(self, s, seconds=30, n=60):
        self.assertTrue(s.run(seconds, healthy(n)), "not streaming: %s %s" % (s.states[-3:], s.failures))

    def done(self, s):
        """Stop a streaming case and check violations and invariants on the complete trace."""
        s.stop()
        self.assertEqual(s.store.load_runtime().dirty, False)
        self.check(s)

    # -- personas and identity

    def test_hs_default(self):
        rig, s = self.make()
        self.assertTrue(s.run(30, lambda s: s.saved_pair))
        self.assertEqual([(a["strategy"], a["n"], a["level"]) for a in s.activations], [("S0", 64, "L0")])
        self.assertEqual((s.failures, s.state, s.display()[0]), ([], "STREAMING", "streaming"))
        p = s.profile()
        self.assertEqual((p.strategy, p.xu_len, p.source, p.get_len, p.speed), ("S0", 64, "service", 64, 480))
        self.assertAlmostEqual(p.fps_pts, 60.0, delta=0.1)
        self.assertGreaterEqual(s.healthy, 300)
        self.assertEqual((s.driver.stats["urbs"], s.driver.urb_bytes), (16, 32768))
        self.assertGreaterEqual(s.activations[0]["t"] - 1000.0, 2.0)          # the enumeration settle
        self.done(s)

    def test_ss_persona(self):
        rig, s = self.make(persona="ss")
        self.stream_ok(s)
        self.assertEqual((s.activations[0]["n"], s.ident.speed, s.ident.layout.max_packet), (384, 5000, 1024))
        for op in ops(s, "ctrl"):
            if op["name"] in ("MAGIC", "STREAM_OFF", "REG_WRITE", "STREAM_ON"):
                self.assertEqual((op["wl"], op["out"][508:512]), (384, "5354" if op["name"] == "MAGIC" else "0000"))
        self.done(s)

    def test_get_len_128(self):
        rig, s = self.make(get_len=128)
        self.stream_ok(s)
        self.assertEqual(s.activations[0]["n"], 128)
        self.assertTrue(s.log.has("get_len unexpected value=128 (expected 64 or 384); using it"))
        self.assertEqual({len(op["out"]) // 2 for op in ops(s, "ctrl", "MAGIC")}, {128})
        self.done(s)

    # -- the strategy ladder

    def test_setif_after_required(self):
        rig, s = self.make(profile={"strategy": "S1"}, setif_required="after_commit")
        self.assertTrue(s.run(80, lambda s: s.saved_pair))
        self.assertEqual((rungs(s), s.failures, actions(s)), (["S1", "S0"], [F.TEST_BARS], [("next_strategy", 12)]))
        self.assertGreaterEqual(s.counts["testbar"], 30)
        self.assertEqual((s.profile().strategy, s.profile().source), ("S0", "service"))
        self.done(s)

    def test_setif_before_required(self):
        rig, s = self.make(profile={"strategy": "S2"}, setif_required="before_probe")
        self.stream_ok(s, 60)
        self.assertEqual((rungs(s), s.failures, actions(s)), (["S2", "S0"], [F.TEST_BARS], [("next_strategy", 12)]))
        self.done(s)

    def test_xu_between_required(self):
        rig, s = self.make(xu_between_required=True)
        self.stream_ok(s, 120)
        self.assertEqual((rungs(s), s.failures), (["S0", "S1", "S2", "S3"], [F.NO_DATA] * 3))
        self.done(s)

    def test_rhythm_required(self):
        rig, s = self.make(rhythm_required=True)
        self.stream_ok(s, 120)
        self.assertEqual((rungs(s), s.failures), (["S0", "S0", "S4"], [F.XU_TIMEOUT] * 2))
        self.assertEqual(actions(s), [("retry_same", 7), ("next_strategy", 8)])
        self.assertEqual(s.decisions[0].settle_mult, 2.0)
        self.assertGreaterEqual(s.activations[1]["t"] - s.activations[0]["t"], 16.0)     # settle doubled once
        self.done(s)

    def test_no_stream_ack(self):
        rig, s = self.make(stream_acks=False)
        self.stream_ok(s)
        act = s.driver.last_activation
        self.assertEqual((act.stream_off_ack, act.stream_on_ack), ("timeout", "timeout"))
        self.done(s)

    def test_slow_busy(self):
        rig, s = self.make(ack_busy_polls=150)
        self.stream_ok(s)
        act = s.driver.last_activation
        self.assertTrue(all(c["busy"] == 150 and c["ack"] == "done" for c in act.cmds if "STREAM" not in c["name"]))
        self.assertGreater(act.total_ms, 15 * 150)
        self.done(s)

    def test_ack_never(self):
        rig, s = self.make(ack_never=True)
        s.run(300)
        self.assertEqual(rungs(s), ["S0", "S0", "S4", "S0"])
        self.assertEqual(actions(s), [("retry_same", 7), ("next_strategy", 8), ("reset_port", 13), ("fault", 15)])
        self.assertEqual((s.state, s.fault.fault, s.fault.delay_s, s.display()[0]), ("FAULT", "wedged", 60.0, "wedged"))
        self.assertEqual((s.counts["resets"], len(ops(s, "reset"))), (1, 1))
        self.check(s)

    def test_settle_violation(self):
        rig, s1 = self.make()
        self.stream_ok(s1)
        s1.stop()
        teardown_bt = s1.store.load_runtime().last_teardown_bt
        s2 = Sup(rig, s1.store, Config(token=""))
        self.stream_ok(s2)
        self.assertGreaterEqual(s2.activations[0]["t"], teardown_bt + 8.0)
        self.assertEqual(s2.failures, [])
        s2.stop()
        node = rig.node()                       # the fake enforces the window: no wait -> REG_WRITE acks time out
        drv = VftDriver(UsbDevice(rig.kernel, node.devpath), node, rig.clock, None, DriverConfig(), inline=True)
        drv.open()
        rig.kernel.note(strategy="S0", n=64, set_config=False)
        with self.assertRaises(XuTimeout) as cm:
            drv.activate(STRATEGIES["S0"], 0xFF, 0xB2, 64)
        self.assertEqual(cm.exception.cmd, "REG 00=40")
        drv.teardown()
        self.check(s2)

    def test_dirty_restart(self):
        rig, s1 = self.make()
        self.stream_ok(s1)
        rig.kernel.note(crash=True)
        s1.driver.dev.close()                   # the process dies mid-stream: no Block C
        self.assertTrue(rig.vft.wedged and s1.store.load_runtime().dirty)
        s2 = Sup(rig, s1.store, Config(token=""))
        self.assertEqual((s2.dirty_start, s2.reset_allowance), (True, 1))
        self.stream_ok(s2, 90)
        self.assertEqual((s2.failures, actions(s2), rungs(s2)), ([F.XU_TIMEOUT], [("reset_port", 6)], ["S0", "S0"]))
        self.assertEqual((s2.reset_allowance, s2.counts["resets"], len(ops(s2, "reset"))), (0, 1, 1))
        self.assertGreaterEqual(s2.activations[0]["t"] - s2.start_bt, 8.0)       # settle from process start
        self.done(s2)

    def test_stale_xu_behind_hub(self):
        rig, s = self.make(ack_never=True, sysname="1-1.2")
        s.run(300)
        self.assertTrue(s.node.behind_hub)
        self.assertEqual(actions(s), [("retry_same", 7), ("next_strategy", 8), ("fault", 15)])
        self.assertEqual((s.state, s.fault.fault, ops(s, "reset")), ("FAULT", "wedged", []))
        self.check(s)

    # -- payloads, URBs and frames

    def test_payload_16k(self):
        rig, s = self.make(payload=16384)
        self.stream_ok(s)
        self.assertEqual(s.driver.urb_bytes, 16384)
        self.assertEqual(round(rig.kernel.completion_events / rig.vft.frames_emitted), 20)
        self.done(s)

    def test_payload_lies(self):
        rig, s = self.make(payload=16384, probe_payload=32768)
        self.stream_ok(s)
        self.assertEqual((s.driver.urb_bytes, s.reasm.max_payload), (32768, 16384))
        self.assertGreaterEqual(s.asm.stats["stride_learned"], 1)
        self.assertEqual(s.failures, [])
        self.done(s)

    def test_payload_quirk_ffff8000(self):
        rig, s = self.make(probe_payload=0xFFFF8000)
        self.stream_ok(s)
        self.assertEqual((s.reasm.max_payload, s.driver.urb_bytes), (32768, 32768))
        self.assertTrue(s.log.has("masked to 32768"))
        self.done(s)

    def test_payload_zero(self):
        rig, s = self.make(probe_payload=0)
        self.stream_ok(s)
        self.assertEqual(s.reasm.max_payload, 32768)
        self.assertTrue(s.log.has("max_payload 0 invalid; using 32768"))
        self.done(s)

    def test_urb_64k(self):
        rig, s = self.make(cfg=Config(token="", urb_bytes=65536))
        self.stream_ok(s)
        self.assertEqual(s.driver.urb_bytes, 65536)
        self.assertEqual(round(rig.kernel.completion_events / rig.vft.frames_emitted), 5)
        self.done(s)

    def test_urb_einval_above_16k(self):
        rig, s = self.make(urb_max_bytes=16384)
        self.stream_ok(s)
        self.assertEqual(s.driver.urb_bytes, 16384)
        self.assertTrue(s.log.has("urb size reduced to=16384"))
        self.assertEqual({op.get("err") for op in ops(s, "submit") if op["len"] > 16384}, {"EINVAL"})
        self.done(s)

    def test_fid_no_eof(self):
        rig, s = self.make(eof=False)
        self.stream_ok(s)
        commit = ops(s, "ctrl", "COMMIT_SET")[-1]
        self.assertEqual(uvc.unpack_probe(bytes.fromhex(commit["out"])).framing_info, 1)   # FID only
        self.assertEqual((s.asm.eof_seen, s.asm.stats["bad_size"] <= 1), (False, True))
        self.done(s)

    def test_err_bit(self):
        rig, s = self.make(err_every_k_frames=3)
        self.stream_ok(s)
        s.run(3)
        bad, ok = s.asm.stats["bad_err"], s.asm.stats["frames_ok"]
        self.assertLessEqual(abs(3 * bad - (bad + ok)), 3)          # every third frame carries ERR
        self.assertEqual((s.state, s.failures), ("STREAMING", []))
        self.done(s)

    def test_size_614400(self):
        rig, s = self.make(frame_bytes_override=614400)
        self.assertTrue(s.run(60, lambda s: len(s.activations) == 2))
        self.assertEqual((s.failures, actions(s), rungs(s)), ([F.NO_FRAMES], [("next_strategy", 12)], ["S0", "S1"]))
        self.assertGreater(s.drivers[0].sink.asm.stats["unexpected_size_614400"], 10)
        self.assertEqual(s.drivers[0].sink.asm.stats["frames_ok"], 0)
        s.stop()
        self.check(s)

    def test_descriptor_640x480(self):
        rig, s = self.make(frame_w=640, frame_h=480)
        s.run(30)
        self.assertEqual((s.state, s.failures, actions(s), s.fault.fault), ("FAULT", [F.UNSUPPORTED], [("fault", 3)],
                                                                            "unsupported"))
        self.assertEqual((s.activations, ops(s, "ctrl", "MAGIC"), s.display()[0]), ([], [], "unsupported"))
        self.check(s)

    def test_fps_27(self):
        rig, s = self.make(fps=27.0)
        self.stream_ok(s)
        s.run(5)
        self.assertAlmostEqual(s.asm.fps.fps_pts, 27.0, delta=0.2)
        self.assertEqual((s.state, s.failures), ("STREAMING", []))
        self.done(s)

    def test_starve(self):
        rig, s = self.make(lag=0.04)                               # a reaper 40 ms late on every wake
        self.stream_ok(s)
        self.assertEqual((s.events, s.n_urbs, s.driver.stats["urbs"]), (["urbs_grown"], 32, 32))
        self.assertGreater(s.driver.stats["starved_wakes"], 1)
        self.assertGreater(s.asm.stats["bad_hdr"], 0)              # bytes were lost before the growth
        s.hiccups = [(rig.clock.monotonic() + 2.0, 0.3)]           # one long reaper stall: the device stops
        self.assertTrue(s.run(60, lambda s: len(s.activations) == 2 and s.state == "STREAMING" and s.healthy > 30))
        self.assertEqual((s.failures, actions(s), s.events.count("urbs_grown")), ([F.STALL], [("retry_same", 10)], 1))
        self.assertEqual((s.driver.stats["urbs"], rig.vft.starve_stops), (32, 1))
        self.done(s)

    def test_eproto_burst(self):
        rig, s = self.make(eproto_burst_at_s=8.0)
        self.assertTrue(s.run(60, lambda s: len(s.activations) == 2 and s.healthy > 30))
        self.assertEqual((s.failures, actions(s), s.brown.total), ([F.XFER_STORM], [("retry_same", 10)], 1))
        self.assertEqual(s.drivers[0].stats["eproto"], 90)        # 0.15 s of completions at 600 URB/s
        self.assertEqual(s.activations[1]["level"], "L0")
        self.done(s)

    def test_epipe(self):
        rig, s = self.make(epipe_at_s=6.0)
        self.stream_ok(s)
        s.run(6)
        self.assertEqual((s.driver.stats["epipe"], s.failures, s.state), (1, [], "STREAMING"))
        self.assertEqual(len(ops(s, "clear_halt")), 1)
        self.done(s)

    # -- plug events, permissions and owners

    def test_unplug_replug(self):
        rig, s = self.make(unplug_at_s=60.0, replug_after_s=3.0)
        self.stream_ok(s)
        s.idle()                                                   # an idle teardown, then a 3 s unplug
        teardown_bt = s.store.load_runtime().last_teardown_bt
        rig.kernel.unplug(replug_after=3.0)
        s.resume()
        self.stream_ok(s)
        self.assertLess(s.activations[1]["t"], teardown_bt + 8.0)  # settle skipped: the device was absent
        self.assertTrue(s.run(60, lambda s: len(s.activations) == 3 and s.state == "STREAMING"))
        self.assertEqual((s.counts["unplugs"], s.failures, s.brown.total), (1, [], 0))
        self.assertEqual(s.drivers[1].teardown_path, "lost")
        self.assertGreater(rig.kernel.devnum, 4)
        self.done(s)

    def test_brownout(self):
        rig, s = self.make(brownout_current_above=0x96)
        self.assertTrue(s.run(120, lambda s: s.activations and s.activations[-1]["level"] == "L1" and s.healthy > 60))
        p = s.profile()
        self.assertEqual((p.ir_level, p.ir_reason), ("L1", "brownout"))
        self.assertEqual((s.failures, actions(s)), ([F.XFER_STORM] * 2, [("retry_same", 10)] * 2))
        self.assertEqual(([a["level"] for a in s.activations], s.brown.total, s.events), (["L0", "L0", "L1"], 2,
                                                                                          ["ir_change"]))
        self.assertEqual(rig.kernel.devnum, 5)                     # each brown-out re-enumerated the tracker
        self.done(s)

    def test_permission_ro(self):
        rig, s = self.make(node_mode="ro")
        s.run(60)
        self.assertEqual((s.state, s.fault.fault, s.fault.delay_s, actions(s)), ("FAULT", "permission", 30.0,
                                                                                [("fault", 2)]))
        self.assertEqual((s.counts["eacces"], len([o for o in ops(s, "open") if o.get("err") == "EACCES"])), (5, 6))
        self.assertEqual(s.display()[0], "no_permission")
        self.check(s)

    def test_eacces_for_3s(self):
        rig, s = self.make(node_mode="ro_for:3")
        self.stream_ok(s)
        self.assertEqual((s.counts["eacces"], s.failures), (2, []))      # 2.0 s and 2.5 s after enumeration
        self.done(s)

    def test_kernel_driver_uvcvideo(self):
        rig, s = self.make(kernel_driver="uvcvideo")
        self.assertEqual(interface_drivers(rig.sys_root, "1-1"), {0: "uvcvideo", 1: "uvcvideo"})
        self.stream_ok(s)
        self.assertEqual([(op["iface"], op["flags"], op["driver"]) for op in ops(s, "disconnect_claim")],
                         [(0, 2, "usbfs"), (1, 2, "usbfs")])
        self.assertEqual([op["driver"] for op in ops(s, "getdriver")], ["uvcvideo", "uvcvideo"])
        self.assertEqual(interface_drivers(rig.sys_root, "1-1"), {0: None, 1: None})
        self.done(s)

    def test_other_usbfs_owner(self):
        rig, s = self.make(other_usbfs_owner=(812, "vftstream"))
        s.run(30)
        self.assertEqual((s.state, s.fault.fault, s.fault.delay_s, s.owner), ("FAULT", "busy", 30.0, ["vftstream pid 812"]))
        self.assertEqual([(op["iface"], op["flags"], op["driver"], op.get("err")) for op in ops(s, "disconnect_claim")],
                         [(1, 2, "usbfs", "EBUSY")])
        self.assertIn("vftstream pid 812", s.display()[3])
        self.check(s)

    # -- XU length and dark images

    def test_programs_only_at_384(self):
        rig, s = self.make(programs_at_len=384)
        self.assertTrue(s.run(240, lambda s: s.saved_pair))
        self.assertEqual((rungs(s), [a["n"] for a in s.activations]), (LADDER6, [64] * 5 + [384]))
        self.assertEqual((s.failures, [d.action for d in s.decisions]), ([F.DARK] * 5, ["next_strategy"] * 5))
        self.assertIn("IR light did not switch on; trying S1", s.decisions[0].hint)
        p = s.profile()
        self.assertEqual((p.strategy, p.xu_len, p.source), ("S5", 384, "service"))
        s.stop()
        s2 = Sup(rig, s.store, Config(token=""))
        self.stream_ok(s2)
        self.assertEqual(([(a["strategy"], a["n"]) for a in s2.activations], s2.failures), ([("S5", 384)], []))
        self.done(s2)

    def dark_ladder(self, s, final_action):
        self.assertEqual(rungs(s), LADDER6 + LADDER6 + ["S0"])
        self.assertEqual(actions(s), [("next_strategy", 12)] * 5 + [("reset_port", 13)] + [("next_strategy", 12)] * 5 +
                         [("ir_trial", 14)] + final_action)
        self.assertEqual([a["level"] for a in s.activations], ["L0"] * 12 + ["L1"])
        self.assertEqual(s.counts["resets"], 1)

    def test_regs_never_latch(self):
        rig, s = self.make(magic_needed=4, fps=15.0)
        s.run(900)
        self.dark_ladder(s, [("fault", 15)])
        self.assertEqual((s.state, s.fault.fault, s.fault.delay_s), ("FAULT", "no_ir", 600.0))
        name, _, message, hint = s.display()
        self.assertEqual((name, message), ("no_ir", "Tracker IR light did not switch on"))
        self.assertIn("Every start sequence gave a black image", hint)
        self.assertEqual(rig.vft.regs, {})
        self.check(s)

    def test_xu_len_config_384(self):
        rig, s = self.make(cfg=Config(token="", xu_len=384), fps=15.0)
        s.run(900)
        self.dark_ladder(s, [("fault", 15)])
        self.assertEqual({a["n"] for a in s.activations}, {384})
        self.assertEqual({op["wl"] for op in s.k.ops if op["kind"] == "ctrl" and op["name"] in XU_NAMES}, {384})
        self.check(s)

    def test_dark_unexplained(self):
        rig, s = self.make(dark_from_s=20.0)
        self.assertTrue(s.run(90, lambda s: s.overlay == "dark"))
        self.assertEqual((s.failures, actions(s)), ([F.DARK, F.DARK], [("ir_trial", 4), ("suppress", 5)]))
        self.assertEqual([(a["strategy"], a["level"], a["trial"]) for a in s.activations],
                         [("S0", "L0", False), ("S0", "L1", True)])
        s.run(15)
        self.assertEqual((s.state, s.display()[0], len(s.failures), len(s.activations)), ("STREAMING", "dark", 2, 2))
        self.assertEqual(s.profile().ir_level, "L0")
        self.done(s)

    def test_dark_recovers_at_L1(self):
        rig, s = self.make(dark_below_current=0x96, dark_from_s=20.0)
        self.assertTrue(s.run(90, lambda s: "ir_persisted" in s.events))
        self.assertEqual((s.failures, actions(s)), ([F.DARK], [("ir_trial", 4)]))
        p = s.profile()
        self.assertEqual((p.ir_level, p.ir_reason, s.state), ("L1", "dark_recovered", "STREAMING"))
        self.done(s)

    def test_dark_start_recovers_at_L1(self):
        rig, s = self.make(dark_below_current=0x96, fps=15.0)
        self.assertTrue(s.run(900, lambda s: "ir_persisted" in s.events))
        self.dark_ladder(s, [])
        p = s.profile()
        self.assertEqual((p.ir_level, p.ir_reason, s.state), ("L1", "dark_recovered", "STREAMING"))
        self.done(s)

    # -- teardown, planes, health and stops

    def teardown_sets(self, s):
        sets = [op for op in s.k.ops if op["kind"] == "ctrl" and op["name"] in ("MAGIC", "STREAM_OFF", "REG_WRITE")]
        return [op["name"] for op in sets[-15:]]

    def test_teardown_magic3(self):
        rig, s = self.make(cfg=Config(token="", teardown_magic3=True))
        self.stream_ok(s)
        s.stop()
        self.assertEqual(self.teardown_sets(s)[-15:], ["MAGIC", "STREAM_OFF", "MAGIC"] + ["REG_WRITE"] * 11 + ["MAGIC"])
        self.assertEqual([rig.vft.regs[a] for a in (2, 3, 4)], [0, 0, 0])
        self.assertEqual(rig.vft.pwm_history[-1][1], 0)
        self.check(s)

    def test_chroma80(self):
        rig, s = self.make(layout="h1_chroma80")
        self.assertTrue(s.run(60, lambda s: s.phase == 0))
        self.assertTrue(s.log.has("pixels: odd plane flat; using even bytes (not Baballonia parity)"))
        self.assertEqual((s.profile().y_phase, s.profile().plane["verdict"]), ("even", "odd_flat"))
        self.assertGreater(s.counts["plane"], 30)
        self.stream_ok(s, n=s.healthy + 60)
        self.assertEqual((s.failures, s.counts["testbar"]), ([], 0))
        self.done(s)

    def test_h2_full(self):
        rig, s = self.make(layout="h2_full")
        self.assertTrue(s.run(30, lambda s: "h2_hint" in s.events))
        self.assertEqual((s.phase, s.layouts[-1].verdict, s.layouts[-1].h2_hint), (1, "distinct", True))
        self.assertEqual(s.failures, [])
        self.done(s)

    def test_frozen(self):
        rig, s = self.make(scene="gradient")
        self.assertTrue(s.run(60, lambda s: len(s.activations) == 2 and s.state == "STREAMING"))
        self.assertEqual((s.failures, actions(s), s.soft_in_row), ([F.FROZEN], [("retry_same", 10)], 1))
        self.assertGreaterEqual(s.counts["frozen"], 60)
        self.done(s)

    def test_stop_while_streaming(self):
        rig, s = self.make()
        self.stream_ok(s)
        s.stop()
        self.assertEqual((s.state, s.store.load_runtime().dirty), ("STOPPED", False))
        self.assertIsNotNone(s.store.load_runtime().last_teardown_bt)
        self.assertEqual(self.teardown_sets(s)[-14:], ["MAGIC", "STREAM_OFF", "MAGIC"] + ["REG_WRITE"] * 11)
        self.assertEqual([rig.vft.regs[a] for a in (2, 3, 4)], [0xFF] * 3)     # OFF writes not latched (no 7b)
        close = ops(s, "close")[-1]
        self.assertEqual((close["inflight"], close["unreaped"], s.drivers[-1].inflight), (0, 0, 0))
        self.check(s)

    def test_stop_during_activation(self):
        rig, s = self.make()

        def hook(kind, phase):
            if kind == "magic" and phase == "done" and rig.vft.magic_count == 2 and s.state == "ACTIVATING":
                s.driver.abort.set()
        rig.vft.on_xu = hook
        s.run(30)
        self.assertEqual((s.state, len(s.activations), s.store.load_runtime().dirty), ("STOPPED", 1, False))
        names = [op["name"] for op in s.k.ops if op["kind"] == "ctrl" and op["name"] in XU_NAMES[:4]]
        self.assertEqual(names, ACT16 + TD15[:-1])          # the stop waited for STREAM_ON's SET_CUR (usb-1)
        self.assertEqual((ops(s, "close")[-1]["inflight"], ops(s, "ctrl", "COMMIT_SET")), (0, []))
        self.check(s)

    def test_suspend_jump(self):
        rig, s = self.make()
        self.stream_ok(s)
        rig.clock.suspend(30.0)
        self.assertTrue(s.run(60, lambda s: len(s.activations) == 2 and s.state == "STREAMING"))
        self.assertEqual((s.events.count("suspend"), s.failures, s.drivers[0].teardown_path), (1, [], "clean"))
        teardown_t = next(t for st, t in s.states if st == "TEARDOWN")
        self.assertGreaterEqual(s.activations[1]["t"] - teardown_t, 8.0)
        self.done(s)


class Cadence(RigCase):
    """usb-1: Block A stopped or failed between MAGIC #2 and STREAM_ON. A stop waits for STREAM_ON's SET_CUR; a
    failure there makes Block C send the 11 OFF writes and one MAGIC first. With teardown_magic3 the fake's PWM
    0x02-0x04 end at 0 and check_trace holds (the default-config Block C bytes: test_ff_driver BlockC)."""
    STOPS = {"write6": ("reg_write", "done", 6, 0.0001), "sleep": ("reg_write", "done", 11, 0.05),
             "magic3": ("magic", "set", 3, 0.0005)}
    LOSE = {"ack8": ("reg_write", 8), "ackm3": ("magic", 3)}      # the one SET_CUR whose ack is lost

    def block_a(self, at):
        rig, _ = self.make()
        node, seen, t = rig.node(), collections.Counter(), {}
        drv = VftDriver(UsbDevice(rig.kernel, node.devpath), node, rig.clock, None, DriverConfig(teardown_magic3=True),
                        inline=True)
        drv.open()
        rig.kernel.note(strategy="S0", n=64, set_config=False)

        def stop():                                     # a SIGTERM lands: request_stop() sets abort
            t["stop"] = rig.clock.monotonic()
            drv.abort.set()

        def hook(kind, phase):
            seen[kind, phase] += 1
            if (kind, phase, seen[kind, phase]) == self.STOPS.get(at, ())[:3]:
                rig.clock.call_later(self.STOPS[at][3], stop)
            if at in self.LOSE and phase == "set":
                rig.vft.m.ack_never = (kind, seen[kind, phase]) == self.LOSE[at]
        rig.vft.on_xu = hook
        with self.assertRaises(XuTimeout if at in self.LOSE else Aborted):
            drv.activate(STRATEGIES["S0"], 0xFF, 0xB2, 64)
        t["raised"], rig.vft.on_xu, rig.vft.m.ack_never = rig.clock.monotonic(), None, False
        rig.kernel.note(abort=at not in self.LOSE)
        drv.teardown()
        return rig, drv, t, [o["name"] for o in rig.kernel.ops if o["kind"] == "ctrl" and o["name"] in XU_NAMES[:4]]

    def test_stop_or_failure_between_magic2_and_stream_on_ends_with_pwm_0(self):
        for at in ("write6", "sleep", "magic3", "ack8", "ackm3"):
            with self.subTest(at=at):
                rig, drv, t, names = self.block_a(at)
                self.assertEqual([rig.vft.regs.get(a) for a in (2, 3, 4)], [0, 0, 0])
                self.assertEqual((drv.teardown_path, rig.kernel.violations, check_trace(rig.kernel.ops)),
                                 ("clean", [], []))
                if at == "ack8":                        # 8 ON writes; Block C: 11 OFF writes and a MAGIC first
                    self.assertEqual(names, ACT16[:11] + ["REG_WRITE"] * 11 + ["MAGIC"] + TD15)
                else:                                   # the stop took effect after STREAM_ON's SET_CUR; a failed
                    self.assertEqual(names, ACT16 + TD15)   # MAGIC #3 is still followed by STREAM_ON
                    self.assertLess(t["raised"] - t.get("stop", t["raised"]), 0.3)

    def test_check_trace_flags_a_misaligned_block_c(self):
        for at, (a, b), why in (("ack8", (11, 23), "a Block C MAGIC latched IR-on writes"),
                                ("magic3", (15, 16), "IR on after the trailing MAGIC")):
            ops = self.block_a(at)[0].kernel.ops
            sets = [i for i, o in enumerate(ops) if o["kind"] == "ctrl" and o["name"] in XU_NAMES[:4]]
            old = ops[:sets[a]] + ops[sets[b]:]         # no recovery prefix / no STREAM_ON: the old driver's trace
            self.assertIn("(9) Block C's MAGIC cadence misaligned: " + why, "\n".join(check_trace(old)))


class MatrixBudget(RigCase):
    def test_runs_are_deterministic(self):
        traces = []
        for _ in range(2):
            rig, s = self.make(ack_busy_polls=3, epipe_at_s=4.0)
            s.run(6)
            s.stop()
            traces.append([(o["kind"], o.get("name"), o.get("st"), round(o["t"], 9)) for o in rig.kernel.ops])
        self.assertGreater(len(traces[0]), 1000)
        self.assertEqual(traces[0], traces[1])

    def test_zz_matrix_count_and_budget(self):
        names = [n for n in dir(Matrix) if n.startswith("test_")]
        self.assertEqual(len(names), 46)
        native = platform.machine().lower() in ("amd64", "x86_64") and ffutil.fps_asserts()
        if Matrix.elapsed and native:           # the whole matrix ran in this process; timing only natively
            self.assertLess(Matrix.elapsed, 60.0)


if __name__ == "__main__":
    unittest.main()
