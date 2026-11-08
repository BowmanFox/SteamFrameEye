# SPDX-License-Identifier: MIT
"""End to end over HTTP (spec §11.3 test_ff_e2e_http, task T4): the threaded Service on the real clock
against FakeKernel at 60 fps (one completion event per URB), StubPreprocessor and StubJpegEncoder,
HTTP on 127.0.0.1 with a free port, read by BabaClient (Baballonia's parser). The frame rate is
asserted only with fps_asserts(). Linux only: SIGTERM to a `frameface_main.py sim` subprocess."""
import ffutil  # first: installs the port guard

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ffutil.setup_path()
from frameface.clock import RealClock  # noqa: E402
from frameface.config import Config, temp_paths  # noqa: E402
from frameface.jpeg import StubJpegEncoder, decode_gray  # noqa: E402
from frameface.logutil import Log  # noqa: E402
from frameface.preprocess import StubPreprocessor  # noqa: E402
from frameface.service import Service  # noqa: E402
from frameface.sim import VftModel, check_trace, make_rig  # noqa: E402
from frameface.sim.babaclient import BabaClient  # noqa: E402
from frameface.worn import NO_WORN  # noqa: E402

MAIN = str(ffutil.FRAMEFACE_DIR / "frameface_main.py")
SLOW = not ffutil.fps_asserts()                 # QEMU: generous waits, no rate asserts


