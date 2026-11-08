# SPDX-License-Identifier: MIT
"""PC test tools (spec §11.3 test_ff_pc_tools, task T6): tools/send_test_face.py and tools/osc_monitor.py.

Every socket here is on a free loopback port. The tools run in this process, where ffutil's port guard is
active, or in child processes that install the same guard before the tool's own code runs (GUARDED_RUN), so a
tool that ignored --port could not reach VRCFaceTracking, VRChat or Baballonia either. The child guard is proven
the T0 way: on a free port forbidden only for that child, with a bound receiver that must stay empty.
"""
import ffutil  # first: installs and self-checks the port guard before any socket exists

import contextlib
import io
import json
import math
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import threading
import time
import types
import unittest

TESTS = Path(__file__).resolve().parent
TOOLS = ffutil.REPO / "tools"
SEND_TEST_FACE = TOOLS / "send_test_face.py"
OSC_MONITOR = TOOLS / "osc_monitor.py"

# Spec §8.3 send order, written out here independently of the tools.
BABBLE45 = (
    "cheekPuffLeft", "cheekPuffRight", "cheekSuckLeft", "cheekSuckRight", "jawOpen", "jawForward", "jawLeft",
    "jawRight", "noseSneerLeft", "noseSneerRight", "mouthFunnel", "mouthPucker", "mouthLeft", "mouthRight",
    "mouthRollUpper", "mouthRollLower", "mouthShrugUpper", "mouthShrugLower", "mouthClose", "mouthSmileLeft",
    "mouthSmileRight", "mouthFrownLeft", "mouthFrownRight", "mouthDimpleLeft", "mouthDimpleRight",
    "mouthUpperUpLeft", "mouthUpperUpRight", "mouthLowerDownLeft", "mouthLowerDownRight", "mouthPressLeft",
    "mouthPressRight", "mouthStretchLeft", "mouthStretchRight", "tongueOut", "tongueUp", "tongueDown",
    "tongueLeft", "tongueRight", "tongueRoll", "tongueBendDown", "tongueCurlUp", "tongueSquish", "tongueFlat",
    "tongueTwistLeft", "tongueTwistRight",
)
JAW_OPEN_HALF = bytes.fromhex("2f 6a 61 77 4f 70 65 6e 00 00 00 00 2c 66 00 00 3f 00 00 00")   # brief §e.10
JAW_OPEN_ZERO = bytes.fromhex("2f 6a 61 77 4f 70 65 6e 00 00 00 00 2c 66 00 00 00 00 00 00")
FRAMES_02S = 12                                     # --seconds 0.2 at the default 60 fps

# Child bootstrap: ffutil's guard first (plus extra ports from FF_T6_EXTRA_FORBIDDEN), then the tool as __main__.
GUARDED_RUN = (
    "import os, runpy, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import ffutil\n"
    "assert ffutil.guard_installed()\n"
    "extra = [int(p) for p in os.environ.get('FF_T6_EXTRA_FORBIDDEN', '').split(',') if p]\n"
    "script = sys.argv[2]\n"
    "sys.argv = [script] + sys.argv[3:]\n"
    "with ffutil.extra_forbidden(extra):\n"
    "    runpy.run_path(script, run_name='__main__')\n"
)


def load_tool(path):
    """The tool as a module object, without writing bytecode next to it."""
    module = types.ModuleType("t6_" + path.stem)
    module.__file__ = str(path)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


send_test_face = load_tool(SEND_TEST_FACE)
osc_monitor = load_tool(OSC_MONITOR)


def guarded(script, *args, extra_forbidden=(), timeout=30, **popen):
    """Run a tool in a child process behind ffutil's port guard; returns subprocess.run's result."""
    env = dict(os.environ, FF_T6_EXTRA_FORBIDDEN=",".join(str(p) for p in extra_forbidden))
    return subprocess.run([sys.executable, "-I", "-B", "-c", GUARDED_RUN, str(TESTS), str(script), *map(str, args)],
                          capture_output=True, text=True, timeout=timeout, env=env, **popen)


def parse_float_message(datagram):
    """(address, value) of a single "/name ,f <float32 BE>" message; fails on anything else."""
    end = datagram.index(b"\0")
    address = datagram[:end].decode("ascii")
    pos = end + 1 + (-(end + 1) % 4)
    assert datagram[end:pos] == b"\0" * (pos - end), datagram
    assert datagram[pos:pos + 4] == b",f\0\0", datagram
    assert len(datagram) == pos + 8, datagram
    return address, struct.unpack(">f", datagram[pos + 4:])[0]


