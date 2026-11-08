# SPDX-License-Identifier: MIT
"""Bulk reassembly, frame assembly, stride learning and the frame pool (spec §4.10; §11.3
test_ff_assembler, task T1). The wire model cuts payloads into URBs the way usbfs completes bulk
URBs: a URB completes when full or when a short packet (< 512 B) ends the transfer."""
import ffutil  # first: installs the port guard

import ctypes
import random
import struct
import threading
import time
from types import SimpleNamespace
import unittest

ffutil.setup_path()
from frameface.assembler import (STAT_KEYS, BulkReassembler, FpsMeter, FrameAssembler,  # noqa: E402
                                 FrameInfo, FramePool, StrideLearner)

FRAME = 320000
STRIDE = 32768


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def monotonic(self):
        return self.t

    boottime = monotonic

    def sleep(self, s):
        self.t += s


def frame_data(seed, size=FRAME):
    return random.Random(seed).randbytes(size)


def header(fid, pts, eof=False, err=False, scr=0):
    flags = 0x0C | fid | (0x02 if eof else 0) | (0x40 if err else 0)
    return bytes([12, flags]) + struct.pack("<IIH", pts, scr & 0xFFFFFFFF, scr & 0x7FF)


def payloads(data, stride, fid, pts, eof=True, err_at=None, make_header=header):
    """`data` as the device sends it: payloads of `stride` bytes (header + data), the last shorter."""
    out, pos, i = [], 0, 0
    while pos < len(data):
        h = make_header(fid, pts, eof=False, err=(i == err_at), scr=i)
        part = data[pos:pos + stride - len(h)]
        pos += len(part)
        if pos >= len(data):
            h = make_header(fid, pts, eof=eof, err=(i == err_at), scr=i)
        out.append(h + part)
        i += 1
    return out


def urbs(pls, urb_len, packet=512):
    """The completed URBs of a host with urb_len-byte URBs (a short packet ends a transfer); a payload
    that is a multiple of `packet` and leaves a URB open is not completed here."""
    out, cur = [], bytearray()
    for p in pls:
        i = 0
        while i < len(p):
            take = min(urb_len - len(cur), len(p) - i)
            cur += p[i:i + take]
            i += take
            if len(cur) == urb_len:
                out.append(bytes(cur))
                cur = bytearray()
        if len(p) % packet and cur:
            out.append(bytes(cur))
            cur = bytearray()
    assert not cur, "a URB was left open"
    return out


def ctypes_view(u):
    return memoryview((ctypes.c_ubyte * len(u)).from_buffer_copy(u))     # what the driver feeds ('<B')


def rig(max_payload=STRIDE, frame=FRAME, strict=True):
    clock = Clock()
    pool = FramePool(frame)
    asm = FrameAssembler(frame, pool, clock, strict)
    events = []
    asm.on_event = lambda name, kv: events.append((name, kv))
    return SimpleNamespace(clock=clock, pool=pool, asm=asm, reasm=BulkReassembler(asm, max_payload), events=events)


def feed(r, url, urb_len, view=memoryview):
    for u in url:
        r.reasm.feed(view(u), len(u), urb_len)


def stats(r, **expected):
    return {k: r.asm.stats[k] for k in STAT_KEYS}, dict({k: 0 for k in STAT_KEYS}, **expected)


