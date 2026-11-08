# SPDX-License-Identifier: MIT
"""Pixels (spec §4.15, Appendices B-D and F; §11.3 test_ff_preprocess; task T3).

The oracle (preprocess_ref, literal OpenCV coefficients) and the SWAR fallback must agree byte for
byte on every test frame, produce the golden SHA-256 values of Appendix D, and use the Appendix F
LUT. The C helper itself is tested in test_ff_native (Linux + gcc); here make_preprocessor() must
fall back to SWAR whenever no usable helper exists, without raising.
"""
import ffutil  # first: installs and self-checks the port guard before anything else

import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ffutil.setup_path()
from frameface import preprocess as pp  # noqa: E402

GEN_VECTORS = ffutil.REPO / "tools" / "parity" / "gen_vectors.py"

APPENDIX_F = """
0,12,15,18,21,22,24,26,27,29,30,31,32,33,34,35,36,37,38,39,40,40,41,42,43,43,44,45,45,46,47,47,
48,48,49,50,50,51,51,52,52,53,53,54,54,55,55,56,56,57,57,58,58,59,59,60,60,60,61,61,62,62,62,63,
63,64,64,64,65,65,66,66,66,67,67,67,68,68,68,69,69,70,70,70,71,71,71,72,72,72,73,73,73,74,74,74,
74,75,75,75,76,76,76,77,77,77,78,78,78,78,79,79,79,80,80,80,80,81,81,81,81,82,82,82,83,83,83,83,
84,84,84,84,85,85,85,85,86,86,86,86,87,87,87,87,88,88,88,88,89,89,89,89,90,90,90,90,91,91,91,91,
91,92,92,92,92,93,93,93,93,94,94,94,94,94,95,95,95,95,95,96,96,96,96,97,97,97,97,97,98,98,98,98,
98,99,99,99,99,99,100,100,100,100,100,101,101,101,101,101,102,102,102,102,102,103,103,103,103,103,104,104,104,104,104,105,
105,105,105,105,105,106,106,106,106,106,107,107,107,107,107,107,108,108,108,108,108,109,109,109,109,109,109,110,110,110,110,110
"""
LUT_SHA256 = "b2e0b84b3479e5d1b2ca97b93eb95bd2b62ff32174f70bb67e1b8a578451bbfa"
LCG1_SHA256 = "34ea99b8466e121a363c2f9c11691bee0063019f6e57a6e1926711de7b0802e5"

# Appendix D: (input, phase, col0, SHA-256 of the 160000-byte output, first 8 bytes)
GOLDEN = (
    ("lcg1", 1, 0, "59ef8cf9adf85f78effb02015405de4d36b2dcf0aa8cc251ee14fd70969cf959", (91, 89, 86, 85, 87, 90, 93, 95)),
    ("lcg1", 0, 200, "22fd4fee4a97b7dcb597edc6ad92b0d281c4e655b4002562b86d29a9e7ba293e", (83, 81, 79, 80, 83, 81, 76, 75)),
    ("lcg2", 1, 200, "c568bdc5d1cf887842973ebe0500baf8e7834fd342a3ed33ba7d20aadfd63df3", (81, 82, 85, 90, 95, 95, 93, 90)),
    ("gradient", 1, 0, "c54e25eb1a0fdb5a6dfc0c0523b0b345e9912f5138f74917309a30c09201d452", (30, 31, 33, 36, 39, 41, 43, 45)),
    ("all255", 1, 0, "f46f38997fc2aa16dddc3cb2ddcb8842f5821d23d5e2c205f513f0d20035b593", (110,) * 8),
    ("zero", 1, 0, "b9ce164d30e4101b009fe4be765a070593cfbdd48f897853de159a8c177fabe8", (0,) * 8),
)


def planes(fn_even, fn_odd=None):
    """A frame whose even bytes are fn_even(x, y) and odd bytes fn_odd(x, y) (default: the same)."""
    fn_odd = fn_odd or fn_even
    out = bytearray(pp.FRAME_BYTES)
    for y in range(400):
        base = y * 800
        out[base:base + 800:2] = bytes(fn_even(x, y) & 0xFF for x in range(400))
        out[base + 1:base + 800:2] = bytes(fn_odd(x, y) & 0xFF for x in range(400))
    return bytes(out)