class Receiver:
    """A bound, non-blocking UDP receiver on a free loopback port that drains into a list."""

    def __init__(self, test):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        test.addCleanup(self.sock.close)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 22)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        test.assertNotIn(self.port, ffutil.FORBIDDEN_TEST_PORTS)
        self.sock.setblocking(False)
        self.datagrams = []

    def drain(self, quiet=0.3, until=None, limit=10.0):
        """Collect datagrams until `until()` is true (if given) and nothing arrived for `quiet` seconds."""
        deadline = time.monotonic() + limit
        last = time.monotonic()
        while time.monotonic() < deadline:
            if select.select([self.sock], [], [], 0.02)[0]:
                while True:
                    try:
                        self.datagrams.append(self.sock.recv(65536))
                    except BlockingIOError:
                        break
                last = time.monotonic()
            elif (until is None or until()) and time.monotonic() - last >= quiet:
                break
        return self.datagrams


def run_in_thread(target, *args):
    result = {}

    def body():
        try:
            result["value"] = target(*args)
        except BaseException as e:      # SystemExit included: the test inspects it
            result["error"] = e

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread, result


class SendTestFace(unittest.TestCase):
    def send(self, *args):
        receiver = Receiver(self)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            thread, result = run_in_thread(send_test_face.main, ["--port", str(receiver.port), *map(str, args)])
            receiver.drain(until=lambda: not thread.is_alive())
            thread.join(10)
        self.assertEqual(result, {"value": 0}, out.getvalue())
        return [parse_float_message(d) for d in receiver.datagrams], receiver.datagrams, out.getvalue()

    def frames(self, messages):
        self.assertEqual(len(messages) % 45, 0)
        return [messages[k:k + 45] for k in range(0, len(messages), 45)]

    def test_default_run_sends_45_distinct_addresses_per_frame(self):
        messages, datagrams, out = self.send("--seconds", "0.2")
        frames = self.frames(messages)
        self.assertEqual(len(frames), FRAMES_02S)
        for frame in frames:
            self.assertEqual([a for a, _ in frame], ["/" + n for n in BABBLE45])
            self.assertEqual(len({a for a, _ in frame}), 45)
            self.assertTrue(all(math.isfinite(v) and 0.0 <= v <= 1.0 for _, v in frame))
        # The sweep starts with row 0 (cheekPuffLeft); /jawOpen is 0.0 in the first second, byte for byte.
        jaw = [d for d in datagrams if d.startswith(b"/jawOpen\0")]
        self.assertEqual(jaw, [JAW_OPEN_ZERO] * FRAMES_02S)
        self.assertGreater(frames[-1][0][1], 0.0)
        self.assertIn("sent %d frames (%d messages)" % (FRAMES_02S, FRAMES_02S * 45), out)

    def test_exact_jaw_open_bytes(self):
        messages, datagrams, _ = self.send("--seconds", "0.2", "--pattern", "still", "--set", "jawOpen=0.5", "--quiet")
        self.assertEqual(len(datagrams), FRAMES_02S * 45)
        self.assertEqual([d for d in datagrams if d.startswith(b"/jawOpen\0")], [JAW_OPEN_HALF] * FRAMES_02S)
        self.assertEqual(datagrams[4], JAW_OPEN_HALF)               # row 4 of every frame
        self.assertEqual(send_test_face.osc_float("/jawOpen", 0.5), JAW_OPEN_HALF)
        for frame in self.frames(messages):
            self.assertEqual(frame[4], ("/jawOpen", 0.5))
            self.assertEqual([v for i, (_, v) in enumerate(frame) if i != 4], [0.0] * 44)

    def test_prefix_rate_and_pins(self):
        messages, _, _ = self.send("--seconds", "0.1", "--rate", "100", "--pattern", "still", "--prefix", "/babble",
                                   "--set", "/MOUTHFUNNEL=0.4", "--set", "tongueOut=1", "--quiet")
        frames = self.frames(messages)
        self.assertEqual(len(frames), 10)
        for frame in frames:
            self.assertEqual([a for a, _ in frame], ["/babble/" + n for n in BABBLE45])
            self.assertEqual((frame[10][1], frame[33][1]), (struct.unpack(">f", struct.pack(">f", 0.4))[0], 1.0))

    def test_port_is_required(self):
        for argv in ([], ["--seconds", "0.1"], ["--host", "127.0.0.1"]):
            err = io.StringIO()
            with self.subTest(argv=argv), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                send_test_face.main(argv)
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("the following arguments are required: --port", err.getvalue())

    def test_bad_arguments_are_usage_errors(self):
        receiver = Receiver(self)
        for bad in (["--port", "0"], ["--port", "70000"], ["--port", "x"], ["--set", "nope=1"], ["--set", "jawOpen=nan"],
                    ["--set", "jawOpen=1e39"], ["--set", "jawOpen"], ["--rate", "0"], ["--seconds", "-1"],
                    ["--prefix", "babble"], ["--pattern", "random"]):
            argv = (["--port", str(receiver.port)] if "--port" not in bad else []) + bad
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                send_test_face.main(argv)
            self.assertEqual(cm.exception.code, 2)
        self.assertEqual(receiver.drain(quiet=0.1), [])


