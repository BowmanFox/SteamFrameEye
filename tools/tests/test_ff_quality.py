# SPDX-License-Identifier: MIT
"""Health gate and plane analysis (spec §4.11, §4.15.6; §11.3 test_ff_quality, task T1)."""
import ffutil  # first: installs the port guard

import random
import unittest

ffutil.setup_path()
from frameface.quality import (Health, HealthTracker, Layout, LayoutSchedule, analyze_layout,  # noqa: E402
                               choose_phase)

FRAME = 320000


def interleave(even, odd):
    out = bytearray(FRAME)
    out[0::2] = even
    out[1::2] = odd
    return bytes(out)


def noise(seed, n=160000):
    return random.Random(seed).randbytes(n)


def gray():
    """A 400x400 image with gradients and noise (std well above 10)."""
    rnd = random.Random(7)
    return bytes(min(255, 30 + (x * 150) // 400 + (y * 40) // 400 + rnd.randrange(12))
                 for y in range(400) for x in range(400))


GRAY = gray()


def h2_full():
    """Two 400-wide images side by side, byte per pixel: neighbouring columns differ."""
    rnd = random.Random(9)
    return bytes(min(255, 40 + (k * 160) // 800 + 6 * (k % 2) + rnd.randrange(4)) for _ in range(400) for k in range(800))


def bars():
    """Eight vertical bars (the bridge chip's test pattern): 8 distinct values, luma copied to chroma."""
    row = bytes(16 + 32 * (x // 50) for x in range(400))
    return interleave(row * 400, row * 400)


class Health_(unittest.TestCase):
    def test_dark_takes_precedence(self):
        t = HealthTracker(1)
        black = bytes(FRAME)
        h = t.classify(black)
        self.assertEqual(h, Health("dark", 0.0, 0, 1))
        rnd = random.Random(1)
        dim = bytes(rnd.randrange(3) for _ in range(FRAME))                 # values 0..2: < 16 distinct values
        h = t.classify(dim)
        self.assertEqual((h.kind, h.distinct, h.max), ("dark", 3, 2))
        self.assertLess(h.mean, 3.0)

    def test_dark_needs_max_below_16(self):
        frame = bytearray(FRAME)
        frame[1::78] = bytes([1]) * 4103
        frame[1 + 78 * 100] = 16                                            # one bright sample
        h = HealthTracker(1).classify(bytes(frame))
        self.assertEqual((h.kind, h.max, h.distinct), ("ok", 16, 2))       # not dark, and mean <= 8: not test bars

    def test_testbar(self):
        h = HealthTracker(1).classify(bars())
        self.assertEqual(h.kind, "testbar")
        self.assertLessEqual(h.distinct, 8)
        self.assertGreater(h.mean, 8)

    def test_frozen_and_ok(self):
        t = HealthTracker(1)
        a, b = noise(1, FRAME), noise(2, FRAME)
        self.assertEqual(t.classify(a).kind, "ok")
        self.assertEqual(t.classify(a).kind, "frozen")
        self.assertEqual(t.classify(bytearray(a)).kind, "frozen")
        self.assertEqual(t.classify(b).kind, "ok")
        self.assertEqual(t.classify(memoryview(b)).kind, "frozen")          # any buffer type
        changed = bytearray(b)
        changed[7 * 1000] ^= 0xFF                                            # a byte that frame[::7] samples
        self.assertEqual(t.classify(bytes(changed)).kind, "ok")
        same_sample = bytearray(changed)
        same_sample[7 * 1000 + 3] ^= 0xFF                                    # not sampled: still frozen
        self.assertEqual(t.classify(bytes(same_sample)).kind, "frozen")

    def test_sample_is_every_78th_byte_of_the_plane(self):
        frame = bytearray(FRAME)
        frame[1::78] = bytes([20]) * 4103
        self.assertEqual(len(frame[1::78]), 4103)
        odd = HealthTracker(1).classify(bytes(frame))
        self.assertEqual((odd.kind, odd.mean, odd.max, odd.distinct), ("testbar", 20.0, 20, 1))
        self.assertEqual(HealthTracker(0).classify(bytes(frame)).kind, "dark")
        plane = interleave(noise(3), bytes(160000))                          # even noise, odd black
        self.assertEqual(HealthTracker(1).classify(plane).kind, "dark")
        self.assertEqual(HealthTracker(0).classify(plane).kind, "ok")
        with self.assertRaises(ValueError):
            HealthTracker(2)

    def test_consecutive_counts(self):
        t = HealthTracker(1)
        for _ in range(3):
            t.classify(bytes(FRAME))
        self.assertEqual(t.consecutive, {"ok": 0, "dark": 3, "testbar": 0, "frozen": 0})
        t.classify(noise(4, FRAME))
        t.classify(noise(5, FRAME))
        self.assertEqual(t.consecutive, {"ok": 2, "dark": 0, "testbar": 0, "frozen": 0})
        t.classify(bars())
        self.assertEqual(t.consecutive, {"ok": 0, "dark": 0, "testbar": 1, "frozen": 0})


class Layouts(unittest.TestCase):
    def test_h1_copy_is_identical(self):
        lay = analyze_layout(interleave(GRAY, GRAY))
        self.assertEqual((lay.verdict, lay.h2_hint, lay.mad_even_odd), ("identical", False, 0.0))
        self.assertGreater(lay.std_even, 10)
        self.assertAlmostEqual(lay.std_even, lay.std_odd)
        self.assertEqual((len(lay.phase_means), len(lay.phase_stds)), (4, 4))
        self.assertAlmostEqual(lay.phase_means[0], lay.phase_means[1])      # luma copied into chroma

    def test_h1_chroma80_is_odd_flat(self):
        lay = analyze_layout(interleave(GRAY, b"\x80" * 160000))
        self.assertEqual((lay.verdict, lay.h2_hint, lay.std_odd), ("odd_flat", False, 0.0))
        self.assertGreater(lay.std_even, 10)
        self.assertEqual((lay.phase_means[1], lay.phase_means[3]), (128.0, 128.0))

    def test_even_flat(self):
        lay = analyze_layout(interleave(b"\x80" * 160000, GRAY))
        self.assertEqual((lay.verdict, lay.std_even), ("even_flat", 0.0))

    def test_h2_full_is_distinct_with_hint(self):
        lay = analyze_layout(h2_full())
        self.assertEqual((lay.verdict, lay.h2_hint), ("distinct", True))
        self.assertGreaterEqual(lay.mad_even_odd, 2)
        self.assertGreater(min(lay.std_even, lay.std_odd), 10)

    def test_flat(self):
        lay = analyze_layout(bytes([128]) * FRAME)
        self.assertEqual(lay, Layout(0.0, 0.0, 0.0, (128.0,) * 4, (0.0,) * 4, "identical", False))

    def test_only_sampled_rows_count(self):
        frame = bytearray(interleave(GRAY, GRAY))
        frame[1 * 800:2 * 800] = bytes(800)                                  # row 1 is not analysed
        self.assertEqual(analyze_layout(bytes(frame)), analyze_layout(interleave(GRAY, GRAY)))
        frame[25 * 800 + 1] ^= 0x40                                           # row 25 is
        self.assertNotEqual(analyze_layout(bytes(frame)).mad_even_odd, 0.0)

    def test_population_std(self):
        row = bytes([10, 30] * 400)                                          # even 10, odd 30
        lay = analyze_layout(row * 400)
        self.assertEqual((lay.std_even, lay.std_odd, lay.mad_even_odd), (0.0, 0.0, 20.0))
        alternating = bytes([0, 0, 20, 20] * 200)                             # even plane 0, 20, 0, 20, ...
        lay = analyze_layout(alternating * 400)
        self.assertEqual((lay.std_even, lay.std_odd), (10.0, 10.0))           # population std, not sample std


class Schedule(unittest.TestCase):
    """§4.15.6: the 30th ok frame, then the first ok frame after each further 10 s during the first
    60 s, then every 60th ok frame."""

    def test_exact_sequence_at_one_frame_per_second(self):
        s = LayoutSchedule()
        picked = [n for n in range(1, 201) if s.ok_frame(float(n - 1))]       # frame n arrives at t = n - 1
        self.assertEqual(picked, [30, 40, 50, 60, 120, 180])

    def test_sixty_fps(self):
        s = LayoutSchedule()
        t0, dt = 1000.3, 1 / 59.7
        picked = [(n, t0 + (n - 1) * dt) for n in range(1, 60 * 90) if s.ok_frame(t0 + (n - 1) * dt)]
        self.assertEqual(picked[0][0], 30)
        early = [(n, t) for n, t in picked if t - t0 < 60.0]
        self.assertEqual(len(early), 6)                                       # 30th, then ~10, 20, 30, 40, 50 s
        for (_, a), (n, b) in zip(early, early[1:]):
            self.assertGreaterEqual(b - a, 10.0)
            self.assertLess(b - dt - a, 10.0, "the first ok frame after the 10 s mark")
        late = [n for n, t in picked if t - t0 >= 60.0]
        self.assertEqual(late, [n for n in range(1, 60 * 90) if n % 60 == 0 and t0 + (n - 1) * dt - t0 >= 60.0])

    def test_reset_starts_a_new_stream(self):
        s = LayoutSchedule()
        for n in range(1, 31):
            hit = s.ok_frame(float(n))
        self.assertTrue(hit)
        s.reset()
        self.assertEqual([n for n in range(1, 31) if s.ok_frame(500.0 + n)], [30])


def L(verdict):
    return Layout(0.0, 0.0, 0.0, (0.0,) * 4, (0.0,) * 4, verdict, False)


class ChoosePhase(unittest.TestCase):
    def test_configured_wins(self):
        self.assertEqual(choose_phase([L("odd_flat")] * 5, "odd"), (1, "configured"))
        self.assertEqual(choose_phase([], "even"), (0, "configured"))
        self.assertEqual(choose_phase([L("identical")] * 5, "even"), (0, "configured"))

    def test_three_of_five_odd_flat_takes_even(self):
        self.assertEqual(choose_phase([L("odd_flat")] * 3 + [L("identical")] * 2, "auto"), (0, "odd_flat"))
        mixed = [L("odd_flat"), L("distinct"), L("odd_flat"), L("identical"), L("odd_flat")]
        self.assertEqual(choose_phase(mixed, "auto"), (0, "odd_flat"))

    def test_two_of_five_keeps_odd(self):
        self.assertEqual(choose_phase([L("odd_flat")] * 2 + [L("identical")] * 3, "auto"), (1, "default"))
        self.assertEqual(choose_phase([], "auto"), (1, "default"))
        self.assertEqual(choose_phase([L("even_flat")] * 5, "auto"), (1, "default"))

    def test_only_the_last_five_count(self):
        self.assertEqual(choose_phase([L("odd_flat")] * 3 + [L("identical")] * 5, "auto"), (1, "default"))
        self.assertEqual(choose_phase([L("identical")] * 10 + [L("odd_flat")] * 3, "auto"), (0, "odd_flat"))
        self.assertEqual(choose_phase([L("odd_flat")] * 3 + [L("identical")] * 3, "auto"), (1, "default"))
        self.assertEqual(choose_phase(iter([L("odd_flat")] * 3), "auto"), (0, "odd_flat"))
        real = [analyze_layout(interleave(GRAY, b"\x80" * 160000))] * 3
        self.assertEqual(choose_phase(real, "auto"), (0, "odd_flat"))


if __name__ == "__main__":
    unittest.main()
