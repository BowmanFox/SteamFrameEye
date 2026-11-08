# SPDX-License-Identifier: MIT
"""The C pixel helper csrc/ffproc.c (spec §4.15; §11.3 test_ff_native; task T3). Linux + gcc only.

Builds the helper with -Werror into a temp dir, then: ff_abi() == 1; bad arguments return -1 and
write nothing; C == SWAR on the parity set and the Appendix D golden hashes; a -DFF_CORRUPT_FOR_TEST
build (out[12345] + 1) and a wrong-ABI library are rejected, so make_preprocessor falls back to SWAR.
Also the container runner tools/docker/arm64-checks.sh (§11.1): its rules are read on every platform;
on Linux it runs against a mini repo with and without csrc/abi_dump.c.
"""
import ffutil  # first: installs and self-checks the port guard before anything else

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

ffutil.setup_path()
from frameface import preprocess as pp  # noqa: E402

SRC = ffutil.FRAMEFACE_DIR / "csrc" / "ffproc.c"
CFLAGS = ["-std=c99", "-O2", "-Wall", "-Wextra", "-Werror", "-fPIC", "-shared"]
GOLDEN = (   # Appendix D: (input, phase, col0, SHA-256 of the output)
    ("lcg1", 1, 0, "59ef8cf9adf85f78effb02015405de4d36b2dcf0aa8cc251ee14fd70969cf959"),
    ("lcg1", 0, 200, "22fd4fee4a97b7dcb597edc6ad92b0d281c4e655b4002562b86d29a9e7ba293e"),
    ("lcg2", 1, 200, "c568bdc5d1cf887842973ebe0500baf8e7834fd342a3ed33ba7d20aadfd63df3"),
    ("gradient", 1, 0, "c54e25eb1a0fdb5a6dfc0c0523b0b345e9912f5138f74917309a30c09201d452"),
    ("all255", 1, 0, "f46f38997fc2aa16dddc3cb2ddcb8842f5821d23d5e2c205f513f0d20035b593"),
    ("zero", 1, 0, "b9ce164d30e4101b009fe4be765a070593cfbdd48f897853de159a8c177fabe8"),
)
WRONG_ABI_C = """\
#include <stdint.h>
int ff_abi(void) { return 2; }
int ff_preprocess(const uint8_t *a, uint32_t b, int c, int d, const uint8_t *e, uint8_t *f, uint32_t g)
{ (void)a; (void)b; (void)c; (void)d; (void)e; (void)f; (void)g; return 0; }
"""
NO_ENTRY_C = "int ff_abi(void) { return 1; }\n"


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Log:
    def __init__(self):
        self.lines = []

    def w(self, component, event, **kv):
        self.lines.append((component, event, kv))


class NativeHelperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ffutil.require_native("gcc", reason="builds csrc/ffproc.c")
        cls._tmp = tempfile.TemporaryDirectory(prefix="ffproc-")
        cls.tmp = Path(cls._tmp.name)
        cls.good, cls.good_build = cls.build("good", SRC)
        cls.corrupt, cls.corrupt_build = cls.build("corrupt", SRC, "-DFF_CORRUPT_FOR_TEST")
        cls.frames = {
            "lcg1": pp.lcg_frame(1), "lcg2": pp.lcg_frame(2), "zero": bytes(pp.FRAME_BYTES),
            "all255": bytes([255]) * pp.FRAME_BYTES,
            "gradient": bytes(((i // 800) * 7 + (i % 800) * 3) & 0xFF for i in range(pp.FRAME_BYTES)),
        }

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @classmethod
    def build(cls, name, source, *defines):
        """gcc <CFLAGS> [defines] -o <tmp>/<name>/libffproc.so <source>; returns (path, process)."""
        out = cls.tmp / name / "libffproc.so"
        out.parent.mkdir()
        proc = subprocess.run(["gcc", *CFLAGS, *defines, "-o", str(out), str(source)],
                              capture_output=True, text=True, timeout=300)
        return out, proc

    def test_builds_with_werror_and_no_diagnostics(self):
        for proc in (self.good_build, self.corrupt_build):
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr.strip(), "")
        self.assertTrue(self.good.is_file() and self.corrupt.is_file())

    def test_source_rules(self):
        text = SRC.read_text(encoding="ascii")
        self.assertEqual(re.findall(r"#include\s*<([^>]+)>", text), ["stdint.h", "string.h"])
        self.assertIn("#define FF_ABI 1", text)
        self.assertIsNone(re.search(r"\b(malloc|calloc|realloc|free|alloca)\s*\(", text))   # no heap
        # no globals: every file-scope `static` is a function
        for line in text.splitlines():
            if line.startswith("static "):
                self.assertIn("(", line, line)

    def test_abi(self):
        lib = ctypes.CDLL(str(self.good))
        lib.ff_abi.restype = ctypes.c_int
        self.assertEqual(lib.ff_abi(), 1)
        helper = pp.load_helper(str(self.good))
        self.assertIsInstance(helper, pp.CPreprocessor)
        self.assertEqual((helper.name, helper.max_fps, helper.path), ("c", None, str(self.good)))

    def test_bad_arguments_return_minus_one(self):
        lib = ctypes.CDLL(str(self.good))
        fn = lib.ff_preprocess
        fn.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_int,
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)
        fn.restype = ctypes.c_int
        frame = ctypes.c_char_p(self.frames["lcg1"])
        lut = (ctypes.c_ubyte * 256).from_buffer_copy(pp.LUT)
        out = (ctypes.c_ubyte * pp.OUT_BYTES)()
        ok = (frame, 320000, 1, 0, lut, out, 160000)
        cases = {
            "NULL frame": (None,) + ok[1:],
            "len 319999": (frame, 319999) + ok[2:],
            "len 320001": (frame, 320001) + ok[2:],
            "len 0": (frame, 0) + ok[2:],
            "phase -1": ok[:2] + (-1,) + ok[3:],
            "phase 2": ok[:2] + (2,) + ok[3:],
            "col0 1": ok[:3] + (1,) + ok[4:],
            "col0 100": ok[:3] + (100,) + ok[4:],
            "col0 199": ok[:3] + (199,) + ok[4:],
            "col0 400": ok[:3] + (400,) + ok[4:],
            "col0 -200": ok[:3] + (-200,) + ok[4:],
            "NULL lut": ok[:4] + (None,) + ok[5:],
            "NULL out": ok[:5] + (None,) + ok[6:],
            "out_len 159999": ok[:6] + (159999,),
            "out_len 320000": ok[:6] + (320000,),
        }
        for why, args in cases.items():
            with self.subTest(why=why):
                ctypes.memset(out, 0xAA, pp.OUT_BYTES)
                self.assertEqual(fn(*args), -1)
                self.assertEqual(bytes(out), b"\xaa" * pp.OUT_BYTES)     # nothing written
        self.assertEqual(fn(*ok), 0)
        self.assertEqual(bytes(out), pp.preprocess_swar(self.frames["lcg1"], 1, 0))
        helper = pp.load_helper(str(self.good))
        for args in ((self.frames["lcg1"][:-1], 1, 0), (self.frames["lcg1"], 2, 0), (self.frames["lcg1"], 1, 100)):
            with self.assertRaises(ValueError):
                helper(*args)

    def test_c_equals_swar_on_the_parity_set(self):
        helper = pp.load_helper(str(self.good))
        for name in ("lcg1", "lcg2", "all255"):
            for phase in (0, 1):
                for col0 in (0, 200):
                    with self.subTest(frame=name, phase=phase, col0=col0):
                        self.assertEqual(helper(self.frames[name], phase, col0),
                                         pp.preprocess_swar(self.frames[name], phase, col0))
        self.assertTrue(pp.parity_ok(helper))

    def test_golden_hashes(self):
        helper = pp.load_helper(str(self.good))
        for name, phase, col0, digest in GOLDEN:
            with self.subTest(input=name, phase=phase, col0=col0):
                out = helper(self.frames[name], phase, col0)
                self.assertEqual(len(out), pp.OUT_BYTES)
                self.assertEqual(sha(out), digest)
                self.assertEqual(sha(helper(bytearray(self.frames[name]), phase, col0)), digest)
                self.assertEqual(sha(helper(memoryview(self.frames[name]), phase, col0)), digest)

    def test_corrupted_build_is_rejected(self):
        corrupt = pp.load_helper(str(self.corrupt))
        self.assertIsNotNone(corrupt)                                   # loads, ABI 1 ...
        f = self.frames["lcg2"]
        bad, good = corrupt(f, 1, 0), pp.preprocess_swar(f, 1, 0)
        self.assertEqual([i for i in range(pp.OUT_BYTES) if bad[i] != good[i]], [12345])
        self.assertEqual(bad[12345], (good[12345] + 1) & 0xFF)
        self.assertFalse(pp.parity_ok(corrupt))                         # ... and fails the gate
        log = Log()
        with mock.patch.dict(os.environ, {"FRAMEFACE_LIBFFPROC": str(self.good)}):
            pre = pp.make_preprocessor(str(self.corrupt.parent), "c", log)
        self.assertEqual((pre.name, pre.max_fps, pre.reason), ("swar", 30, "parity"))
        self.assertEqual(log.lines, [("pixels", "c helper rejected", {"reason": "parity"})])

    def test_wrong_abi_and_missing_entry_point(self):
        for name, source, reason in (("abi2", WRONG_ABI_C, "abi"), ("noentry", NO_ENTRY_C, "load")):
            with self.subTest(reason=reason):
                c_file = self.tmp / (name + ".c")
                c_file.write_text(source, encoding="ascii")
                path, proc = self.build(name, c_file)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIsNone(pp.load_helper(str(path)))
                log = Log()
                pre = pp.make_preprocessor(str(path.parent), "auto", log)
                self.assertEqual((pre.name, pre.reason), ("swar", reason))
                self.assertEqual(log.lines, [("pixels", "c helper rejected", {"reason": reason})])

    def test_make_preprocessor_selects_the_helper(self):
        log = Log()
        for mode in ("auto", "c"):
            pre = pp.make_preprocessor(str(self.good.parent), mode, log)
            self.assertIsInstance(pre, pp.CPreprocessor)
        with mock.patch.dict(os.environ, {"FRAMEFACE_LIBFFPROC": str(self.good)}):
            pre = pp.make_preprocessor(None, "auto", log)               # the test runner's variable
        self.assertEqual((pre.name, pre.path), ("c", str(self.good)))
        self.assertEqual(log.lines, [])
        self.assertIsInstance(pp.make_preprocessor(str(self.good.parent), "swar"), pp.SwarPreprocessor)

    def test_runner_helper(self):
        """The helper arm64-checks.sh built (FRAMEFACE_LIBFFPROC), or this test's own build."""
        path = os.environ.get("FRAMEFACE_LIBFFPROC") or str(self.good)
        pre = pp.make_preprocessor(None, "c") if os.environ.get("FRAMEFACE_LIBFFPROC") else \
            pp.make_preprocessor(str(self.good.parent), "c")
        self.assertEqual((pre.name, pre.path), ("c", path))

    def test_concurrent_calls(self):
        helper = pp.load_helper(str(self.good))
        jobs = [("lcg1", 1, 0), ("lcg2", 0, 200), ("gradient", 1, 200), ("all255", 0, 0)] * 2
        results = [None] * len(jobs)

        def work(i):
            name, phase, col0 = jobs[i]
            results[i] = helper(self.frames[name], phase, col0)
        threads = [threading.Thread(target=work, args=(i,)) for i in range(len(jobs))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        for (name, phase, col0), out in zip(jobs, results):
            self.assertEqual(out, pp.preprocess_swar(self.frames[name], phase, col0))


RUNNER = ffutil.REPO / "tools" / "docker" / "arm64-checks.sh"
RUNNER_PS1 = ffutil.REPO / "tools" / "docker" / "arm64-checks.ps1"
ABI_STAND_IN_C = '#include <stdio.h>\nint main(void)\n{\n    puts("{}");\n    return 0;\n}\n'
PROBE_TEST = '''\
import json, os, subprocess, unittest
class Probe(unittest.TestCase):
    def test_env(self):
        lib, abi = os.environ.get("FRAMEFACE_LIBFFPROC"), os.environ.get("FRAMEFACE_ABI_DUMP")
        print("RUNNER-ENV " + json.dumps({
            "lib": lib, "lib_file": bool(lib) and os.path.isfile(lib), "abi": abi,
            "lib_in_cwd": bool(lib) and os.path.samefile(os.path.dirname(lib), os.getcwd()),
            "abi_in_cwd": bool(abi) and os.path.samefile(os.path.dirname(abi), os.getcwd()),
            "abi_out": subprocess.run([abi], capture_output=True, text=True).stdout if abi else None,
            "cwd": os.getcwd(), "native": os.environ.get("FRAMEFACE_REQUIRE_NATIVE"),
            "nobytecode": os.environ.get("PYTHONDONTWRITEBYTECODE")}), flush=True)
        self.assertNotEqual(os.environ.get("RUNNER_PROBE_FAIL"), "1", "failure requested")
'''


class RunnerScriptTest(unittest.TestCase):
    """tools/docker/arm64-checks.sh (§11.1): every artefact optional, pattern arguments, no bash -n
    of install.sh; on Linux a real run against a mini repo with and without csrc/abi_dump.c."""

    def test_static_rules(self):
        text = RUNNER.read_text(encoding="ascii")
        self.assertNotIn("\r", text)
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertIn("set -eu", code)
        for artefact in ("abi_dump.c", "ffproc.c"):
            self.assertIn('if [ -f "$R/headset/frameface/csrc/%s" ]; then' % artefact, code)
        self.assertIn("[ $# -gt 0 ] || set -- 'test_ff_*.py'", code)
        self.assertIn('FRAMEFACE_REQUIRE_NATIVE=1 PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover '
                      '-s "$R/tools/tests" -p "$p" -v', code)
        self.assertNotIn("bash -n", code)
        self.assertNotIn("install.sh", code)
        ps1 = RUNNER_PS1.read_text(encoding="ascii")
        for needle in ("'--platform', 'linux/arm64'", "'-e', 'FRAMEFACE_NO_FPS_ASSERT=1'", ":/src:ro'",
                       "'sh', '/src/tools/docker/arm64-checks.sh', '/src') + $patterns", "exit $LASTEXITCODE"):
            self.assertIn(needle, ps1)

    def run_runner(self, repo, cwd, *patterns, fail=False):
        env = dict(os.environ, RUNNER_PROBE_FAIL="1" if fail else "0")
        env.pop("FRAMEFACE_LIBFFPROC", None)
        env.pop("FRAMEFACE_ABI_DUMP", None)
        return subprocess.run(["sh", str(RUNNER), str(repo), *patterns], cwd=str(cwd), env=env,
                              capture_output=True, text=True, timeout=600)

    def test_runs_with_and_without_abi_dump(self):
        ffutil.require_native("sh", "gcc", "python3", reason="runs tools/docker/arm64-checks.sh")
        for with_abi in (False, True):
            with self.subTest(abi_dump=with_abi), tempfile.TemporaryDirectory(prefix="ffrunner-") as tmp:
                repo = Path(tmp, "mini repo")
                csrc = repo / "headset" / "frameface" / "csrc"
                csrc.mkdir(parents=True)
                (repo / "tools" / "tests").mkdir(parents=True)
                (csrc / "ffproc.c").write_bytes(SRC.read_bytes())
                if with_abi:
                    (csrc / "abi_dump.c").write_text(ABI_STAND_IN_C, encoding="ascii")
                (repo / "tools" / "tests" / "test_ff_zz_probe.py").write_text(PROBE_TEST, encoding="ascii")
                before = sorted(str(p) for p in repo.rglob("*"))
                # without abi_dump.c: an absolute repo path; with it: a relative one
                r = self.run_runner(repo if not with_abi else repo.name, tmp, "test_ff_zz_probe.py")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(sorted(str(p) for p in repo.rglob("*")), before)     # nothing written
                line = next((x for x in r.stdout.splitlines() if x.startswith("RUNNER-ENV ")), None)
                self.assertIsNotNone(line, r.stdout + r.stderr)
                env = json.loads(line[len("RUNNER-ENV "):])
                self.assertTrue(env["lib_file"] and env["lib_in_cwd"], env)          # built and run in the temp dir
                self.assertFalse(env["cwd"].startswith(os.path.realpath(str(repo))), env)
                self.assertEqual((env["native"], env["nobytecode"]), ("1", "1"))
                if with_abi:
                    self.assertTrue(env["abi_in_cwd"], env)
                    self.assertEqual(env["abi_out"], "{}\n")
                else:
                    self.assertIsNone(env["abi"])
                r = self.run_runner(repo, tmp, "test_ff_zz_probe.py", fail=True)
                self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)          # a failing test fails the run


if __name__ == "__main__":
    unittest.main()