def _impulses(points):
    out = bytearray(pp.FRAME_BYTES)
    for x, y in points:
        out[y * 800 + 2 * x] = out[y * 800 + 2 * x + 1] = 255
    return bytes(out)


# >= 24 frames (§11.3): lcg seeds 1-6, zero, 255, gradient, column stripes, checkerboards, impulses
# and further structures that stress the stretch, both borders of the blur and the plane choice.
FRAME_BUILDERS = {
    **{"lcg%d" % s: (lambda s=s: pp.lcg_frame(s)) for s in range(1, 7)},
    "zero": lambda: bytes(pp.FRAME_BYTES),
    "all255": lambda: bytes([255]) * pp.FRAME_BYTES,
    "gradient": lambda: bytes(((i // 800) * 7 + (i % 800) * 3) & 0xFF for i in range(pp.FRAME_BYTES)),
    "stripes_col1": lambda: planes(lambda x, y: 255 * (x & 1)),
    "stripes_col4": lambda: planes(lambda x, y: 255 * ((x // 4) & 1)),
    "stripes_col7": lambda: planes(lambda x, y: 255 * ((x // 7) & 1)),
    "stripes_bytes": lambda: planes(lambda x, y: 255, lambda x, y: 0),
    "stripes_rows": lambda: planes(lambda x, y: 255 * (y & 1)),
    "checker1": lambda: planes(lambda x, y: 255 * ((x + y) & 1)),
    "checker8": lambda: planes(lambda x, y: 255 * (((x // 8) + (y // 8)) & 1)),
    "checker_planes": lambda: planes(lambda x, y: 255 * (((x // 3) + (y // 3)) & 1),
                                     lambda x, y: 255 * (1 - (((x // 3) + (y // 3)) & 1))),
    "impulses_corners": lambda: _impulses([(x, y) for x in (0, 199, 200, 399) for y in (0, 399)]),
    "impulses_inner": lambda: _impulses([(x, y) for x in (1, 2, 197, 198, 201, 202, 397, 398)
                                         for y in (1, 2, 397, 398)]),
    "ramp_x": lambda: planes(lambda x, y: 399 - x, lambda x, y: x),
    "ramp_y": lambda: planes(lambda x, y: y * 255 // 399),
    "sparse_dots": lambda: bytes(255 if b > 250 else 0 for b in pp.lcg_frame(77)),
    "diagonals": lambda: planes(lambda x, y: 255 * ((x + y) % 3 == 0), lambda x, y: 255 * ((x - y) % 5 == 0)),
    "camera_split": lambda: planes(lambda x, y: 255 if x < 200 else 0, lambda x, y: 0 if x < 200 else 255),
    "dark_noise": lambda: bytes(b & 0x0F for b in pp.lcg_frame(99)),
}

_frames, _ref = {}, {}


def frame(name):
    if name not in _frames:
        _frames[name] = FRAME_BUILDERS[name]()
    return _frames[name]


def ref(name, phase, col0):
    """preprocess_ref output, computed once per (frame, phase, col0) for the whole module."""
    key = (name, phase, col0)
    if key not in _ref:
        _ref[key] = pp.preprocess_ref(frame(name), phase, col0)
    return _ref[key]


def sha(data):
    return hashlib.sha256(data).hexdigest()


class LutTest(unittest.TestCase):
    def test_lut_equals_appendix_f(self):
        values = [int(v) for v in APPENDIX_F.replace("\n", "").split(",")]
        self.assertEqual(len(values), 256)
        self.assertEqual(pp.LUT, bytes(values))
        self.assertEqual(sha(pp.LUT), LUT_SHA256)

    def test_lut_formula(self):
        self.assertEqual(pp.LUT, bytes(int((i / 2048) ** 0.4 * 255) for i in range(256)))
        # Baballonia's spelling: (byte)(Math.Pow(i / 2048.0, 1.0 / 2.5) * 255.0)
        self.assertEqual(pp.LUT, bytes(int(math.pow(i / 2048.0, 1.0 / 2.5) * 255.0) for i in range(256)))
        self.assertEqual([pp.LUT[i] for i in (0, 1, 16, 64, 128, 192, 255)], [0, 12, 36, 63, 84, 98, 110])


class GoldenTest(unittest.TestCase):
    def test_lcg_frame(self):
        self.assertEqual(sha(frame("lcg1")), LCG1_SHA256)
        self.assertEqual(len(pp.lcg_frame(5)), pp.FRAME_BYTES)
        self.assertEqual(pp.lcg_frame(1, 16), frame("lcg1")[:16])
        self.assertEqual(pp.lcg_frame(1 + (1 << 32), 64), pp.lcg_frame(1, 64))     # seed taken mod 2^32
        self.assertNotEqual(pp.lcg_frame(2, 64), pp.lcg_frame(1, 64))

    def check_golden(self, fn, label):
        for name, phase, col0, digest, first8 in GOLDEN:
            with self.subTest(impl=label, input=name, phase=phase, col0=col0):
                out = fn(name, phase, col0)
                self.assertIsInstance(out, bytes)
                self.assertEqual(len(out), pp.OUT_BYTES)
                self.assertEqual(tuple(out[:8]), first8)
                self.assertEqual(sha(out), digest)

    def test_golden_ref(self):
        self.check_golden(ref, "ref")

    def test_golden_swar(self):
        self.check_golden(lambda n, p, c: pp.preprocess_swar(frame(n), p, c), "swar")


class AgreementTest(unittest.TestCase):
    def test_at_least_24_frames(self):
        self.assertGreaterEqual(len(FRAME_BUILDERS), 24)
        for name in FRAME_BUILDERS:
            self.assertEqual(len(frame(name)), pp.FRAME_BYTES, name)
        digests = {sha(frame(n)) for n in FRAME_BUILDERS}
        self.assertEqual(len(digests), len(FRAME_BUILDERS), "test frames must be distinct")

    def test_ref_equals_swar(self):
        compared = 0
        for name in FRAME_BUILDERS:
            for phase in (0, 1):
                for col0 in (0, 200):
                    with self.subTest(frame=name, phase=phase, col0=col0):
                        expected = ref(name, phase, col0)
                        got = pp.preprocess_swar(frame(name), phase, col0)
                        if got != expected:
                            i = next(k for k in range(pp.OUT_BYTES) if got[k] != expected[k])
                            self.fail("first difference at x=%d y=%d: ref %d swar %d"
                                      % (i % 400, i // 400, expected[i], got[i]))
                        compared += 1
        self.assertGreaterEqual(compared, 24 * 2 * 2)

    def test_output_range_and_plane_choice(self):
        # every output value is a LUT value; the phase picks the plane, col0 the camera
        self.assertLessEqual(set(ref("lcg3", 1, 0)), set(pp.LUT))
        self.assertEqual(set(ref("stripes_bytes", 0, 0)), {110})          # even plane all 255
        self.assertEqual(set(ref("stripes_bytes", 1, 0)), {0})            # odd plane all 0
        self.assertEqual(set(ref("camera_split", 0, 0)), {110})           # even: left 255
        self.assertEqual(set(ref("camera_split", 0, 200)), {0})           # even: right 0
        self.assertEqual(set(ref("camera_split", 1, 200)), {110})         # odd: right 255


class InputTest(unittest.TestCase):
    def test_inputs_other_than_bytes(self):
        f = frame("lcg4")
        want = pp.preprocess_swar(f, 1, 200)
        self.assertEqual(pp.preprocess_swar(bytearray(f), 1, 200), want)
        self.assertEqual(pp.preprocess_swar(memoryview(f), 1, 200), want)
        self.assertEqual(pp.preprocess_ref(bytearray(f), 1, 200), want)
        self.assertEqual(pp.SwarPreprocessor()(bytearray(f), 1, 200), want)

    def test_bad_arguments(self):
        f = frame("zero")
        for fn in (pp.preprocess_ref, pp.preprocess_swar):
            for args in ((f[:-1], 1, 0), (f + b"\0", 1, 0), (f, 2, 0), (f, -1, 0), (f, 1, 100),
                         (f, 1, 400), (f, 1, -200)):
                with self.subTest(fn=fn.__name__, length=len(args[0]), phase=args[1], col0=args[2]):
                    with self.assertRaises(ValueError):
                        fn(*args)
            with self.assertRaises(TypeError):
                fn(f, 1.0, 0)

    def test_stub_preprocessor(self):
        stub = pp.StubPreprocessor()
        self.assertEqual((stub.name, stub.max_fps), ("stub", None))
        f = frame("lcg5")
        out = stub(f, 0, 200)
        self.assertIsInstance(out, bytes)
        self.assertEqual(out, f[1::2])
        self.assertEqual(len(out), pp.OUT_BYTES)
        self.assertEqual(stub(bytearray(f), 1, 0), f[1::2])


class SelectionTest(unittest.TestCase):
    """make_preprocessor without a usable helper: SWAR, a WARN line, never an exception."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("FRAMEFACE_LIBFFPROC", None)          # the container runner sets it
        tmp = tempfile.TemporaryDirectory(prefix="ffpre-")
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    class Log:
        def __init__(self):
            self.lines = []

        def w(self, component, event, **kv):
            self.lines.append((component, event, kv))

    def check_swar(self, pre, reason):
        self.assertIsInstance(pre, pp.SwarPreprocessor)
        self.assertEqual((pre.name, pre.max_fps, pre.reason), ("swar", 30, reason))
        f = frame("lcg2")
        self.assertEqual(pre(f, 0, 0), pp.preprocess_swar(f, 0, 0))

    def test_falls_back_to_swar_without_helper(self):
        for mode in ("auto", "c"):
            with self.subTest(mode=mode):
                log = self.Log()
                pre = pp.make_preprocessor(self.dir, mode, log)
                self.check_swar(pre, "load")
                self.assertEqual(pre.path, os.path.join(self.dir, "libffproc.so"))
                self.assertEqual(log.lines, [("pixels", "c helper rejected", {"reason": "load"})])

    def test_plain_callable_log_and_no_log(self):
        lines = []
        self.check_swar(pp.make_preprocessor(self.dir, "auto", lines.append), "load")
        self.assertEqual(lines, ["W pixels: c helper rejected reason=load"])
        self.check_swar(pp.make_preprocessor(self.dir, "c"), "load")

    def test_unloadable_helper_file(self):
        bogus = os.path.join(self.dir, "libffproc.so")
        with open(bogus, "wb") as f:
            f.write(b"\x7fELF this is not a shared object" * 8)
        self.assertIsNone(pp.load_helper(bogus))
        self.check_swar(pp.make_preprocessor(self.dir, "auto"), "load")
        self.assertIsNone(pp.load_helper(os.path.join(self.dir, "missing.so")))

    def test_env_helper_missing(self):
        os.environ["FRAMEFACE_LIBFFPROC"] = os.path.join(self.dir, "nowhere", "libffproc.so")
        log = self.Log()
        pre = pp.make_preprocessor(None, "c", log)
        self.check_swar(pre, "load")
        self.assertEqual(pre.path, os.environ["FRAMEFACE_LIBFFPROC"])
        self.assertEqual(len(log.lines), 1)

    def test_swar_mode_and_bad_mode(self):
        log = self.Log()
        self.check_swar(pp.make_preprocessor(None, "swar", log), "")
        self.assertEqual(log.lines, [])
        with self.assertRaises(ValueError):
            pp.make_preprocessor(None, "fast")

    def test_helper_path(self):
        self.assertEqual(pp.helper_path(self.dir), os.path.join(self.dir, "libffproc.so"))
        self.assertEqual(pp.helper_path(None),
                         os.path.join(os.path.expanduser("~"), "frameface", "lib", "libffproc.so"))
        os.environ["FRAMEFACE_LIBFFPROC"] = "/opt/x/libffproc.so"
        self.assertEqual(pp.helper_path(None), "/opt/x/libffproc.so")
        self.assertEqual(pp.helper_path(self.dir), os.path.join(self.dir, "libffproc.so"))  # explicit dir wins


class ParityGateTest(unittest.TestCase):
    """parity_ok compares a candidate with SWAR on lcg_frame(1), lcg_frame(2) and all-255, both phases
    and both cameras (§4.15.5)."""

    def test_swar_passes(self):
        self.assertTrue(pp.parity_ok(pp.SwarPreprocessor()))
        self.assertTrue(pp.parity_ok(lambda f, p, c: bytearray(pp.preprocess_swar(f, p, c))))

    def test_one_wrong_pixel_fails(self):
        def corrupt(f, p, c):            # what -DFF_CORRUPT_FOR_TEST does to the C helper
            out = bytearray(pp.preprocess_swar(f, p, c))
            out[12345] = (out[12345] + 1) & 0xFF
            return bytes(out)
        self.assertFalse(pp.parity_ok(corrupt))

    def test_every_case_is_checked(self):
        seen = []
        all255 = bytes([255]) * pp.FRAME_BYTES

        def wrong_in_one_case(f, p, c):
            seen.append((sha(f), p, c))
            out = pp.preprocess_swar(f, p, c)
            if f == all255 and (p, c) == (1, 200):         # the last case of the set
                return out[:-1] + b"\x00"
            return out
        self.assertFalse(pp.parity_ok(wrong_in_one_case))
        self.assertEqual(len(set(seen)), 12)
        self.assertEqual({digest for digest, _, _ in seen},
                         {sha(frame("lcg1")), sha(frame("lcg2")), sha(all255)})
        self.assertEqual(sorted({(p, c) for _, p, c in seen}), [(0, 0), (0, 200), (1, 0), (1, 200)])

    def test_failures_are_false_not_exceptions(self):
        def boom(f, p, c):
            raise OSError("helper crashed")
        self.assertFalse(pp.parity_ok(boom))
        self.assertFalse(pp.parity_ok(pp.StubPreprocessor()))
        self.assertFalse(pp.parity_ok(lambda f, p, c: None))


class TimingTest(unittest.TestCase):
    def test_swar_timing(self):
        f = frame("lcg1")
        pp.preprocess_swar(f)
        best = float("inf")
        for _ in range(15):
            t0 = time.perf_counter()
            pp.preprocess_swar(f)
            best = min(best, time.perf_counter() - t0)
        machine = platform.machine().lower()
        print("\nSWAR %.2f ms/frame (best of 15; Python %s, %s)" % (best * 1e3, platform.python_version(), machine),
              file=sys.stderr)
        if machine in ("x86_64", "amd64") and ffutil.fps_asserts():
            self.assertLessEqual(best, 0.008)


class GenVectorsTest(unittest.TestCase):
    """tools/parity/gen_vectors.py (Appendix G) writes frames, oracle outputs and manifest.json."""

    def test_two_frames(self):
        with tempfile.TemporaryDirectory(prefix="ffparity-") as out:
            r = subprocess.run([sys.executable, "-I", "-B", str(GEN_VECTORS), "--out", out,
                                "--frames", "zero,checkerboard"],
                               capture_output=True, text=True, timeout=600)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            with open(os.path.join(out, "manifest.json"), encoding="utf-8") as fh:
                manifest = json.load(fh)
            self.assertEqual(bytes(manifest["lut"]), pp.LUT)
            self.assertEqual(manifest["lut_sha256"], LUT_SHA256)
            names = {f["name"]: f for f in manifest["frames"]}
            self.assertEqual(set(names), {"zero", "checkerboard"})
            self.assertEqual(names["zero"]["file"], "frame_4.bin")     # index in the full list
            frames = {}
            for f in manifest["frames"]:
                data = Path(out, f["file"]).read_bytes()
                self.assertEqual((len(data), sha(data)), (pp.FRAME_BYTES, f["sha256"]))
                frames[f["index"]] = data
            self.assertEqual(frames[4], bytes(pp.FRAME_BYTES))
            combos = set()
            for e in manifest["expected"]:
                data = Path(out, e["file"]).read_bytes()
                self.assertEqual(e["file"], "expected_%d_%d_%d.bin" % (e["frame"], e["phase"], e["col0"]))
                self.assertEqual(sha(data), e["sha256"])
                self.assertEqual(data, pp.preprocess_swar(frames[e["frame"]], e["phase"], e["col0"]))
                combos.add((e["frame"], e["phase"], e["col0"]))
            self.assertEqual(len(combos), 2 * 2 * 2)
            self.assertEqual(sorted(os.listdir(out)), sorted(
                ["manifest.json"] + [f["file"] for f in manifest["frames"]] + [e["file"] for e in manifest["expected"]]))

    def test_unknown_frame_name(self):
        with tempfile.TemporaryDirectory(prefix="ffparity-") as out:
            r = subprocess.run([sys.executable, "-I", "-B", str(GEN_VECTORS), "--out", out, "--frames", "nope"],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertEqual(os.listdir(out), [])


if __name__ == "__main__":
    unittest.main()