class OscMonitor(unittest.TestCase):
    def test_parses_what_send_test_face_sends(self):
        monitor = osc_monitor.Monitor("127.0.0.1", 0)
        self.addCleanup(monitor.close)
        port = monitor.address[1]
        self.assertNotIn(port, ffutil.FORBIDDEN_TEST_PORTS)
        records = []
        thread, result = run_in_thread(monitor.run, 10.0, FRAMES_02S * 45, records.append)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(send_test_face.main(["--port", str(port), "--seconds", "0.2", "--pattern", "still",
                                                  "--set", "jawOpen=0.5", "--quiet"]), 0)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", result)
        self.assertEqual((monitor.messages, monitor.datagrams, monitor.malformed), (FRAMES_02S * 45, FRAMES_02S * 45, 0))
        self.assertTrue(all("error" not in r and r["types"] == "f" and len(r["args"]) == 1 for r in records))
        for k in range(0, len(records), 45):
            frame = records[k:k + 45]
            self.assertEqual([r["address"] for r in frame], ["/" + n for n in BABBLE45])
            self.assertEqual(frame[4]["args"], [0.5])
        self.assertEqual(sorted(monitor.counts.values()), [FRAMES_02S] * 45)

    def test_parse_packet(self):
        parse = osc_monitor.parse_packet
        self.assertEqual(parse(JAW_OPEN_HALF), [("/jawOpen", "f", [0.5])])
        bundle = b"#bundle\0" + struct.pack(">Q", 1)
        for m in (JAW_OPEN_HALF, send_test_face.osc_float("/mouthClose", 0.25)):
            bundle += struct.pack(">i", len(m)) + m
        self.assertEqual(parse(bundle), [("/jawOpen", "f", [0.5]), ("/mouthClose", "f", [0.25])])
        nested = b"#bundle\0" + struct.pack(">Q", 1) + struct.pack(">i", len(bundle)) + bundle
        self.assertEqual(len(parse(nested)), 2)
        deeper = b"#bundle\0" + struct.pack(">Q", 1) + struct.pack(">i", len(nested)) + nested
        with self.assertRaises(osc_monitor.OscError):
            parse(deeper)
        keeper_true = send_test_face.osc_string("/avatar/parameters/LipTrackingActive") + send_test_face.osc_string(",T")
        keeper_false = send_test_face.osc_string("/avatar/parameters/ExpressionTrackingActive") + send_test_face.osc_string(",F")
        self.assertEqual((len(keeper_true), len(keeper_false)), (44, 48))
        self.assertEqual(parse(keeper_true), [("/avatar/parameters/LipTrackingActive", "T", [True])])
        self.assertEqual(parse(keeper_false), [("/avatar/parameters/ExpressionTrackingActive", "F", [False])])
        mixed = (send_test_face.osc_string("/x") + send_test_face.osc_string(",isb") + struct.pack(">i", -3)
                 + send_test_face.osc_string("hi") + struct.pack(">i", 3) + b"abc\0")
        self.assertEqual(parse(mixed), [("/x", "isb", [-3, "hi", "616263"])])
        for bad in (b"", JAW_OPEN_HALF[:-1], JAW_OPEN_HALF[:8], b"/jawOpen", JAW_OPEN_HALF + b"\0\0\0\0",
                    b"/jawOpen\0\0\0x,f\0\0" + struct.pack(">f", 0.5), b"/jawOpen\0\0\0\0" + struct.pack(">f", 0.5),
                    b"/jawOpen\0\0\0\0,q\0\0" + struct.pack(">f", 0.5), b"jawOpen\0,f\0\0" + struct.pack(">f", 0.5),
                    b"#bundle\0", b"#bundle\0" + struct.pack(">Q", 1) + struct.pack(">i", 6) + b"/a\0\0,f"):
            with self.subTest(bad=bad), self.assertRaises(osc_monitor.OscError):
                parse(bad)

    def test_main_prints_json_lines_and_counts_malformed_datagrams(self):
        monitor_port = ffutil.free_port("udp")
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            thread, result = run_in_thread(osc_monitor.main, ["--port", str(monitor_port), "--json", "--count", "3",
                                                              "--seconds", "10"], out)
            deadline = time.monotonic() + 5
            while "listening on" not in err.getvalue() and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertIn("listening on 127.0.0.1:%d" % monitor_port, err.getvalue())
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for datagram in (b"not osc", JAW_OPEN_HALF, send_test_face.osc_float("/mouthClose", 0.25),
                             send_test_face.osc_string("/avatar/parameters/LipTrackingActive") + send_test_face.osc_string(",T")):
                sender.sendto(datagram, ("127.0.0.1", monitor_port))
        thread.join(10)
        self.assertEqual(result, {"value": 0})
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertIn("error", lines[0])
        self.assertEqual([(r["address"], r["types"], r["args"]) for r in lines[1:4]],
                         [("/jawOpen", "f", [0.5]), ("/mouthClose", "f", [0.25]), ("/avatar/parameters/LipTrackingActive", "T", [True])])
        self.assertEqual(lines[4]["summary"]["messages"], 3)
        self.assertEqual(lines[4]["summary"]["malformed"], 1)

    def test_port_is_required(self):
        for argv in ([], ["--json"], ["--seconds", "1"]):
            err = io.StringIO()
            with self.subTest(argv=argv), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                osc_monitor.main(argv, io.StringIO())
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("the following arguments are required: --port", err.getvalue())

    def test_port_in_use_is_reported(self):
        receiver = Receiver(self)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = osc_monitor.main(["--port", str(receiver.port), "--seconds", "1"], io.StringIO())
        self.assertEqual(code, 1)
        self.assertIn("cannot listen on 127.0.0.1:%d" % receiver.port, err.getvalue())


