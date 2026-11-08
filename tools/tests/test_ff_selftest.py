# SPDX-License-Identifier: MIT
"""The install gate (spec §11.3 test_ff_selftest, task T4): exit codes 0/2/3/4 (ABI mismatch, a failed
simulation, a rejected C helper, missing TurboJPEG), --quick in < 2 s, the result file (--result,
default the state dir's selftest.json) with the version, isolation from a pre-filled real state dir
and the real lock, the >= 3 parts in 30 s rule under FRAMEFACE_NO_FPS_ASSERT=1, and the
FRAMEFACE_SELFTEST_FORCE hook. HOME and USERPROFILE point at a temp tree in every case."""
import ffutil  # first: installs the port guard

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ffutil.setup_path()
from frameface import cli, selftest  # noqa: E402
from frameface.config import default_paths  # noqa: E402
from frameface.jpeg import JpegError, StubJpegEncoder  # noqa: E402
from frameface.preprocess import SwarPreprocessor  # noqa: E402
from frameface.usbfs import abi_table  # noqa: E402

CSRC = str(ffutil.FRAMEFACE_DIR / "csrc")
RESULT_KEYS = {"v", "version", "exit", "abi", "pixels", "jpeg", "sim", "when"}


def native_jpeg():
    """The real TurboJPEG encoder on Linux (None: let the selftest load it), the stub elsewhere."""
    if sys.platform.startswith("linux"):
        try:
            ffutil.require_native("turbojpeg", reason="the selftest's JPEG step")
            return None
        except unittest.SkipTest:
            pass
    return StubJpegEncoder()


