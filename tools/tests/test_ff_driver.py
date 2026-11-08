# SPDX-License-Identifier: MIT
"""VftDriver on FakeKernel (spec §11.3 test_ff_driver, task T2): the exact Block A/B/C control traces
(§4.7, §4.8, §4.12), the S3/S4/S5 placements, DISCONNECT_CLAIM, the reaper status table, URB size
halving, inline and threaded streaming, and exactly one completion event per URB."""
import ffutil  # first: installs the port guard

import ctypes as C
import math
import re
import time
import unittest

ffutil.setup_path()
from frameface import uvc  # noqa: E402
from frameface.assembler import BulkReassembler, FrameAssembler, FramePool  # noqa: E402
from frameface.clock import RealClock  # noqa: E402
from frameface.policy import STRATEGIES, Failure as F, xu_len_for  # noqa: E402
from frameface.quality import HealthTracker  # noqa: E402
from frameface.sim import VftModel, check_trace, fakevft, make_rig  # noqa: E402
from frameface.sim.scenes import PHASES, animation_phase  # noqa: E402
from frameface.usbfs import (DISCONNECT_CLAIM, RESET, SETINTERFACE, DeviceGone, DisconnectClaim, SetIf,  # noqa: E402
                             UsbDevice)
from frameface.vft import ActivationError, DriverConfig, VftDriver, XuError, XuTimeout  # noqa: E402

XU_SETS = ("MAGIC", "STREAM_OFF", "STREAM_ON", "REG_WRITE")


class Session:
    """A driver session on a fresh rig: open() done, an activation note added for check_trace."""

    def __init__(self, test, strategy="S0", cfg=None, clock=None, inline=True, **knobs):
        self.rig = make_rig(VftModel(**knobs), clock=clock)
        test.addCleanup(self.rig.close)
        self.k, self.clock, self.s = self.rig.kernel, self.rig.clock, STRATEGIES[strategy]
        node = self.rig.node()
        self.drv = VftDriver(UsbDevice(self.k, node.devpath), node, self.clock, None, cfg or DriverConfig(),
                             inline=inline)
        self.ident = self.drv.open(set_config=self.s.set_config)
        self.n = xu_len_for(strategy, "auto", None, None, self.ident.get_len)
        self.k.note(strategy=strategy, n=self.n, set_config=self.s.set_config)

    def mark(self):
        return len(self.k.ops)

    def activate(self):
        return self.drv.activate(self.s, 0xFF, 0xB2, self.n)

    def negotiate(self):
        return self.drv.negotiate(self.s, 0xFF, 0xB2, self.n)

    def start(self):
        self.params = self.negotiate()
        self.pool = FramePool(320000)
        self.asm = FrameAssembler(320000, self.pool, self.clock)
        self.drv.start(self.params, BulkReassembler(self.asm, self.params.max_payload, self.ident.layout.max_packet))

    def stream(self, seconds):
        self.activate()
        self.start()
        self.run(seconds)

    def run(self, seconds):
        end = self.clock.monotonic() + seconds
        while self.clock.monotonic() < end:
            self.drv.pump(0.01)

    def ctrl(self, start=0):
        return [op for op in self.k.ops[start:] if op["kind"] == "ctrl"]


def xu_rows(cmds, n, polls=3, stream_polls=3):
    """(name, rt, rq, wv, wi, wl, out) of each SET_CUR and its GET_CUR polls, op by op."""
    out = []
    for name, data in cmds:
        out.append((name, 0x21, 0x01, 0x0200, 0x0400, n, data.hex()))
        out += [("XU_GET_CUR", 0xA1, 0x81, 0x0200, 0x0400, n, None)] * (stream_polls if "STREAM" in name else polls)
    return out


def block_a(n, polls=3, stream_polls=3, pwm=0xFF, current=0xB2):
    """Every control request of Block A (§4.7): MAGIC, STREAM_OFF, MAGIC, 11 REG_WRITE, MAGIC, STREAM_ON."""
    cmds = [("MAGIC", uvc.magic(n)), ("STREAM_OFF", uvc.stream_cmd(False, n)), ("MAGIC", uvc.magic(n))]
    cmds += [("REG_WRITE", uvc.reg_write(a, v, n)) for a, v in uvc.ir_regs(pwm, current)]
    cmds += [("MAGIC", uvc.magic(n)), ("STREAM_ON", uvc.stream_cmd(True, n))]
    return xu_rows(cmds, n, polls, stream_polls)