class Framing(unittest.TestCase):
    def check_stats(self, r, **expected):
        got, want = stats(r, **expected)
        self.assertEqual(got, want)

    def test_wire_layout(self):
        pls = payloads(frame_data(1), STRIDE, 1, 5)
        self.assertEqual([len(p) for p in pls], [32768] * 9 + [25208])
        self.assertEqual(sum(len(p) for p in pls), 320120)
        self.assertEqual(25208, 49 * 512 + 120)
        self.assertEqual([p[1] for p in pls], [0x0D] * 9 + [0x0F])           # A.4: 0c 0d ... last 0c 0f

    def test_urb_sizes_32k_16k_64k(self):
        for urb_len, per_frame, view in ((32768, 10, memoryview), (16384, 20, ctypes_view), (65536, 5, memoryview)):
            with self.subTest(urb_len=urb_len):
                r = rig()
                for k in range(3):
                    data, pts = frame_data(k), 1000 + 2500000 * k
                    url = urbs(payloads(data, STRIDE, k & 1, pts), urb_len)
                    self.assertEqual(len(url), per_frame)
                    if urb_len == 65536:                                      # a header inside the URB
                        self.assertEqual(url[0][32768:32770], bytes([12, 0x0C | (k & 1)]))
                    feed(r, url, urb_len, view)
                    buf, info = r.pool.take(0)
                    self.assertIs(type(buf), bytearray)
                    self.assertTrue(buf == data)
                    self.assertEqual(info, FrameInfo(seq=k + 1, t_mono=r.clock.t, pts=pts, size=FRAME, why="eof"))
                self.check_stats(r, frames_ok=3)

    def test_payload_16384(self):
        r = rig(max_payload=16384)
        for k in range(2):
            data = frame_data(10 + k)
            pls = payloads(data, 16384, k & 1, 77 + k)
            self.assertEqual([len(p) for p in pls], [16384] * 19 + [8944])
            url = urbs(pls, 32768)
            self.assertEqual([len(u) for u in url], [32768] * 9 + [25328])   # two payloads per URB
            feed(r, url, 32768)
            self.assertTrue(r.pool.take(0)[0] == data)
        self.check_stats(r, frames_ok=2)

    def test_zero_length_packet_ends_the_payload(self):
        size = 9 * 32756 + 16372                                             # last payload exactly 16384 B
        r = rig(frame=size)
        data = frame_data(3, size)
        pls = payloads(data, STRIDE, 0, 9)
        self.assertEqual(len(pls[-1]), 16384)
        feed(r, urbs(pls, 16384), 16384)
        self.assertIsNone(r.pool.take(0), "the last payload is still open")
        r.reasm.feed(memoryview(b""), 0, 16384)                              # ZLP
        buf, info = r.pool.take(0)
        self.assertEqual((bytes(buf) == data, info.why, info.size), (True, "eof", size))
        r.reasm.feed(memoryview(b""), 0, 16384)                              # a ZLP with nothing open
        self.check_stats(r, frames_ok=1)

    def test_short_urbs(self):
        r = rig()
        data = frame_data(4)
        pls = payloads(data, 1000, 1, 1234)                                  # every payload ends short
        url = urbs(pls, 32768)
        self.assertEqual(len(url), len(pls))
        feed(r, url, 32768)
        buf, info = r.pool.take(0)
        self.assertEqual((buf == data, info.pts), (True, 1234))
        self.check_stats(r, frames_ok=1)

    def test_err_bit(self):
        r = rig()
        feed(r, urbs(payloads(frame_data(5), STRIDE, 0, 1, err_at=4), 32768), 32768)
        self.assertIsNone(r.pool.take(0))
        data = frame_data(6)
        feed(r, urbs(payloads(data, STRIDE, 1, 2), 32768), 32768)
        self.assertTrue(r.pool.take(0)[0] == data)
        self.check_stats(r, frames_ok=1, bad_err=1)

    def test_transfer_error_marks_the_frame_bad(self):
        r = rig()
        url = urbs(payloads(frame_data(7), STRIDE, 0, 1), 32768)
        feed(r, url[:3], 32768)
        r.asm.error()                                                        # e.g. -EPROTO on a URB
        feed(r, url[3:], 32768)
        self.check_stats(r, bad_err=1)

    def test_fid_toggle_without_eof_accepted_at_full_size(self):
        # The FID rule on its own: a full frame whose last payload end was not seen is finished by the
        # next frame's toggled FID and accepted.
        r = rig()
        data = frame_data(8)
        for i, p in enumerate(payloads(data, STRIDE, 0, 42, eof=False)):
            self.assertEqual(r.asm.begin(p, 0, len(p)), 12)
            r.asm.data(p, 12, len(p) - 12)
            if i < 9:
                r.asm.end(p[1])
        self.assertIsNone(r.pool.take(0))
        nxt = header(1, 43)
        self.assertEqual(r.asm.begin(nxt, 0, 12), 12)
        buf, info = r.pool.take(0)
        self.assertEqual((buf == data, info.why, info.pts, info.size), (True, "fid", 42, FRAME))
        self.check_stats(r, frames_ok=1, fid_breaks=1)

    def test_entry_points_take_ctypes_views(self):
        # begin/data/end called directly with the '<B' views that usbfs URB buffers export
        r = rig()
        data = frame_data(91)
        for p in payloads(data, STRIDE, 1, 7):
            v = ctypes_view(p)
            self.assertEqual(r.asm.begin(v, 0, len(p)), 12)
            r.asm.data(v, 12, len(p) - 12)
            r.asm.end(p[1])
        buf, info = r.pool.take(0)
        self.assertEqual((buf == data, info.why, info.pts), (True, "eof", 7))

    def test_fid_toggle_ends_a_partial_frame(self):
        r = rig()
        feed(r, urbs(payloads(frame_data(9), STRIDE, 0, 1, eof=False)[:5], 32768), 32768)
        feed(r, urbs(payloads(frame_data(10), STRIDE, 1, 2), 32768), 32768)
        self.check_stats(r, frames_ok=1, fid_breaks=1, bad_size=1)

    def test_eof_less_size_completion(self):
        r = rig()
        for k in range(3):
            data = frame_data(20 + k)
            feed(r, urbs(payloads(data, STRIDE, k & 1, 100 + k, eof=False), 32768), 32768)
            buf, info = r.pool.take(0)
            self.assertEqual((buf == data, info.why, info.seq), (True, "size", k + 1))
        self.check_stats(r, frames_ok=3)

    def test_overflow(self):
        r = rig()
        feed(r, urbs(payloads(frame_data(30, FRAME + 1000), STRIDE, 0, 1), 32768), 32768)
        self.assertIsNone(r.pool.take(0))
        data = frame_data(31)
        feed(r, urbs(payloads(data, STRIDE, 1, 2), 32768), 32768)
        self.assertTrue(r.pool.take(0)[0] == data)
        self.check_stats(r, frames_ok=1, overflow=1, bad_size=1)

    def test_first_partial_frame_dropped(self):
        r = rig()
        feed(r, urbs(payloads(frame_data(40), STRIDE, 0, 1)[6:], 32768), 32768)     # joined mid-frame
        for k in (1, 2):
            data = frame_data(40 + k)
            feed(r, urbs(payloads(data, STRIDE, k & 1, 1 + k), 32768), 32768)
            self.assertTrue(r.pool.take(0)[0] == data)
        self.check_stats(r, frames_ok=2, bad_size=1)

    def test_614400_counted_not_published(self):
        r = rig()
        data = frame_data(49)
        feed(r, urbs(payloads(data, STRIDE, 1, 1), 32768), 32768)          # the device ends frames with EOF
        self.assertTrue(r.pool.take(0)[0] == data)
        feed(r, urbs(payloads(frame_data(50, 614400), STRIDE, 0, 2), 32768), 32768)
        self.assertIsNone(r.pool.take(0))
        self.check_stats(r, frames_ok=1, unexpected_size_614400=1, overflow=1)

    def test_614400_stream_from_the_start(self):
        # before the first EOF a full buffer ends the frame (EOF-less rule); from then on every
        # 614400-byte frame is counted and none is published
        r = rig()
        for k in range(4):
            feed(r, urbs(payloads(frame_data(51 + k, 614400), STRIDE, k & 1, k), 32768), 32768)
        self.assertIsNone(r.pool.take(0))
        self.assertEqual((r.asm.stats["unexpected_size_614400"], r.asm.stats["frames_ok"], r.asm.eof_seen),
                         (3, 0, True))

    def test_strict_to_lenient_after_one_second(self):
        def short_header(fid, pts, eof=False, err=False, scr=0):        # PTS only: lenient, not strict
            return bytes([6, 0x04 | fid | (0x02 if eof else 0)]) + struct.pack("<I", pts)

        r = rig()
        frames = []
        for k in range(8):
            data = frame_data(60 + k)
            frames.append(data)
            feed(r, urbs(payloads(data, STRIDE, k & 1, 500 + k, make_header=short_header), 32768), 32768)
            if k < 4:
                self.assertEqual(r.asm.mode, "strict", "switched before 1.0 s")
            r.clock.t += 0.25
        self.assertEqual(r.asm.mode, "lenient")
        self.assertEqual(r.events, [("lenient_headers", {})])
        buf, info = r.pool.take(0)
        self.assertEqual((buf == frames[-1], info.pts), (True, 507))
        self.assertGreaterEqual(r.asm.stats["frames_ok"], 2)
        self.assertGreaterEqual(r.asm.stats["bad_hdr"], 41)

    def test_lenient_needs_20_starts_and_a_full_second(self):
        r = rig()
        bad = bytes([6, 0x05]) + bytes(32762)                              # one rejected payload start per URB
        for _ in range(100):                                                # many starts, 0.9 s
            r.reasm.feed(memoryview(bad), len(bad), 32768)
            r.clock.t += 0.009
        self.assertEqual(r.asm.mode, "strict")
        r2 = rig()
        for _ in range(10):                                                 # 2 s, only 19 starts
            r2.reasm.feed(memoryview(bad), len(bad), 32768)
        r2.clock.t += 2.0
        for _ in range(9):
            r2.reasm.feed(memoryview(bad), len(bad), 32768)
        self.assertEqual(r2.asm.mode, "strict")
        r2.reasm.feed(memoryview(bad), len(bad), 32768)                     # the 20th start after 2 s
        self.assertEqual(r2.asm.mode, "lenient")
        r3 = rig()                                                           # a valid header restarts the run
        for _ in range(15):
            r3.reasm.feed(memoryview(bad), len(bad), 32768)
        r3.clock.t += 0.5
        good = header(0, 1) + bytes(1000)
        r3.reasm.feed(memoryview(good), len(good), 32768)
        r3.clock.t += 0.7
        for _ in range(15):
            r3.reasm.feed(memoryview(bad), len(bad), 32768)
        self.assertEqual(r3.asm.mode, "strict")

    def test_fps_of_accepted_frames(self):
        r = rig()
        for k in range(4):
            pts = (4294000000 + 2500000 * k) & 0xFFFFFFFF                    # wraps after the first frame
            feed(r, urbs(payloads(frame_data(k), STRIDE, k & 1, pts), 32768), 32768)
            r.clock.t += 1 / 60
        self.assertAlmostEqual(r.asm.fps.fps_in, 60.0, places=6)
        self.assertEqual(r.asm.fps.fps_pts, 60.0)                          # 150 MHz / 2500000, across the wrap

    def test_stats_keys_always_present(self):
        r = rig()
        self.assertEqual(dict(r.asm.stats), {k: 0 for k in STAT_KEYS})
        self.assertEqual(set(STAT_KEYS), {"frames_ok", "bad_size", "bad_err", "bad_hdr", "overflow", "fid_breaks",
                                          "unexpected_size_614400", "dropped_busy", "stride_learned"})
        self.assertEqual((r.asm.mode, rig(strict=False).asm.mode), ("strict", "lenient"))