class E2E(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ffe2e-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.rig = make_rig(VftModel(), clock=RealClock(), tmp=os.path.join(self.root, "rig"))
        self.addCleanup(self.rig.close)
        self.paths = temp_paths(os.path.join(self.root, "tree"))

    def start(self, port=0):
        cfg = Config(token="", allow_peers=["127.0.0.0/8"], cpu_io="auto", cpu_work="auto", nice_io=0)
        self.out = io.StringIO()
        log = Log("info", self.paths.events, journal=True, stream=self.out, clock=self.rig.clock)
        svc = Service(cfg, self.paths, sys=self.rig.kernel, clock=self.rig.clock, sys_root=self.rig.sys_root,
                      dev_root=self.rig.dev_root, proc_root=self.rig.proc_root, encoder=StubJpegEncoder(),
                      pre=StubPreprocessor(), worn=NO_WORN, threaded=True, http=True, log=log,
                      bind=("127.0.0.1", port))
        svc.trace_note = self.rig.kernel.note
        svc.startup()
        self.assertTrue(svc.http_port)
        stop = threading.Event()
        th = threading.Thread(target=svc.run, args=(stop,), daemon=True)
        th.start()
        self.addCleanup(th.join, 10)
        self.addCleanup(svc.stop)
        return svc, th

    def client(self, svc):
        c = BabaClient("http://127.0.0.1:%d/stream" % svc.http_port).start()
        self.addCleanup(c.stop)
        return c

    def until(self, pred, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            time.sleep(0.02)
        return pred()

    def check(self, svc):
        k = self.rig.kernel
        self.assertEqual((k.violations, check_trace(k.ops), k.inflight(), svc.errors), ([], [], 0, []))

    def test_stream_rate_and_clean_stop(self):
        svc, th = self.start()
        c = self.client(svc)
        self.assertTrue(c.wait_first_frame(13.0))
        self.assertTrue(self.until(lambda: svc.state == "STREAMING" and svc.published > 30, 30 if SLOW else 10))
        time.sleep(0.5)
        t0, n0 = time.monotonic(), c.frames
        time.sleep(3.0)
        rate = (c.frames - n0) / (time.monotonic() - t0)
        print("BabaClient %.1f parts/s, fps_in %s" % (rate, svc.status()["stream"]["fps_in"]))
        if ffutil.fps_asserts():
            self.assertGreaterEqual(rate, 40.0)
        self.assertGreater(c.frames - n0, 0)
        self.assertEqual((c.reconnects, c.decode_errors), (0, 0))
        svc.stop()
        th.join(10)
        self.assertEqual(svc.state, "STOPPED")
        self.assertEqual(svc.drivers[-1].teardown_path, "clean")
        self.check(svc)

    def test_placeholder_without_a_device(self):
        self.rig.kernel.unplug()
        svc, th = self.start()
        t0 = time.monotonic()
        c = self.client(svc)
        self.assertTrue(c.wait_first_frame(13.0))
        self.assertLessEqual(c.first_frame_t - t0, 2.0 if not SLOW else 5.0)
        self.assertGreaterEqual(c.first_frame_t - t0, 1.3)
        self.assertEqual(c.last_jpeg, svc.placeholder)
        self.assertEqual((svc.state, svc.status()["display"]), ("NO_DEVICE", "no_device"))
        time.sleep(2.5)
        self.assertEqual((c.frames, c.reconnects), (1, 0))            # once per connection, then keepalives
        self.assertGreaterEqual(c.keepalives, 1)

    def test_six_second_unplug_keeps_the_client_connected(self):
        svc, th = self.start()
        c = self.client(svc)
        self.assertTrue(self.until(lambda: svc.state == "STREAMING" and c.frames > 20, 30 if SLOW else 10))
        ka0 = c.keepalives
        self.rig.kernel.unplug(replug_after=6.0)
        self.assertTrue(self.until(lambda: svc.state == "NO_DEVICE", 5))
        n0 = c.frames
        time.sleep(5.0)
        self.assertEqual(c.frames, n0)                                 # nothing live while unplugged
        self.assertGreaterEqual(c.keepalives - ka0, 2)                 # every 2 s
        self.assertGreaterEqual(svc.status()["stream"]["keepalives"], 2)
        self.assertTrue(self.until(lambda: c.frames > n0 + 10, 40 if SLOW else 12))   # replug, settle, stream
        self.assertEqual((c.reconnects, svc.counts["unplugs"]), (0, 1))
        self.assertEqual(svc.drivers[0].teardown_path, "lost")

    def test_client_reconnects_after_a_service_restart(self):
        svc, th = self.start()
        port = svc.http_port
        c = self.client(svc)
        self.assertTrue(self.until(lambda: c.frames > 5, 30 if SLOW else 10))
        svc.stop()
        th.join(10)
        t_stop, n0 = time.monotonic(), c.frames
        svc2, th2 = self.start(port)                                   # systemd restarts the unit
        self.assertEqual(svc2.http_port, port)
        self.assertTrue(self.until(lambda: c.reconnects >= 1 and c.frames > n0, 7.0))
        self.assertLessEqual(time.monotonic() - t_stop, 7.0)
        self.assertTrue(svc2.dirty_start is False)                      # the first one tore down cleanly

    def test_sigterm_to_a_sim_subprocess_tears_down_cleanly(self):
        ffutil.require_native(reason="POSIX signals to a child process")
        port = ffutil.free_port("tcp")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.Popen([sys.executable, "-I", "-B", MAIN, "sim", "--port", str(port), "--seconds", "300"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        url = proc.stdout.readline().decode().strip()
        self.assertEqual(url, "http://127.0.0.1:%d/stream/" % port + url.rsplit("/", 1)[-1])
        c = BabaClient(url, decode=decode_gray).start()
        self.addCleanup(c.stop)
        self.assertTrue(c.wait_frames(6, 120 if SLOW else 20), "no live frames from the sim")
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err.decode()[-2000:])
        summary = json.loads(out.decode().strip().splitlines()[-1])["sim_exit"]
        self.assertEqual((summary["teardown"], summary["inflight"], summary["violations"]), ("clean", 0, []))
        self.assertGreater(summary["frames"], 5)
        self.assertGreater(summary["parts_sent"], 4)
        self.assertEqual(c.decode_errors, 0)                            # real JPEGs that decode
        start = next(line for line in err.decode().splitlines() if "I service: start" in line)
        self.assertRegex(start, r"jpeg=tj[23]")                         # TurboJPEG, not the stub
        if os.environ.get("FRAMEFACE_LIBFFPROC"):                       # the runner's C helper passed parity
            self.assertIn("pixels=c", start)


if __name__ == "__main__":
    unittest.main()