def block_c(n, magic3, polls=3):
    """The control requests of Block C's XU part (§4.12): MAGIC, STREAM_OFF, MAGIC, 11 OFF writes [, MAGIC]."""
    cmds = [("MAGIC", uvc.magic(n)), ("STREAM_OFF", uvc.stream_cmd(False, n)), ("MAGIC", uvc.magic(n))]
    cmds += [("REG_WRITE", uvc.reg_write(a, v, n)) for a, v in uvc.ir_regs(0x00, 0xB2)]
    return xu_rows(cmds + [("MAGIC", uvc.magic(n))] * magic3, n, polls)


def row(op):
    return (op["name"], op["rt"], op["rq"], op["wv"], op["wi"], op["wl"], op["out"])


class BlockA(unittest.TestCase):
    def check_s0(self, n, **knobs):
        se = Session(self, **knobs)
        self.assertEqual(se.n, n)
        m = se.mark()
        act = se.activate()
        ops = se.k.ops[m:]
        self.assertEqual([(o["kind"], o["iface"], o["alt"], o["inflight"]) for o in ops[:1]], [("setif", 1, 0, 0)])
        self.assertEqual([row(o) for o in ops[1:]], block_a(n))
        gets = [o for o in ops if o.get("name") == "XU_GET_CUR"]
        self.assertEqual([o["in"][:2] for o in gets[:3]], ["55", "55", "56"])
        sets = [o for o in ops if o.get("name") in XU_SETS]
        self.assertEqual(gets[2]["in"][2:34], sets[0]["out"][:32])           # echo of the first 16 bytes
        gap = lambda i: sets[i]["t"] - gets[3 * i - 1]["t"]                   # SET_CUR i after the last poll of i-1
        self.assertAlmostEqual(gap(2), 0.25, delta=0.002)                     # MAGIC #2 250 ms after STREAM_OFF
        self.assertAlmostEqual(gap(14), 0.10, delta=0.002)                    # MAGIC #3 100 ms after the writes
        self.assertAlmostEqual(se.clock.monotonic() - gets[-1]["t"], 0.50, delta=0.002)
        self.assertEqual([c["name"] for c in act.cmds],
                         ["MAGIC#1", "STREAM_OFF", "MAGIC#2"] + ["REG %02x=%02x" % r for r in uvc.ir_regs()] +
                         ["MAGIC#3", "STREAM_ON"])
        self.assertTrue(all(c["busy"] == 2 and c["ack"] == "done" and c["echo16"] for c in act.cmds))
        self.assertEqual((act.stream_off_ack, act.stream_on_ack, act.xu_len), ("done", "done", n))
        se.drv.teardown()
        self.assertEqual((se.k.violations, check_trace(se.k.ops)), ([], []))
        return ops

    def test_s0_trace_n64(self):
        ops = self.check_s0(64)
        self.assertNotIn("5354", {o["out"][508:512] for o in ops if o.get("out")})

    def test_s0_trace_n384(self):
        ops = self.check_s0(384, get_len=384)
        magics = [o for o in ops if o.get("name") == "MAGIC"]
        self.assertEqual({o["out"][508:512] for o in magics}, {"5354"})
        self.assertEqual({o["out"][508:512] for o in ops if o.get("name") in ("STREAM_OFF", "REG_WRITE")}, {"0000"})

    def test_s3_block_a_between_probe_get_min_and_commit(self):
        se = Session(self, strategy="S3")
        m = se.mark()
        act = se.activate()
        self.assertEqual(([o["kind"] for o in se.k.ops[m:]], act.cmds), (["setif"], []))
        se.negotiate()
        names = [o.get("name", o["kind"]) for o in se.k.ops[m:]]
        xu = [i for i, n in enumerate(names) if n in XU_SETS]
        self.assertEqual(names[:6], ["setif", "PROBE_GET_CUR", "PROBE_SET", "PROBE_GET_CUR", "PROBE_GET_MAX",
                                     "PROBE_GET_MIN"])
        self.assertEqual((len(xu), names[-2:]), (16, ["COMMIT_SET", "setif"]))
        self.assertTrue(names.index("PROBE_GET_MIN") < xu[0] and xu[-1] < names.index("COMMIT_SET"))
        self.assertEqual(se.drv.last_activation.strategy, "S3")

    def test_s4_get_len_around_every_set_cur(self):
        se = Session(self, strategy="S4")
        m = se.mark()
        se.activate()
        names = [o["name"] for o in se.ctrl(m)]
        sets = [i for i, n in enumerate(names) if n in XU_SETS]
        self.assertEqual(len(sets), 16)
        for i in sets:
            self.assertEqual((names[i - 1], names[i + 1], names[i + 2]), ("XU_GET_LEN", "XU_GET_LEN", "XU_GET_CUR"))
        with self.assertRaises(XuTimeout):                                    # a rhythm-only device ignores S0
            Session(self, rhythm_required=True).activate()
        act = Session(self, strategy="S4", rhythm_required=True).activate()
        self.assertTrue(all(c["ack"] == "done" for c in act.cmds))

    def test_s5_at_get_len_64(self):
        se = Session(self, strategy="S5")
        self.assertEqual([o.get("name", o["kind"]) + str(o.get("iface", "")) for o in se.k.ops[:8]],
                         ["open", "speed", "caps", "setconfig", "claim0", "claim1", "XU_GET_INFO", "XU_GET_LEN"])
        self.assertEqual((se.k.ops[3]["value"], se.ident.get_len, se.n, se.ident.set_config), (1, 64, 384, "ok"))
        m = se.mark()
        act = se.activate()
        se.negotiate()
        ops = se.k.ops[m:]
        exp = block_a(384, stream_polls=0)                  # no si_before; STREAM_OFF/ON are not polled
        self.assertEqual([row(o) for o in ops[:len(exp)]], exp)
        self.assertEqual({o["out"][508:512] for o in ops if o.get("name") == "MAGIC"}, {"5354"})
        self.assertEqual({o["out"][508:512] for o in ops if o.get("name") in ("STREAM_OFF", "STREAM_ON", "REG_WRITE")},
                         {"0000"})
        self.assertEqual([o.get("name", o["kind"]) for o in ops[len(exp):]],     # si_mid before the probe, no si_after
                         ["setif", "PROBE_GET_CUR", "PROBE_SET", "PROBE_GET_CUR", "PROBE_GET_MAX", "PROBE_GET_MIN",
                          "COMMIT_SET"])
        self.assertAlmostEqual(ops[len(exp)]["t"] - ops[len(exp) - 1]["t"], 0.5, delta=0.002)
        self.assertEqual((act.stream_off_ack, act.stream_on_ack), ("not_polled", "not_polled"))
        bound = Session(self, strategy="S5", kernel_driver="uvcvideo")    # EBUSY from a bound driver: WARN, go on
        self.assertEqual(bound.ident.set_config, "error:EBUSY")