def with_fake_headers(pls, fid, pts):
    """Each payload with a strict-looking header (same FID, other PTS) at payload offset 1024."""
    out = []
    for p in pls:
        if len(p) > 1100:
            p = p[:1024] + header(fid, pts ^ 0x5A5A5A5A) + p[1036:]
        out.append(p)
    return out


class Stride(unittest.TestCase):
    def frames(self, n, start=0, fakes=False):
        for k in range(start, start + n):
            data = frame_data(70 + k)
            pls = payloads(data, 16384, k & 1, 9000 + 333 * k)
            if fakes:
                pls = with_fake_headers(pls, k & 1, 9000 + 333 * k)
            yield k, data, urbs(pls, 32768)

    def test_learner_needs_three_frames_and_ignores_other_pts(self):
        for fakes in (False, True):
            learner, results = StrideLearner(512), []
            for k, _, url in self.frames(4, fakes=fakes):
                for u in url:
                    got = learner.observe(memoryview(u), len(u), 32768)
                    if got is not None:
                        results.append((k, got))
            self.assertEqual(results, [(2, 16384)], "fakes=%s" % fakes)

    def test_learner_takes_ctypes_views(self):
        learner, results = StrideLearner(512), []
        for k, _, url in self.frames(4):
            for u in url:
                got = learner.observe(ctypes_view(u), len(u), 32768)
                if got is not None:
                    results.append((k, got))
        self.assertEqual(results, [(2, 16384)])

    def test_reassembler_learns_16384_from_3_frames(self):
        for fakes in (False, True):
            with self.subTest(fakes=fakes):
                r = rig(max_payload=32768)                                  # the probe said 32768
                learned_at, last = None, None
                for k, data, url in self.frames(10, fakes=fakes):
                    feed(r, url, 32768)
                    if learned_at is None and r.reasm.max_payload == 16384:
                        learned_at = k
                    if k == 4:
                        self.assertTrue(r.reasm.learning)
                    last = data
                    r.clock.t += 0.4
                self.assertEqual(r.reasm.max_payload, 16384)
                self.assertEqual(r.events, [("stride_learned", {"stride": 16384})])
                self.assertEqual(r.asm.stats["stride_learned"], 1)
                # failing from frame 0: learning from frame 3 (1.2 s), learned in the third learning frame
                self.assertEqual(learned_at, 5)
                self.assertFalse(r.reasm.learning)
                buf, _ = r.pool.take(0)
                self.assertEqual(buf == last, not fakes)                   # fakes are written into the image
                self.assertGreaterEqual(r.asm.stats["frames_ok"], 4)

    def test_no_learning_while_frames_succeed(self):
        r = rig()
        for k in range(6):
            feed(r, urbs(payloads(frame_data(k), STRIDE, k & 1, k), 32768), 32768)
            r.clock.t += 0.5
        self.assertEqual((r.reasm.learning, r.reasm.max_payload, r.asm.stats["frames_ok"]), (False, STRIDE, 6))