class CommandLine(unittest.TestCase):
    """The real command lines, in child processes behind ffutil's port guard."""

    def test_missing_port_is_a_usage_error(self):
        for script in (SEND_TEST_FACE, OSC_MONITOR):
            with self.subTest(script=script.name):
                run = guarded(script)
                self.assertEqual(run.returncode, 2, run.stderr)
                self.assertIn("usage: %s" % script.name, run.stderr)
                self.assertIn("the following arguments are required: --port", run.stderr)
                self.assertEqual(run.stdout, "")

    def test_child_guard_is_active(self):
        receiver = Receiver(self)
        run = guarded(SEND_TEST_FACE, "--port", receiver.port, "--seconds", "0.1", extra_forbidden=[receiver.port])
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("AssertionError: forbidden port %d" % receiver.port, run.stderr)
        self.assertEqual(receiver.drain(quiet=0.2), [])

    def test_monitor_and_sender_end_to_end(self):
        port = ffutil.free_port("udp")
        monitor = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", GUARDED_RUN, str(TESTS), str(OSC_MONITOR), "--port", str(port), "--json",
             "--count", str(FRAMES_02S * 45), "--seconds", "20"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(os.environ, FF_T6_EXTRA_FORBIDDEN=""))
        self.addCleanup(lambda: monitor.poll() is None and monitor.kill())
        ready = monitor.stderr.readline()
        self.assertIn("listening on 127.0.0.1:%d" % port, ready)
        sender = guarded(SEND_TEST_FACE, "--port", port, "--seconds", "0.2", "--pattern", "still", "--set", "jawOpen=0.5")
        self.assertEqual(sender.returncode, 0, sender.stderr)
        self.assertIn("sent %d frames" % FRAMES_02S, sender.stdout)
        out, err = monitor.communicate(timeout=20)
        self.assertEqual(monitor.returncode, 0, err)
        lines = [json.loads(line) for line in out.splitlines()]
        records, summary = lines[:-1], lines[-1]["summary"]
        self.assertEqual(summary["messages"], FRAMES_02S * 45)
        self.assertEqual(summary["addresses"], 45)
        for k in range(0, len(records), 45):
            self.assertEqual([r["address"] for r in records[k:k + 45]], ["/" + n for n in BABBLE45])
        self.assertEqual({r["args"][0] for r in records if r["address"] == "/jawOpen"}, {0.5})


if __name__ == "__main__":
    unittest.main()