class Gate(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ffgate-")
        self.addCleanup(shutil.rmtree, self.home, True)
        patcher = mock.patch.dict(os.environ, {"HOME": self.home, "USERPROFILE": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in ("FRAMEFACE_SELFTEST_FORCE",):
            os.environ.pop(var, None)
        self.lines = []
        self.result = os.path.join(self.home, "frameface.new", "selftest.json")

    def gate(self, **kw):
        kw.setdefault("result_path", self.result)
        kw.setdefault("out", self.lines.append)
        kw.setdefault("lib_dir", None)
        return selftest.run_selftest(**kw)

    def read(self, path=None):
        with open(path or self.result, encoding="utf-8") as f:
            return json.load(f)

    def test_full_run_is_isolated_and_exits_0(self):
        real = default_paths(self.home)
        os.makedirs(real.state_dir)
        files = {"runtime.json": b'{"v": 1, "boot_id": "b", "dirty": true}\n',
                 "profile.json": b'{"v": 1, "desc_sha1": "x", "strategy": "S2"}\n',
                 "events.jsonl": b'{"t": "x", "bt": 1.0, "ev": "start"}\n'}
        for name, data in files.items():
            with open(os.path.join(real.state_dir, name), "wb") as f:
                f.write(data)
        before = {n: os.stat(os.path.join(real.state_dir, n)).st_mtime_ns for n in files}
        lock_existed = os.path.exists(real.lock)
        time.sleep(0.05)
        encoder = native_jpeg()
        with mock.patch("frameface.usbfs.LinuxSys", side_effect=AssertionError("real usbfs used")), \
                mock.patch("frameface.service.WornMonitor", side_effect=AssertionError("real eye server read")):
            code = self.gate(encoder=encoder, result_path=None)
        res = self.read(real.selftest)                                  # default: the state dir's file
        self.assertEqual((code, res["exit"], res["sim"], res["version"], res["v"]), (0, 0, "ok", "0.6.0", 1),
                         self.lines)
        self.assertEqual(set(res), RESULT_KEYS)
        if os.environ.get("FRAMEFACE_LIBFFPROC"):                       # the container: the real C helper ...
            self.assertEqual(res["pixels"], "c")
        if os.environ.get("FRAMEFACE_ABI_DUMP"):                        # ... the real abi_dump ...
            self.assertEqual(res["abi"], "ok")
        if encoder is None:                                             # ... and TurboJPEG
            self.assertIn(res["jpeg"], ("tj3", "tj2"))
        self.assertEqual(sorted(os.listdir(real.state_dir)), sorted(list(files) + ["selftest.json"]))
        for name, data in files.items():
            path = os.path.join(real.state_dir, name)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), data, name)
            self.assertEqual(os.stat(path).st_mtime_ns, before[name], name)
        if not lock_existed:
            self.assertFalse(os.path.exists(real.lock))                 # the real flock was never taken
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config")))
        self.assertIn("[ok] sim: stream, teardown and trace invariants", self.lines)

    def test_abi_mismatch_exits_2(self):
        wrong = abi_table()
        wrong["sizes"] = dict(wrong["sizes"], urb=64)
        with mock.patch.object(selftest, "_run_abi_dump", return_value=wrong):
            code = self.gate(quick=True, encoder=StubJpegEncoder())
        res = self.read()
        self.assertEqual((code, res["abi"], res["sim"]), (2, "fail", "skip"))
        self.assertTrue(any("sizes.urb" in line for line in self.lines), self.lines)
        with mock.patch.object(selftest, "_run_abi_dump", return_value=abi_table()):
            self.assertEqual(self.gate(quick=True, encoder=StubJpegEncoder()), 0)
        self.assertEqual(self.read()["abi"], "ok")

    def test_abi_dump_binary_mismatch_exits_2_on_linux(self):
        ffutil.require_native("sh", reason="an abi_dump stand-in script")
        lib = os.path.join(self.home, "lib")
        os.makedirs(lib)
        script = os.path.join(lib, "abi_dump")
        with open(script, "w") as f:
            f.write("#!/bin/sh\necho '{\"sizes\": {\"urb\": 64}}'\n")
        os.chmod(script, 0o755)
        self.assertEqual(self.gate(lib_dir=lib, quick=True, encoder=StubJpegEncoder()), 2)
        self.assertEqual(self.read()["abi"], "fail")

    def test_failed_simulation_exits_2(self):
        code = self.gate(encoder=StubJpegEncoder(), sim_knobs={"scene": "bars"})     # test bars: nothing live
        res = self.read()
        self.assertEqual((code, res["sim"]), (2, "fail"))
        self.assertTrue(any(line.startswith("[fail] sim:") and "parts within" in line for line in self.lines),
                        self.lines)

    def test_parity_failure_exits_3(self):
        bad = SwarPreprocessor("parity", os.path.join(self.home, "lib", "libffproc.so"))
        with mock.patch.object(selftest, "make_preprocessor", return_value=bad):
            code = self.gate(quick=True, encoder=StubJpegEncoder())
        res = self.read()
        self.assertEqual((code, res["pixels"], res["jpeg"]), (3, "swar", "stub"))
        with mock.patch.object(selftest, "make_preprocessor", return_value=bad), \
                mock.patch.object(selftest.JpegEncoder, "load", side_effect=JpegError("no library")):
            self.assertEqual(self.gate(quick=True), 3)                  # 3 hides 4 ...
        self.assertEqual(self.read()["jpeg"], "missing")                 # ... but the result records it

    def test_corrupt_helper_exits_3_on_linux(self):
        ffutil.require_native("gcc", reason="a corrupted C helper build")
        lib = os.path.join(self.home, "lib")
        os.makedirs(lib)
        subprocess.run(["gcc", "-std=c99", "-O2", "-fPIC", "-shared", "-DFF_CORRUPT_FOR_TEST", "-o",
                        os.path.join(lib, "libffproc.so"), os.path.join(CSRC, "ffproc.c")], check=True)
        self.assertEqual(self.gate(lib_dir=lib, quick=True, encoder=StubJpegEncoder()), 3)
        self.assertEqual(self.read()["pixels"], "swar")

    def test_missing_turbojpeg_exits_4(self):
        with mock.patch.object(selftest.JpegEncoder, "load", side_effect=JpegError("no libturbojpeg")):
            code = self.gate(quick=True)
        res = self.read()
        self.assertEqual((code, res["jpeg"], res["sim"]), (4, "missing", "skip"))

    def test_quick_is_fast_and_writes_the_result_file(self):
        staged = os.path.dirname(self.result)                           # install.sh's ~/frameface.new (0755)
        os.makedirs(staged)
        if os.name == "posix":
            os.chmod(staged, 0o755)
        t0 = time.monotonic()
        code = self.gate(quick=True, encoder=StubJpegEncoder())
        took = time.monotonic() - t0
        print("selftest --quick %.2f s" % took)
        if ffutil.fps_asserts():
            self.assertLess(took, 2.0)
        res = self.read()
        self.assertEqual((code, res["exit"], res["version"], res["sim"]), (0, 0, "0.6.0", "skip"))
        self.assertEqual(set(res), RESULT_KEYS)
        if os.name == "posix":
            self.assertEqual(os.stat(staged).st_mode & 0o777, 0o755)     # the folder's mode is left alone
            self.assertEqual(os.stat(self.result).st_mode & 0o777, 0o600)

    def test_no_fps_assert_accepts_3_parts_in_30_s(self):
        with mock.patch.dict(os.environ, {"FRAMEFACE_NO_FPS_ASSERT": "1"}):
            code = self.gate(encoder=StubJpegEncoder(), sim_knobs={"fps": 1.0})       # 1 frame per second
        self.assertEqual((code, self.read()["sim"]), (0, "ok"), self.lines)
        if ffutil.fps_asserts():                                        # natively the rule is 10 parts in 5 s
            with mock.patch.dict(os.environ, {"FRAMEFACE_NO_FPS_ASSERT": ""}):
                self.assertEqual(self.gate(encoder=StubJpegEncoder(), sim_knobs={"fps": 1.0}), 2)

    def test_force_hook(self):
        with mock.patch.dict(os.environ, {"FRAMEFACE_SELFTEST_FORCE": "3"}):
            t0 = time.monotonic()
            self.assertEqual(self.gate(), 3)
            self.assertLess(time.monotonic() - t0, 1.0)
        res = self.read()
        self.assertEqual((res["exit"], res["version"], res["forced"]), (3, "0.6.0", True))

    def test_cli_selftest_json_and_result(self):
        raw = io.StringIO()
        with mock.patch.object(selftest.JpegEncoder, "load", side_effect=JpegError("none")), \
                contextlib.redirect_stdout(raw):
            code = cli.main(["selftest", "--quick", "--json", "--result", self.result])
        doc = json.loads(raw.getvalue())
        self.assertEqual((code, doc["exit"], doc["jpeg"]), (4, 4, "missing"))
        self.assertEqual(doc, self.read())


if __name__ == "__main__":
    unittest.main()