class BlockB(unittest.TestCase):
    def test_negotiation_trace(self):
        se = Session(self)
        se.activate()
        m = se.mark()
        p = se.negotiate()
        ops = se.k.ops[m:]
        self.assertEqual([(o.get("name", o["kind"]), o.get("wl")) for o in ops],
                         [("PROBE_GET_CUR", 34), ("PROBE_SET", 34), ("PROBE_GET_CUR", 34), ("PROBE_GET_MAX", 34),
                          ("PROBE_GET_MIN", 34), ("COMMIT_SET", 34), ("setif", None)])
        answer = bytes.fromhex(ops[0]["in"])
        self.assertEqual(ops[1]["out"], ("010001010a8b0200" + answer[8:].hex()))
        g, _ = uvc.fix_probe(uvc.unpack_probe(bytes.fromhex(ops[2]["in"])), 320000)
        self.assertEqual(ops[5]["out"], uvc.pack_probe(g, 34).hex())
        self.assertEqual((p.probe_len, p.max_frame, p.max_payload, p.interval, p.framing_info),
                         (34, 320000, 32768, 166666, 3))

    def test_negotiation_epipe_retries(self):
        se = Session(self, probe_stall_lens=(34, 48), commit_stall_lens=(26, 48))
        se.activate()
        m = se.mark()
        p = se.negotiate()
        got = [(o["name"], o["wl"], o.get("err")) for o in se.ctrl(m)]
        stall = [("ERROR_CODE", 1, None)]
        self.assertEqual(got, [("PROBE_GET_CUR", 34, None), ("PROBE_SET", 34, "EPIPE")] + stall +
                         [("PROBE_GET_CUR", 48, None), ("PROBE_SET", 48, "EPIPE")] + stall +
                         [("PROBE_GET_CUR", 26, None), ("PROBE_SET", 26, None), ("PROBE_GET_CUR", 26, None),
                          ("PROBE_GET_MAX", 26, None), ("PROBE_GET_MIN", 26, None), ("COMMIT_SET", 26, "EPIPE")] +
                         stall + [("COMMIT_SET", 48, "EPIPE")] + stall + [("COMMIT_SET", 34, None)])
        self.assertEqual(p.probe_len, 26)


