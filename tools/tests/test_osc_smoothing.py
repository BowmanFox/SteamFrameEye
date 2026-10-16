"""Direct OSC filter behavior, dashboard persistence, and actual loopback datagrams."""
import json
import math
from pathlib import Path
import random
import socket
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "headset"))
import eye_smoothing
import frameeye_osc as sender
import frameeye_overlay as overlay


def record(t, yaw=0, pitch=0, openness=(1, 1)):
    direction = (math.tan(math.radians(yaw)), math.tan(math.radians(pitch)), -1)
    return dict(producer_state=1, sample_flag=1, sample_time=t,
                gaze=(direction, direction), cov=((0.0004,) * 3,) * 2,
                fixation=direction, openness=openness)


V5 = sender.Layout(5, sender.EYE_SERVER_ABI[5][0], sender.EYE_SERVER_ABI[5][1], False)


def memory(r, version=5, size=None, initialized=1):
    """An eye server object as the given ABI version lays it out, holding record r."""
    size = sender.EYE_SERVER_ABI[version][0] if size is None else size
    buffer = bytearray(size)
    struct.pack_into("<II", buffer, sender.VERSION_OFF, version, initialized)
    struct.pack_into(sender.EYE_DATA_FMT, buffer, size - sender.RECORD_TAIL,
                     r["producer_state"], 1, r["sample_time"], *r["gaze"][0], *r["gaze"][1],
                     *r["cov"][0], *r["cov"][1], *r["fixation"],
                     *([0.0] * 12), *r["openness"], *([0.0] * 8))
    return buffer


class EyeServerLayout(unittest.TestCase):
    def test_known_versions(self):
        for version, offset in ((4, 0x152), (5, 0x157)):
            with self.subTest(version=version):
                buffer = memory(record(1.5, 10), version)
                layout = sender.detect_layout(buffer, len(buffer))
                self.assertEqual((layout.version, layout.record_offset, layout.guessed), (version, offset, False))
                r = sender.parse_record(buffer, layout.record_offset)
                self.assertEqual(r["sample_time"], 1.5)
                self.assertTrue(sender.record_valid(r, layout.guessed))

    def test_reading_v5_with_the_v4_offset_is_rejected(self):
        buffer = memory(record(1.5, 10), 5)
        self.assertFalse(sender.record_valid(sender.parse_record(buffer, 0x152)))

    def test_not_initialized_and_too_small(self):
        buffer = memory(record(1), 5, initialized=0)
        with self.assertRaises(sender.SourceNotReady) as ctx:
            sender.detect_layout(buffer, len(buffer))
        self.assertEqual(ctx.exception.state, "starting")
        buffer = memory(record(1), 5)[:0x4F000]
        with self.assertRaises(sender.SourceNotReady) as ctx:
            sender.detect_layout(buffer, len(buffer))
        self.assertEqual(ctx.exception.state, "unsupported")

    def test_unknown_version_guesses_offset_and_needs_plausible_samples(self):
        unit = math.sqrt(0.5)
        r = record(2.0)
        r["gaze"] = ((0, 0, -1), (unit, 0, -unit))
        size = sender.EYE_SERVER_ABI[5][0] + 3
        buffer = memory(r, 5, size=size)
        struct.pack_into("<I", buffer, sender.VERSION_OFF, 6)
        layout = sender.detect_layout(buffer, len(buffer))
        self.assertEqual((layout.version, layout.record_offset, layout.guessed), (6, 0x15A, True))
        self.assertTrue(sender.record_valid(sender.parse_record(buffer, layout.record_offset), True))
        r["gaze"] = ((0, 0, -40), r["gaze"][1])
        buffer = memory(r, 5, size=size)
        self.assertFalse(sender.record_valid(sender.parse_record(buffer, 0x15A), True))
        tiny = bytearray(0x1000)
        struct.pack_into("<II", tiny, 0, 6, 1)
        with self.assertRaises(sender.SourceNotReady) as ctx:
            sender.detect_layout(tiny, len(tiny))
        self.assertEqual(ctx.exception.state, "unsupported")

    def test_open_mmap_and_version_change(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "eye-server.mmap")
            Path(path).write_bytes(memory(record(3.0), 4))
            mm, layout, inode = sender.open_mmap(path)
            try:
                self.assertEqual((layout.version, layout.record_offset), (4, 0x152))
                self.assertIn("version 4", sender.source_info(path))
                self.assertFalse(sender.source_changed(path, mm, layout, inode))
                self.assertTrue(sender.source_changed(path, mm, layout._replace(version=5), inode))
            finally:
                mm.close()
            self.assertIn("not available", sender.source_info(str(Path(temp) / "missing")))