class Fps(unittest.TestCase):
    def test_pts_median_and_arrival_ewma(self):
        m = FpsMeter()
        self.assertEqual((m.fps_in, m.fps_pts), (0.0, None))
        m.add(10.0, 1000)
        self.assertEqual((m.fps_in, m.fps_pts), (0.0, None))
        t, pts = 10.0, 1000
        for k in range(1, 100):
            step = 5000000 if k % 10 == 0 else 2500000                       # every 10th frame lost: one double step
            t, pts = t + step / 150e6, (pts + step) & 0xFFFFFFFF
            m.add(t, pts)
        self.assertEqual(m.fps_pts, 60.0)                                     # the median ignores the double steps
        self.assertTrue(50 < m.fps_in < 60, m.fps_in)                         # arrivals include the gaps
        slow = FpsMeter()
        for k in range(61):
            slow.add(k / 27.0, (k * 5555555) & 0xFFFFFFFF)                    # ~27 fps, PTS wraps at 2^32
        self.assertAlmostEqual(slow.fps_pts, 150e6 / 5555555, places=6)
        self.assertAlmostEqual(slow.fps_in, 27.0, places=3)
        even = FpsMeter(window=4)
        for k, p in enumerate((0, 100, 300, 600, 1000, 1500)):              # last 4 steps 200, 300, 400, 500
            even.add(float(k), p)
        self.assertEqual(even.fps_pts, 150e6 / 350)