class BlockC(unittest.TestCase):
    def teardown_trace(self, magic3):
        se = Session(self, cfg=DriverConfig(teardown_magic3=magic3))
        se.stream(1.0)
        m, readback = se.mark(), []

        def hook(d):
            se.k.note(hook=True)
            readback.extend(d.xu(uvc.reg_read(a, d.n), 200)[1][17] for a in (2, 3, 4))
        se.drv.teardown(after_xu=hook)
        ops = se.k.ops[m:]
        pos = {k: next(i for i, o in enumerate(ops) if f(o)) for k, f in (
            ("hook", lambda o: o.get("hook")), ("discard", lambda o: o["kind"] == "discard"),
            ("stream_off", lambda o: o.get("name") == "STREAM_OFF"))}
        xu_part = [o for o in ops[:pos["hook"]] if o["kind"] == "ctrl"]
        self.assertEqual([row(o) for o in xu_part], block_c(64, magic3))     # steps 3-7 (7b) op by op
        self.assertEqual([(o["kind"], o.get("iface")) for o in ops[pos["hook"]:]
                          if o["kind"] not in ("ctrl", "reap", "submit")],     # hook, steps 9, 11-13
                         [("note", None)] + [("discard", None)] * 16 +
                         [("clear_halt", None), ("release", 1), ("release", 0), ("close", None)])
        self.assertGreater(sum(1 for o in ops[:pos["stream_off"]] if o["kind"] == "submit"), 0)   # still resubmitting
        sets = [o for o in xu_part if o["name"] != "XU_GET_CUR"]
        polls = lambda i: [o for o in xu_part if o["name"] == "XU_GET_CUR" and o["t"] > sets[i]["t"]][2]["t"]
        self.assertAlmostEqual(sets[0]["t"] - ops[0]["t"], 0.03, delta=0.006)                 # step 2: 30 ms
        self.assertAlmostEqual(sets[2]["t"] - polls(1), 0.10, delta=0.002)                     # step 5: 100 ms
        after = sets[14]["t"] if magic3 else ops[pos["hook"]]["t"]
        self.assertAlmostEqual(after - polls(13), 0.20, delta=0.002)                           # step 7: 200 ms
        self.assertEqual((ops[-1]["kind"], ops[-1]["inflight"], ops[-1]["unreaped"]), ("close", 0, 0))
        self.assertEqual((se.drv.teardown_path, se.drv.inflight), ("clean", 0))
        self.assertEqual((se.k.violations, check_trace(se.k.ops, service=False)), ([], []))
        return readback

    def test_teardown_order_and_after_xu(self):
        self.assertEqual(self.teardown_trace(False), [0xFF] * 3)            # OFF writes buffered, not latched

    def test_teardown_magic3(self):
        self.assertEqual(self.teardown_trace(True), [0x00] * 3)            # the 7b MAGIC latches PWM 0

    def test_teardown_aborts_xu_part_after_first_timeout(self):
        se = Session(self)
        se.stream(1.0)
        se.rig.vft.m.ack_never = True                                       # the device stops acknowledging
        m, t0 = se.mark(), time.perf_counter()
        se.drv.teardown()
        ops = se.k.ops[m:]
        self.assertEqual([o["name"] for o in ops if o.get("name") in XU_SETS], ["MAGIC"])
        polls = [o for o in ops if o.get("name") == "XU_GET_CUR"]
        set_t = next(o["t"] for o in ops if o.get("name") == "MAGIC")
        self.assertAlmostEqual(polls[-1]["t"] - set_t, 0.2, delta=0.003)
        self.assertEqual([o["kind"] + str(o.get("iface", "")) for o in ops if o["kind"] in ("clear_halt", "release",
                                                                                           "close")],
                         ["clear_halt", "release1", "release0", "close"])
        self.assertEqual((se.drv.teardown_path, ops[-1]["inflight"]), ("clean", 0))
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual((se.k.violations, check_trace(se.k.ops)), ([], []))

    def test_teardown_on_a_gone_device_takes_the_lost_path(self):
        se = Session(self)
        se.stream(0.5)
        se.k.unplug()
        with self.assertRaises(DeviceGone):
            se.run(0.1)
        m = se.mark()
        se.drv.teardown(clean=True)
        ops = se.k.ops[m:]
        self.assertEqual([o for o in ops if o["kind"] == "ctrl"], [])
        self.assertEqual((se.drv.teardown_path, ops[-1]["kind"], se.drv.inflight), ("lost", "close", 0))
        self.assertEqual((se.k.violations, check_trace(se.k.ops)), ([], []))