class OscSmoothing(unittest.TestCase):
    def test_noise_suppression_and_step_latency(self):
        for rate in (60, 90, 120):
            with self.subTest(rate=rate):
                smoother = eye_smoothing.DirectOscSmoother()
                noise = random.Random(193)
                raw_energy = out_energy = 0
                for i in range(rate * 10):
                    yaw = (noise.random() - 0.5) * 1.5 + (8 if i % 83 == 0 else 0)
                    value = smoother.process(record(i / rate, yaw))[0] * 45
                    if i >= rate:
                        raw_energy += yaw * yaw
                        out_energy += value * value
                self.assertLess(out_energy, raw_energy * 0.1)
                smoother.reset()
                for i in range(rate):
                    smoother.process(record(i / rate))
                outputs = [smoother.process(record(1 + i / rate, 15))[0] * 45 for i in range(rate)]
                reach = next(i / rate for i, x in enumerate(outputs) if x >= 13.5)
                self.assertLessEqual(reach, 0.12)
                self.assertLessEqual(max(outputs), 15.00001)

    def test_spikes_rejected_on_all_six_gaze_axes(self):
        smoother = eye_smoothing.DirectOscSmoother()
        smoother.process(record(0))
        smoother.process(record(1 / 90))
        for i in range(2, 12):
            result = smoother.process(record(i / 90, 12 if i == 2 else 0, -10 if i == 2 else 0))
            self.assertEqual(result[:6], (0,) * 6)

    def test_blinks_hold_gaze_and_winks_follow_open_eye(self):
        smoother = eye_smoothing.DirectOscSmoother()
        start = smoother.process(record(0, 5))
        for i in range(1, 10):
            closed = record(i / 90, 30, 20, (0, 0))
            closed["cov"] = ((0.05,) * 3,) * 2
            result = smoother.process(closed)
            self.assertEqual(result[:6], start[:6])
        self.assertEqual(result[6:], (0, 0))
        for i in range(10, 45):
            wink = record(i / 90, 7, openness=(0, 1))
            wink["gaze"] = ((100, 100, -1), wink["gaze"][1])
            wink["cov"] = ((0.05,) * 3, (0.0004,) * 3)
            result = smoother.process(wink)
        self.assertAlmostEqual(result[0] * 45, 7, delta=0.05)
        self.assertEqual(result[0:2], result[2:4])
        self.assertEqual(result[2:4], result[4:6])
        self.assertEqual(result[6:], (0, 1))

    def test_short_blink_and_lid_dropout(self):
        smoother = eye_smoothing.DirectOscSmoother()
        smoother.process(record(0))
        dropout = smoother.process(record(1 / 90, openness=(0, 1)))
        self.assertEqual(dropout[6:], (1, 1))
        smoother.process(record(2 / 90))
        blink = [smoother.process(record((3 + i) / 90, openness=(0, 0)))[6] for i in range(6)]
        self.assertLess(min(blink), 0.03, "a 67 ms blink must reach closed")

    def test_clock_reset_and_reconnect_clear_history(self):
        smoother = eye_smoothing.DirectOscSmoother()
        smoother.process(record(100, -10, openness=(0, 0)))
        reset = smoother.process(record(0, 12, -8))
        self.assertAlmostEqual(reset[0] * 45, 12)
        self.assertEqual(reset[6:], (1, 1))
        reconnect = smoother.process(record(10, -7))
        self.assertAlmostEqual(reconnect[0] * 45, -7)

    def test_settings_toggle_is_persisted_and_legacy_config_defaults_on(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "frameeye.conf.json")
            with patch.object(overlay, "CONFIG", path):
                Path(path).write_text(json.dumps({"raw": ["127.0.0.1:9021"], "target": "127.0.0.1:9000"}))
                args = sender.build_parser().parse_args([])
                app = overlay.OverlayApp(args)
                try:
                    self.assertTrue(app.runtime.osc_smoothing)
                    x0, y0, _, _ = overlay.ROW_SMOOTHING
                    app.handle_click(x0 + 5, y0 + 5)
                    self.assertFalse(app.runtime.osc_smoothing)
                    self.assertFalse(overlay.Settings(args).osc_smoothing)
                    app.handle_click(x0 + 5, y0 + 5)
                    self.assertTrue(overlay.Settings(args).osc_smoothing)
                finally:
                    app.runtime.sock.close()
        args = sender.build_parser().parse_args(["--target", "127.0.0.1:9000", "--no-osc-smoothing"])
        self.assertIn("--no-osc-smoothing", sender.runtime_args(args))

    def test_actual_udp_is_smoothed_but_sfey_stays_raw(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as raw, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as osc:
            for sock in (raw, osc):
                sock.bind(("127.0.0.1", 0))
                sock.settimeout(1)
            args = sender.build_parser().parse_args([
                "--raw", "127.0.0.1:%d" % raw.getsockname()[1],
                "--target", "127.0.0.1:%d" % osc.getsockname()[1]])
            runtime = sender.SenderRuntime(args)
            runtime.layout = V5
            self.addCleanup(runtime.sock.close)

            def send_and_receive(r, seq):
                valid, values, errors = runtime.send_record(memory(r), seq)
                self.assertTrue(valid)
                self.assertEqual(errors, 0)
                raw_packet = raw.recv(256)
                messages = [osc.recv(512) for _ in range(9)]
                floats = {p.split(b"\0", 1)[0].decode(): struct.unpack(">f", p[-4:])[0]
                          for p in messages if b",f\0" in p}
                return raw_packet, floats, values

            send_and_receive(record(0), 1)
            send_and_receive(record(1 / 90), 2)
            packet, messages, values = send_and_receive(record(2 / 90, 12), 3)
            self.assertEqual(len(packet), 96)
            self.assertEqual(packet[:4], b"SFEY")
            self.assertAlmostEqual(struct.unpack_from("<f", packet, 20)[0], math.tan(math.radians(12)), places=6)
            self.assertEqual(messages["/avatar/parameters/FT/v2/EyeLeftX"], 0)
            self.assertEqual(values[0], 0)
            # Disabling smoothing, or resuming after a pause, must not reuse the old filter state.
            runtime.osc_smoothing = False
            _, messages, _ = send_and_receive(record(3 / 90, 12), 4)
            self.assertAlmostEqual(messages["/avatar/parameters/FT/v2/EyeLeftX"], 12 / 45, places=6)
            runtime.enabled = False
            runtime.send_record(memory(record(4 / 90)), 5)
            runtime.enabled = runtime.osc_smoothing = True
            _, messages, _ = send_and_receive(record(5 / 90, -8), 6)
            self.assertAlmostEqual(messages["/avatar/parameters/FT/v2/EyeLeftX"], -8 / 45, places=6)
            # A corrupt timestamp must not poison the filter or produce OSC.
            valid, _, _ = runtime.send_record(memory(record(float("nan"))), 7)
            self.assertFalse(valid)
            self.assertEqual(struct.unpack_from("<H", raw.recv(256), 6)[0], 0)
            osc.settimeout(0.03)
            with self.assertRaises(socket.timeout):
                osc.recv(512)


if __name__ == "__main__":
    unittest.main()