class Pool(unittest.TestCase):
    def test_same_three_buffers_and_dropped_busy(self):
        pool = FramePool(1000)
        self.assertEqual(len(pool.buffers), 3)
        ids = {id(b) for b in pool.buffers}
        self.assertIsNone(pool.take(0))
        seen = set()
        for i in range(30):
            mv = pool.fill()
            seen.add(id(mv.obj))
            mv[0] = i
            pool.publish(FrameInfo(i, float(i), None, 1000, "eof"))
            if i % 3 == 2:                                                   # sometimes the consumer is late
                mv = pool.fill()
                seen.add(id(mv.obj))
                mv[0] = 200 + i
                pool.publish(FrameInfo(200 + i, float(i), None, 1000, "eof"))
            buf, info = pool.take(0)
            seen.add(id(buf))
            self.assertEqual(buf[0], info.seq)
            self.assertIsNot(pool.fill().obj, buf)
        self.assertEqual(seen, ids)
        self.assertEqual(pool.stats["dropped_busy"], 10)
        self.assertEqual((pool.stats["published"], pool.stats["taken"]), (40, 30))
        with self.assertRaises(ValueError):
            FramePool(10, n=2)

    def test_assembler_reports_dropped_busy(self):
        r = rig()
        for k in range(3):
            feed(r, urbs(payloads(frame_data(k), STRIDE, k & 1, k), 32768), 32768)
        self.assertEqual((r.asm.stats["frames_ok"], r.asm.stats["dropped_busy"]), (3, 2))
        self.assertEqual(r.pool.take(0)[1].seq, 3)

    def test_take_waits(self):
        pool = FramePool(10)
        t0 = time.monotonic()
        self.assertIsNone(pool.take(0.05))
        self.assertGreaterEqual(time.monotonic() - t0, 0.04)
        timer = threading.Timer(0.05, pool.publish, (FrameInfo(1, 0.0, None, 10, "eof"),))
        timer.start()
        self.addCleanup(timer.cancel)
        got = pool.take(5.0)
        self.assertEqual(got[1].seq, 1)

    def test_producer_and_consumer_threads(self):
        pool, n = FramePool(4096), 400
        taken = []

        def produce():
            for i in range(1, n + 1):
                mv = pool.fill()
                mv[:] = bytes([i & 0xFF]) * 4096
                pool.publish(FrameInfo(i, 0.0, None, 4096, "eof"))
                if i % 50 == 0:
                    time.sleep(0.001)

        producer = threading.Thread(target=produce)
        producer.start()
        while producer.is_alive() or (pool._ready is not None):
            got = pool.take(0.2)
            if got is None:
                continue
            buf, info = got
            snapshot = bytes(buf)
            time.sleep(0.0002)                                               # hold it while the producer runs
            self.assertEqual(bytes(buf), snapshot, "the process buffer changed under the consumer")
            self.assertEqual(snapshot, bytes([info.seq & 0xFF]) * 4096)
            taken.append(info.seq)
        producer.join()
        self.assertEqual(taken, sorted(taken))
        self.assertEqual(taken[-1], n)
        self.assertEqual(pool.stats["published"], n)
        self.assertEqual(pool.stats["dropped_busy"] + len(taken), n)


if __name__ == "__main__":
    unittest.main()