class Claims(unittest.TestCase):
    def test_disconnect_claim_takes_kernel_driver(self):
        se = Session(self, kernel_driver="uvcvideo")
        dcs = [(o["iface"], o["flags"], o["driver"], o.get("err")) for o in se.k.ops if o["kind"] == "disconnect_claim"]
        self.assertEqual(dcs, [(0, 2, "usbfs", None), (1, 2, "usbfs", None)])
        self.assertEqual([o["driver"] for o in se.k.ops if o["kind"] == "getdriver"], ["uvcvideo"] * 2)

    def test_disconnect_claim_never_steals_from_usbfs(self):
        rig = make_rig(VftModel(other_usbfs_owner=(812, "vftstream")))
        self.addCleanup(rig.close)
        node = rig.node()
        drv = VftDriver(UsbDevice(rig.kernel, node.devpath), node, rig.clock, None, DriverConfig(), inline=True)
        with self.assertRaises(ActivationError) as cm:
            drv.open()
        self.assertEqual((cm.exception.failure, cm.exception.info), (F.BUSY, {"interface": 1, "driver": "usbfs"}))
        kinds = [(o["kind"], o.get("iface"), o.get("flags"), o.get("err")) for o in rig.kernel.ops[1:]]
        self.assertIn(("disconnect_claim", 1, 2, "EBUSY"), kinds)
        self.assertEqual(kinds[-2:], [("release", 0, None, None), ("close", None, None, None)])
        self.assertEqual(rig.kernel.violations, [])
        dev = UsbDevice(rig.kernel, node.devpath)                           # the fake catches a steal
        dev.open()
        dc = DisconnectClaim(interface=1, flags=0, driver=b"usbfs")
        rig.kernel.ioctl(dev.fd, DISCONNECT_CLAIM, C.addressof(dc))
        self.assertTrue(rig.kernel.violations[0].startswith("steal_from_usbfs_owner"))


