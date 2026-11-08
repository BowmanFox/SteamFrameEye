# SPDX-License-Identifier: MIT
"""Trace and replay (spec §11.3 test_ff_replay, task T2): TraceSys over FakeKernel -> ReplayKernel
reproduces the state sequence, frame count and frame sizes; an SS-persona trace replays with n=384.
The runs use `Sup`, the compact supervisor of the driver matrix, from ffsup (it lived here until this
file reached the 600-line limit)."""
import ffutil  # first: installs the port guard

import ctypes as C
import json
import os
import types
import unittest

ffutil.setup_path()
from ffsup import RigCase, Sup  # noqa: E402
from frameface.config import Config  # noqa: E402
from frameface.sim import ReplayKernel, TraceSys, VftModel, make_rig  # noqa: E402
from frameface.usbfs import REAPURBNDELAY, SUBMITURB, Urb, UsbDevice  # noqa: E402


class Replay(RigCase):
    def record(self, persona, seconds=7.0, cfg=None):
        trace, pixels = (os.path.join(self.tmpdir(), n) for n in ("trace.jsonl", "trace-pixels.jsonl"))
        rig = make_rig(VftModel(persona=persona))
        self.addCleanup(rig.close)
        ts = TraceSys(rig.kernel, trace, pixels, clock=rig.clock)
        sup = Sup(rig, self.store(rig), cfg or Config(token=""), sys=ts)
        sup.run(seconds)
        sup.stop()
        ts.close_trace()
        self.check(sup)
        return sup, ts, trace, pixels

    def replay(self, trace, persona, seconds=7.0, cfg=None):
        rig = make_rig(VftModel(persona=persona))             # its sysfs tree; the I/O comes from the trace
        self.addCleanup(rig.close)
        rk = ReplayKernel(trace, rig.clock)
        sup = Sup(rig, self.store(rig), cfg or Config(token=""), sys=rk)
        sup.run(seconds)
        sup.stop()
        return sup, rk

    def test_replay_reproduces_states_frames_and_sizes(self):
        a, _, trace, _ = self.record("hs")
        b, rk = self.replay(trace, "hs")
        names = [s for s, _ in a.states]
        self.assertEqual(names, ["NO_DEVICE", "SETTLING", "OPENING", "ACTIVATING", "NEGOTIATING", "STARTING",
                                 "STREAMING", "TEARDOWN", "STOPPED"])
        self.assertEqual([s for s, _ in b.states], names)
        self.assertGreater(len(a.frame_sizes), 200)
        self.assertEqual(len(b.frame_sizes), len(a.frame_sizes))
        self.assertEqual(b.frame_sizes, a.frame_sizes)
        self.assertEqual(b.drivers[0].stats["urbs_ok"], a.drivers[0].stats["urbs_ok"])
        self.assertEqual(b.counts["ok"], a.counts["ok"])               # scene filler: healthy, never frozen
        self.assertEqual(b.drivers[0].teardown_path, "clean")
        self.assertGreater(rk.completion_events, 2000)

    def test_ss_trace_replays_with_n_384(self):
        a, _, trace, _ = self.record("ss", seconds=5.0)
        self.assertEqual(a.activations[0]["n"], 384)
        b, rk = self.replay(trace, "ss", seconds=5.0)
        self.assertEqual(b.activations[0]["n"], 384)
        self.assertEqual([s for s, _ in b.states], [s for s, _ in a.states])
        xu = [op for op in rk.ops if op["op"] == "ctrl" and op["wi"] == 0x0400 and op["rq"] in (0x01, 0x81)]
        self.assertGreater(len(xu), 30)
        self.assertEqual({op["wl"] for op in xu}, {384})
        self.assertEqual(len(b.frame_sizes), len(a.frame_sizes))

    def test_trace_holds_at_most_16_payload_bytes_and_pixels_go_to_their_file(self):
        sup, ts, trace, pixels = self.record("hs", seconds=4.0)
        with open(trace, encoding="utf-8") as f:
            recs = [json.loads(line) for line in f]
        self.assertEqual((ts.records.maxlen, list(ts.records)), (4096, recs[-4096:]))   # memory: the newest only
        tiny = TraceSys(types.SimpleNamespace(open=lambda p, f: 5), os.path.join(self.tmpdir(), "t.jsonl"), keep=3)
        for _ in range(5):
            tiny.open("x", 0)
        tiny.close_trace()
        self.assertEqual(len(tiny.records), 3)                          # bounded with a file (a long soak) ...
        self.assertIsNone(TraceSys(None).records.maxlen)                # ... and complete without one
        reaps = [r for r in recs if r["op"] == "reap" and r.get("st") == 0 and r.get("len")]
        self.assertGreater(len(reaps), 100)
        for r in recs:
            self.assertNotIn("data", r)
            self.assertLessEqual(len(bytes.fromhex(r.get("hdr", ""))), 16)
        self.assertTrue(all(r["hdr"].startswith("0c") for r in reaps))
        with open(pixels, encoding="utf-8") as f:
            full = [json.loads(line) for line in f]
        self.assertEqual(len(full), len(recs))
        sizes = [len(bytes.fromhex(r["data"])) for r in full if r["op"] == "reap" and r.get("st") == 0 and r.get("len")]
        self.assertEqual(sizes, [r["len"] for r in reaps])
        if os.name == "posix":
            self.assertEqual(os.stat(pixels).st_mode & 0o777, 0o600)
        first = recs[0]
        self.assertEqual(list(first)[:2], ["t", "op"])
        ctrl = next(r for r in recs if r["op"] == "ctrl" and r["rq"] == 0x01 and r["wi"] == 0x0400)
        self.assertTrue(ctrl["out"].startswith("5152"))             # MAGIC #1, full command recorded

    def test_a_trace_reaping_more_than_the_replay_urb_holds_is_refused(self):
        a, _, trace, _ = self.record("hs", seconds=3.0, cfg=Config(token="", urb_bytes=65536))
        rig = make_rig(VftModel())
        self.addCleanup(rig.close)
        rk = ReplayKernel(trace, rig.clock)
        fd, backing = rk.open(rig.devpath, 0), (C.c_ubyte * 131072)()     # room past the URB: nothing at risk
        urb = Urb(type=3, endpoint=0x81, buffer=C.addressof(backing), buffer_length=32768, usercontext=0)
        with self.assertRaises(ValueError) as cm:                         # a 32768-byte URB, 65536-byte reaps
            rk.ioctl(fd, SUBMITURB, C.addressof(urb))
            rig.clock.advance(3.0)
            rk.ioctl(fd, REAPURBNDELAY, C.addressof(C.c_void_p()))
        self.assertIn("65536", str(cm.exception))
        self.assertEqual((urb.actual_length, bytes(backing[32768:])), (0, bytes(98304)))   # nothing past it
        b, _ = self.replay(trace, "hs", seconds=3.0, cfg=Config(token="", urb_bytes=65536))   # the same URB size
        self.assertEqual((b.drivers[0].urb_bytes, b.frame_sizes), (65536, a.frame_sizes))

    def test_replay_without_a_recorded_answer_stalls(self):
        _, _, trace, _ = self.record("hs", seconds=3.0)
        rig = make_rig(VftModel())
        self.addCleanup(rig.close)
        dev = UsbDevice(ReplayKernel(trace, rig.clock), rig.devpath)
        dev.open()
        from frameface import uvc
        with self.assertRaises(OSError) as cm:
            dev.control_in(uvc.xu_setup(uvc.GET_LEN, 2, unit=7))          # never recorded
        self.assertEqual(cm.exception.errno, 32)
        self.assertEqual(dev.control_in(uvc.xu_setup(uvc.GET_LEN, 2)), (64).to_bytes(2, "little"))


if __name__ == "__main__":
    unittest.main()