class Pump(unittest.TestCase):
    def test_reaper_status_table(self):
        se = Session(self)
        se.stream(0.3)
        drv, k = se.drv, se.k
        self.assertGreater(drv.stats["urbs_ok"], 50)
        self.assertGreater(drv.stats["bytes"], 50 * 32000)

        def force(status):
            before = dict(drv.stats)
            halts = sum(1 for o in k.ops if o["kind"] == "clear_halt")
            k.force_status(status)
            for _ in range(20):
                drv.pump(0.01)
                if not k._forced:
                    break
            return {key: drv.stats[key] - before[key] for key in before if isinstance(before[key], int)}, \
                sum(1 for o in k.ops if o["kind"] == "clear_halt") - halts
        d, halts = force(-32)
        self.assertEqual((d["epipe"], halts, drv.inflight), (1, 1, 16))
        for status, key in ((-71, "eproto"), (-84, "eilseq"), (-62, "etime"), (-75, "eoverflow"), (-5, "other")):
            bad = se.asm.stats["bad_err"]
            d, halts = force(status)
            self.assertEqual((d[key], halts, drv.inflight), (1, 0, 16), key)
            se.run(0.05)
            self.assertGreater(se.asm.stats["bad_err"], bad, key)           # asm.error(): that frame is bad
        for status, inflight in ((-2, 15), (-104, 14)):
            d, _ = force(status)
            self.assertEqual((d["discarded"], drv.inflight), (1, inflight))
        k.force_status(-19)
        with self.assertRaises(DeviceGone):
            se.run(0.1)
        self.assertTrue(drv.gone)
        drv.urbs.gone = False
        k.force_status(-108)                                                # -ESHUTDOWN alone: flushed, not gone
        se.run(1.0)
        self.assertEqual((drv.gone, drv.urbs.flushed, drv.stats["flushed"], drv.inflight), (False, True, 1, 0))

    def test_urb_size_halving_einval(self):
        se = Session(self, urb_max_bytes=16384)
        se.stream(0.5)
        self.assertEqual((se.drv.urb_bytes, se.drv.inflight), (16384, 16))
        self.assertEqual([(o["len"], o.get("err")) for o in se.k.ops if o["kind"] == "submit"][:2],
                         [(32768, "EINVAL"), (16384, None)])
        self.assertGreater(se.asm.stats["frames_ok"], 20)

    def test_urb_size_halving_enomem(self):
        se = Session(self, usbfs_memory_bytes=300000)
        se.stream(0.5)
        subs = [(o["len"], o.get("err")) for o in se.k.ops if o["kind"] == "submit"]
        self.assertEqual(subs[:10], [(32768, None)] * 9 + [(32768, "ENOMEM")])
        self.assertEqual(sum(1 for o in se.k.ops if o["kind"] == "discard"), 9)
        self.assertEqual((se.drv.urb_bytes, se.drv.inflight, len(se.drv.slots)), (16384, 16, 32))
        self.assertGreater(se.asm.stats["frames_ok"], 20)
        se.drv.teardown()
        self.assertEqual((se.k.violations, check_trace(se.k.ops)), ([], []))

    def test_inline_streams(self):
        se = Session(self)
        se.stream(2.0)
        self.assertGreaterEqual(se.asm.stats["frames_ok"], 110)
        self.assertEqual(se.drv.urbs.reaper, None)
        se.drv.teardown()
        self.assertEqual((se.k.violations, check_trace(se.k.ops)), ([], []))

    def test_threaded_streams(self):
        se = Session(self, clock=RealClock(), inline=False, ack_busy_polls=0)
        se.activate()
        se.start()
        reaper = se.drv.urbs.reaper
        self.assertTrue(reaper is not None and reaper.is_alive() and reaper.name == "reaper")
        got, end = 0, time.monotonic() + 5.0
        while got < 30 and time.monotonic() < end:
            got += se.pool.take(0.5) is not None
        self.assertGreaterEqual(got, 30)
        se.drv.teardown()
        self.assertEqual((reaper.is_alive(), se.drv.urbs.reaper, se.drv.inflight), (False, None, 0))
        self.assertEqual((se.k.violations, check_trace(se.k.ops)), ([], []))

    def test_one_completion_event_per_urb(self):
        self.assertEqual(math.ceil(320000 / (32768 - 12)), 10)
        for urb, per_frame in ((0, 10), (16384, 20), (65536, 5)):
            se = Session(self, cfg=DriverConfig(urb_bytes=urb))
            se.stream(1.0)
            frames, events = se.rig.vft.frames_emitted, se.k.completion_events
            self.assertGreater(frames, 50)
            self.assertLessEqual(abs(events - frames * per_frame), per_frame, urb)
            self.assertLessEqual(se.clock.callbacks_run, events + 3)        # no packet-level events at all

    def test_fake_detects_rule_breaks(self):
        """The violation detectors fire (so `violations == []` elsewhere means something)."""
        a = Session(self)
        a.stream(0.2)
        fd = a.drv.dev.fd
        a.k.ioctl(fd, SETINTERFACE, C.addressof(SetIf(interface=1, altsetting=0)))
        a.k.close(fd)
        with self.assertRaises(OSError):
            a.k.ioctl(fd, RESET, 0)
        self.assertEqual([v.split(":")[0] for v in a.k.violations],
                         ["setinterface_with_urbs_inflight", "ioctl_after_close"])
        b = Session(self, sysname="1-1.2")
        b.stream(0.2)
        other = UsbDevice(b.k, b.rig.node().devpath)                       # a fresh fd, as reset_port uses
        other.open()
        other.reset()
        self.assertEqual([v.split(":")[0] for v in b.k.violations], ["reset_while_streaming", "reset_behind_hub"])
        with self.assertRaises(DeviceGone):                                 # the reset re-enumerates the device
            b.run(0.1)
        b.drv.teardown(clean=False)
        with self.assertRaises(RuntimeError) as cm:                         # the driver itself refuses a hub port
            b.drv.reset_port()
        self.assertIn("behind a hub", str(cm.exception))

    def test_get_len_invalid_or_stalling_is_unsupported(self):
        def opened(rig):
            node = rig.node()
            drv = VftDriver(UsbDevice(rig.kernel, node.devpath), node, rig.clock, None, DriverConfig(), inline=True)
            with self.assertRaises(ActivationError) as cm:
                drv.open()
            self.assertEqual((cm.exception.failure, rig.kernel.ops[-1]["kind"]), (F.UNSUPPORTED, "close"))
            return [(o["name"], o.get("err")) for o in rig.kernel.ops if o["kind"] == "ctrl"]
        for value in (8, 2000):                                             # outside 16..1024
            rig = make_rig(VftModel(get_len=value))
            self.addCleanup(rig.close)
            self.assertEqual(opened(rig), [("XU_GET_INFO", None), ("XU_GET_LEN", None)])
        rig = make_rig(VftModel())
        self.addCleanup(rig.close)
        control = rig.vft.control

        def stall_get_len(rt, rq, wv, wi, wl, data):
            if rq == uvc.GET_LEN:
                rig.vft.last_error = 6
                raise OSError(32, "stall")
            return control(rt, rq, wv, wi, wl, data)
        rig.vft.control = stall_get_len
        self.assertEqual(opened(rig), [("XU_GET_INFO", None), ("XU_GET_LEN", "EPIPE"), ("ERROR_CODE", None),
                                       ("XU_GET_LEN", "EPIPE"), ("ERROR_CODE", None)])
        lens = [o["t"] for o in rig.kernel.ops if o.get("name") == "XU_GET_LEN"]
        self.assertAlmostEqual(lens[1] - lens[0], 0.1, delta=0.002)       # STALL twice, 100 ms apart

    def test_knob_stall_wrong_len(self):
        se = Session(self, strategy="S5", stall_wrong_len=True)          # 384-byte commands on a 64 persona
        with self.assertRaises(XuError) as cm:
            se.activate()
        self.assertEqual(cm.exception.code, 4)                              # REQUEST_ERROR_CODE: out of range
        magic = next(o for o in se.k.ops if o.get("name") == "MAGIC")
        self.assertEqual((magic["err"], magic["wrong_xu_length"]), ("EPIPE", True))

    def test_knob_ir_off_dark(self):
        for ir_off_dark, kind in ((True, "dark"), (False, "ok")):
            se = Session(self, magic_needed=4, ir_off_dark=ir_off_dark)    # the writes never latch
            se.stream(0.3)
            self.assertEqual(se.rig.vft.regs, {})
            self.assertEqual(HealthTracker(1).classify(se.pool.take(0)[0]).kind, kind)

    def test_knob_reset_clears_wedge(self):
        for clears in (True, False):
            se = Session(self, reset_clears_wedge=clears)
            se.stream(0.3)
            se.drv.dev.close()                                              # closed while streaming: wedged
            self.assertTrue(se.rig.vft.wedged)
            se.drv.reset_port()
            se.clock.advance(1.5)
            self.assertEqual(se.rig.vft.wedged, not clears)
            node = se.rig.node()
            drv = VftDriver(UsbDevice(se.k, node.devpath), node, se.clock, None, DriverConfig(), inline=True)
            drv.open()
            if clears:
                self.assertEqual(drv.activate(se.s, 0xFF, 0xB2, 64).cmds[0]["ack"], "done")
            else:
                with self.assertRaises(XuTimeout):
                    drv.activate(se.s, 0xFF, 0xB2, 64)

    def test_knob_start_deadline(self):
        se = Session(self)
        se.activate()
        params = se.negotiate()
        se.clock.sleep(1.2)                                                 # the URBs come 1.2 s after COMMIT
        asm = FrameAssembler(320000, FramePool(320000), se.clock)
        se.drv.start(params, BulkReassembler(asm, params.max_payload, 512))
        se.run(1.0)
        self.assertEqual((se.k.completion_events, se.drv.stats["bytes"]), (0, 0))  # the device idles

    def test_xu_error_and_reg_read(self):
        se = Session(self, reg_read_supported=False)
        se.activate()
        with self.assertRaises(XuError) as cm:
            se.drv.xu(uvc.reg_read(0x05, se.n), 200)
        self.assertEqual(cm.exception.code, 0xFF)
        ok = Session(self)
        ok.activate()
        ack, reply = ok.drv.xu(uvc.reg_read(0x05, ok.n), 200)
        self.assertEqual((ack, reply[17], len(reply)), ("done", 0xB2, 64))

    def test_s5_stream_commands_are_tolerated_unpolled(self):
        se = Session(self, strategy="S5")
        control = se.rig.vft.control

        def stall_stream_off(rt, rq, wv, wi, wl, data):
            if rq == uvc.SET_CUR and data and bytes(data[:4]) == uvc.STREAM_OFF:
                se.rig.vft.last_error = 2
                raise OSError(32, "stall")
            return control(rt, rq, wv, wi, wl, data)
        se.rig.vft.control = stall_stream_off
        act = se.activate()                                                 # §4.7: STREAM_OFF tolerated (S5: unpolled)
        self.assertEqual((act.stream_off_ack, act.stream_on_ack, act.cmds[2]["name"]), ("error:02", "not_polled",
                                                                                        "MAGIC#2"))
        se.drv.teardown()                                                   # Block C step 4 tolerates it too
        self.assertEqual(sum(1 for o in se.ctrl() if o["name"] == "REG_WRITE"), 22)
        self.assertEqual((se.k.violations, check_trace(se.k.ops)), ([], []))

    def test_a_reused_driver_starts_each_session_fresh(self):
        se = Session(self)
        se.stream(0.5)
        se.drv.teardown()
        first = se.drv.stats["urbs_ok"]
        se.clock.advance(9.0)                                               # the fake's 8 s settle
        t_open = se.clock.monotonic()
        se.drv.open()
        se.k.note(strategy="S0", n=se.n, set_config=False)
        self.assertEqual((se.drv.teardown_path, se.drv.stats["urbs_ok"], se.drv.urbs.first_data_t), (None, 0, None))
        se.stream(0.5)
        self.assertGreater(se.drv.urbs.first_data_t, t_open)               # NO_DATA stays detectable
        self.assertLess(abs(se.drv.stats["urbs_ok"] - first), first // 4)  # this session's counters only
        se.drv.teardown()
        self.assertEqual((se.drv.teardown_path, se.k.violations, check_trace(se.k.ops)), ("clean", [], []))

    def test_real_clock_rig_prerenders_its_frames(self):
        calls, render = [], fakevft.make_scene_frame
        fakevft.make_scene_frame = lambda *a: calls.append(a) or render(*a)
        self.addCleanup(setattr, fakevft, "make_scene_frame", render)
        make_rig(VftModel(scene="noise", layout="h2_full"), clock=RealClock()).close()
        self.assertEqual({(s, animation_phase(s, t), layout) for s, t, layout in calls},  # no render in a completion
                         {("noise", p, "h2_full") for p in range(PHASES)} | {("dark", 0, "h2_full"), ("bars", 0, "h2_full")})
        calls.clear()
        make_rig(VftModel()).close()                                        # SimClock: virtual time, nothing to warm
        self.assertEqual(calls, [])


class Invariants(unittest.TestCase):
    """check_trace() reports each of the 13 invariants of §11.3, so `check_trace(...) == []` is not vacuous."""

    def test_each_invariant_is_detected(self):
        se = Session(self)
        se.stream(0.3)
        se.drv.teardown()
        good, fd = se.k.ops, se.k.ops[0]["fd"]
        self.assertEqual((se.k.violations, check_trace(good)), ([], []))
        at = lambda t, f, i=0: [j for j, o in enumerate(t) if f(o)][i]
        name = lambda x: lambda o: o.get("name") == x
        kind = lambda x: lambda o: o["kind"] == x
        iface = lambda k, i: lambda o: o["kind"] == k and o.get("iface") == i

        def broken(k, mutate, service=True):
            t = [dict(o) for o in good]
            mutate(t)
            got = check_trace(t, service)
            self.assertIn(k, {int(m) for m in re.findall(r"^fd \d+ \((\d+)\)", "\n".join(got), re.M)}, got[:3])
            return t

        def op(t, before, **kv):
            t.insert(before, dict({"t": t[before]["t"], "fd": fd}, **kv))
        broken(1, lambda t: t.pop(at(t, iface("claim", 1))))
        broken(2, lambda t: [o.update(name="XU_GET_INFO") for o in t if o.get("name") == "XU_GET_LEN"])
        broken(3, lambda t: t.pop(at(t, name("MAGIC"), 2)))
        rr = broken(4, lambda t: op(t, at(t, name("COMMIT_SET")), kind="ctrl", name="REG_READ", rt=0x21, rq=1, wv=0x200,
                                    wi=0x400, wl=64, out=uvc.reg_read(2, 64).hex()))
        self.assertEqual(check_trace(rr, service=False), [])               # the probe's readbacks are allowed
        broken(5, lambda t: t[at(t, name("MAGIC"))].update(wl=384))
        broken(6, lambda t: t.insert(at(t, name("STREAM_ON")), t.pop(at(t, name("COMMIT_SET")))))
        broken(7, lambda t: t[at(t, kind("submit"))].update(t=t[at(t, kind("submit"))]["t"] + 0.2))
        broken(8, lambda t: t[at(t, kind("setif"), 1)].update(inflight=3))
        broken(9, lambda t: t.pop(at(t, iface("release", 0))))
        broken(9, lambda t: t[-1].update(inflight=2))
        broken(9, lambda t: t.insert(at(t, name("MAGIC"), 3), t.pop(at(t, kind("discard")))))
        broken(10, lambda t: op(t, at(t, name("MAGIC"), 3), kind="enodev", request=0))
        broken(11, lambda t: op(t, at(t, kind("claim")), kind="disconnect_claim", iface=1, flags=0, driver="usbfs"))
        broken(12, lambda t: op(t, at(t, kind("discard")), kind="reset", sysname="1-1", inflight=4))
        broken(12, lambda t: op(t, at(t, kind("close")), kind="reset", sysname="1-1.2", inflight=0))
        broken(13, lambda t: op(t, at(t, name("XU_GET_INFO")), kind="setconfig", value=1))


if __name__ == "__main__":
    unittest.main()
