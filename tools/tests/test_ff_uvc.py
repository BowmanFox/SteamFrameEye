# SPDX-License-Identifier: MIT
"""UVC requests, XU commands, probe/commit and payload headers (spec §4.5, §4.8, §4.10, Appendix A;
§11.3 test_ff_uvc, task T1). Every byte vector is written out here independently of the code."""
import ffutil  # first: installs the port guard

import dataclasses
import struct
import unittest

ffutil.setup_path()
from frameface import uvc  # noqa: E402
from frameface.uvc import Ack  # noqa: E402


def hx(text):
    return bytes.fromhex(text)


class Setups(unittest.TestCase):
    """The §4.5 table (A.1): fields and the 8-byte SETUP packet of every request."""

    def check(self, setup, fields, packet):
        self.assertEqual((setup.request_type, setup.request, setup.value, setup.index, setup.length), fields)
        self.assertEqual(setup.pack(), hx(packet))
        self.assertEqual(len(setup.pack()), 8)

    def test_xu_requests(self):
        self.check(uvc.xu_setup(uvc.GET_LEN, 2), (0xA1, 0x85, 0x0200, 0x0400, 2), "A1 85 00 02 00 04 02 00")
        self.check(uvc.xu_setup(uvc.GET_INFO, 1), (0xA1, 0x86, 0x0200, 0x0400, 1), "A1 86 00 02 00 04 01 00")
        self.check(uvc.xu_setup(uvc.SET_CUR, 64), (0x21, 0x01, 0x0200, 0x0400, 64), "21 01 00 02 00 04 40 00")
        self.check(uvc.xu_setup(uvc.SET_CUR, 384), (0x21, 0x01, 0x0200, 0x0400, 384), "21 01 00 02 00 04 80 01")
        self.check(uvc.xu_setup(uvc.GET_CUR, 64), (0xA1, 0x81, 0x0200, 0x0400, 64), "A1 81 00 02 00 04 40 00")
        self.check(uvc.xu_setup(uvc.GET_CUR, 384), (0xA1, 0x81, 0x0200, 0x0400, 384), "A1 81 00 02 00 04 80 01")

    def test_xu_on_another_unit_and_selector(self):
        # wIndex = unit << 8 | interface 0, wValue = selector << 8 (the discovered unit is used)
        self.check(uvc.xu_setup(uvc.GET_LEN, 2, unit=5, selector=1), (0xA1, 0x85, 0x0100, 0x0500, 2),
                   "A1 85 00 01 00 05 02 00")

    def test_probe_and_commit(self):
        for request, code in ((uvc.GET_CUR, "81"), (uvc.GET_MIN, "82"), (uvc.GET_MAX, "83"), (uvc.GET_DEF, "87")):
            with self.subTest(request=hex(request)):
                self.check(uvc.vs_setup(request, uvc.VS_PROBE, 34), (0xA1, request, 0x0100, 0x0001, 34),
                           "A1 %s 00 01 01 00 22 00" % code)
        self.check(uvc.vs_setup(uvc.SET_CUR, uvc.VS_PROBE, 34), (0x21, 0x01, 0x0100, 0x0001, 34),
                   "21 01 00 01 01 00 22 00")
        self.check(uvc.vs_setup(uvc.SET_CUR, uvc.VS_COMMIT, 34), (0x21, 0x01, 0x0200, 0x0001, 34),
                   "21 01 00 02 01 00 22 00")
        self.check(uvc.vs_setup(uvc.SET_CUR, uvc.VS_COMMIT, 48), (0x21, 0x01, 0x0200, 0x0001, 48),
                   "21 01 00 02 01 00 30 00")

    def test_error_code_and_set_interface(self):
        self.check(uvc.vc_error_code_setup(), (0xA1, 0x81, 0x0200, 0x0000, 1), "A1 81 00 02 00 00 01 00")
        self.check(uvc.set_interface_setup(), (0x01, 0x0B, 0, 1, 0), "01 0B 00 00 01 00 00 00")
        self.check(uvc.set_interface_setup(1, 0), (0x01, 0x0B, 0, 1, 0), "01 0B 00 00 01 00 00 00")

    def test_direction_and_constants(self):
        self.assertTrue(uvc.xu_setup(uvc.GET_CUR, 64).is_in)
        self.assertFalse(uvc.xu_setup(uvc.SET_CUR, 64).is_in)
        self.assertFalse(uvc.set_interface_setup().is_in)
        self.assertEqual((uvc.XU_UNIT, uvc.XU_SELECTOR, uvc.CTRL_TIMEOUT_MS, uvc.VS_PROBE, uvc.VS_COMMIT),
                         (4, 2, 1000, 1, 2))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            uvc.xu_setup(uvc.GET_LEN, 2).length = 3


class Commands(unittest.TestCase):
    """Appendix A.2 and the command catalog of §4.5."""

    def test_magic_without_trailer_at_64(self):
        self.assertEqual(uvc.magic(64), hx("51 52") + bytes(62))
        self.assertEqual(len(uvc.magic(64)), 64)

    def test_magic_trailer_at_384(self):
        m = uvc.magic(384)
        self.assertEqual(len(m), 384)
        expected = bytearray(384)
        expected[0:2] = hx("51 52")
        expected[254], expected[255] = 0x53, 0x54
        self.assertEqual(m, bytes(expected))
        self.assertEqual((m[254], m[255]), (0x53, 0x54))
        self.assertEqual(m.count(0), 384 - 4)

    def test_trailer_only_from_256(self):
        self.assertEqual(uvc.magic(255), hx("51 52") + bytes(253))
        self.assertEqual(uvc.magic(256)[254:], hx("53 54"))
        self.assertEqual(uvc.magic(128), hx("51 52") + bytes(126))     # an odd GET_LEN used as read
        self.assertNotIn(0x53, uvc.stream_cmd(True, 384))                # STREAM and REG commands never carry it
        self.assertNotIn(0x53, uvc.reg_write(0x02, 0xFF, 384))

    def test_stream_on_off(self):
        self.assertEqual(uvc.stream_cmd(False, 64), hx("50 14 00 00") + bytes(60))
        self.assertEqual(uvc.stream_cmd(True, 64), hx("50 14 00 01") + bytes(60))
        self.assertEqual(uvc.stream_cmd(True, 384), hx("50 14 00 01") + bytes(380))

    def test_reg_write_and_read(self):
        self.assertEqual(uvc.reg_write(0x02, 0xFF, 64),
                         hx("50 ab 60 01 01 00 00 00 02 90 01 00 01 00 00 00 ff") + bytes(47))
        self.assertEqual(uvc.reg_read(0x05, 64),
                         hx("51 ab 60 01 01 00 00 00 05 90 01 00 01 00 00 00 00") + bytes(47))
        self.assertEqual(uvc.reg_write(0x0F, 0x03, 384),
                         hx("50 ab 60 01 01 00 00 00 0f 90 01 00 01 00 00 00 03") + bytes(367))

    def test_ir_regs_order(self):
        on = uvc.ir_regs(0xFF, 0xB2)
        self.assertEqual(on, ((0x00, 0x40), (0x08, 0x01), (0x70, 0x00), (0x02, 0xFF), (0x03, 0xFF), (0x04, 0xFF),
                              (0x0E, 0x00), (0x05, 0xB2), (0x06, 0xB2), (0x07, 0xB2), (0x0F, 0x03)))
        self.assertEqual(uvc.ir_regs(), on)
        off = uvc.ir_regs(0x00, 0x96)
        self.assertEqual(off, ((0x00, 0x40), (0x08, 0x01), (0x70, 0x00), (0x02, 0x00), (0x03, 0x00), (0x04, 0x00),
                               (0x0E, 0x00), (0x05, 0x96), (0x06, 0x96), (0x07, 0x96), (0x0F, 0x03)))
        self.assertEqual(len(on), 11)

    def test_commands_must_fit(self):
        with self.assertRaises(ValueError):
            uvc.reg_write(0x02, 0xFF, 16)          # 17 bytes do not fit a GET_LEN of 16
        with self.assertRaises(ValueError):
            uvc.magic(1)
        for bad in ((0x100, 1), (-1, 1), (1, 0x100), (1, -1)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                uvc.reg_write(bad[0], bad[1], 64)
        self.assertEqual(uvc.pad(b"\x51\x52", 4), hx("51 52 00 00"))

    def test_command_length(self):
        self.assertEqual(uvc.command_length(uvc.magic(384)), 2)
        self.assertEqual(uvc.command_length(uvc.stream_cmd(False, 64)), 4)
        self.assertEqual(uvc.command_length(uvc.reg_write(0x02, 0x00, 64)), 17)
        self.assertEqual(uvc.command_length(uvc.reg_read(0x02, 64)), 17)
        self.assertEqual(uvc.command_length(uvc.MAGIC), 2)


class ParseAck(unittest.TestCase):
    def test_busy(self):
        cmd = uvc.magic(64)
        self.assertEqual(uvc.parse_ack(cmd, b"\x55" + bytes(63)), (Ack.BUSY, 0, False))
        self.assertEqual(uvc.parse_ack(cmd, b"\x55")[0:2], (Ack.BUSY, None))

    def test_done_with_full_echo(self):
        for cmd in (uvc.magic(64), uvc.stream_cmd(True, 64), uvc.reg_write(0x02, 0xFF, 64), uvc.magic(384)):
            reply = uvc.done_reply(cmd, len(cmd))
            self.assertEqual(reply[:17], b"\x56" + cmd[:16])
            self.assertEqual(uvc.parse_ack(cmd, reply), (Ack.DONE, 0, True))

    def test_done_needs_only_the_command_bytes(self):
        # MAGIC: k = 2; the rest of the echo is recorded as echo16_ok = False, not an error
        cmd = uvc.magic(64)
        reply = hx("56 51 52") + b"\xee" * 14 + b"\x00" + bytes(46)
        self.assertEqual(uvc.parse_ack(cmd, reply), (Ack.DONE, 0, False))
        # STREAM: k = 4
        cmd = uvc.stream_cmd(True, 64)
        self.assertEqual(uvc.parse_ack(cmd, hx("56 50 14 00 01") + b"\xee" * 59)[0::2], (Ack.DONE, False))
        self.assertEqual(uvc.parse_ack(cmd, hx("56 50 14 00 00") + bytes(59))[0], Ack.ERROR)
        # an unpadded command gives the same answers as the padded one
        self.assertEqual(uvc.parse_ack(uvc.MAGIC, reply[:64]), uvc.parse_ack(uvc.magic(64), reply[:64]))

    def test_echo_mismatch_is_an_error(self):
        cmd = uvc.reg_write(0x02, 0xFF, 64)
        reply = bytearray(uvc.done_reply(cmd, 64))
        reply[9] ^= 0x01                           # echo of the register address differs
        self.assertEqual(uvc.parse_ack(cmd, bytes(reply))[0::2], (Ack.ERROR, False))
        stale = uvc.done_reply(uvc.reg_write(0x03, 0xFF, 64), 64)   # the previous command's answer
        self.assertEqual(uvc.parse_ack(cmd, stale)[0], Ack.ERROR)

    def test_error_codes(self):
        cmd = uvc.magic(64)
        for code in (0x00, 0x01, 0x54, 0x57, 0xFF):
            with self.subTest(code=code):
                reply = bytes([code]) + uvc.done_reply(cmd, 64)[1:]
                self.assertEqual(uvc.parse_ack(cmd, reply), (Ack.ERROR, 0, True))

    def test_short_replies(self):
        cmd = uvc.reg_write(0x02, 0xFF, 64)
        self.assertEqual(uvc.parse_ack(cmd, b""), (Ack.ERROR, None, False))
        self.assertEqual(uvc.parse_ack(uvc.magic(64), hx("56 51")), (Ack.ERROR, None, False))   # echo cut short
        self.assertEqual(uvc.parse_ack(uvc.magic(64), hx("56 51 52")), (Ack.DONE, None, False))
        self.assertEqual(uvc.parse_ack(cmd, uvc.done_reply(cmd, 16))[0:2], (Ack.ERROR, None))     # needs 1 + 16
        self.assertEqual(uvc.parse_ack(cmd, uvc.done_reply(cmd, 17)), (Ack.DONE, None, True))     # no value byte
        self.assertEqual(uvc.parse_ack(cmd, uvc.done_reply(cmd, 18)), (Ack.DONE, 0, True))

    def test_value_byte(self):
        cmd = uvc.reg_read(0x05, 64)
        reply = uvc.done_reply(cmd, 64, value=0xB2)
        self.assertEqual(reply[17], 0xB2)
        self.assertEqual(uvc.parse_ack(cmd, reply), (Ack.DONE, 0xB2, True))
        self.assertEqual(uvc.parse_ack(cmd, reply[:18]), (Ack.DONE, 0xB2, True))
        self.assertEqual(uvc.parse_ack(cmd, uvc.done_reply(cmd, 384, value=0xFF))[1], 0xFF)

    def test_ack_values(self):
        self.assertEqual([a.value for a in Ack], ["busy", "done", "error"])
        self.assertEqual(Ack.DONE, "done")
        self.assertEqual((str(Ack.DONE), "%s" % Ack.BUSY, "{}".format(Ack.ERROR)), ("done", "busy", "error"))


class Probes(unittest.TestCase):
    def test_probe_length(self):
        self.assertEqual([uvc.probe_length(v) for v in (0x0100, 0x010F, 0x0110, 0x014F, 0x0150, 0x0200)],
                         [26, 26, 34, 34, 48, 48])

    def test_formats(self):
        self.assertEqual({n: struct.calcsize(f) for n, f in uvc.PROBE_FORMATS.items()}, {26: 26, 34: 34, 48: 48})
        self.assertEqual(uvc.PROBE_FORMATS, {26: "<HBBIHHHHHII", 34: "<HBBIHHHHHIIIBBBB",
                                             48: "<HBBIHHHHHIIIBBBBBBBBHQ"})

    def test_expected_device_answer(self):
        # §4.8: dwMaxVideoFrameSize @18 = 00 e2 04 00, dwMaxPayloadTransferSize @22 = 00 80 00 00,
        # dwClockFrequency @26 = 80 d1 f0 08, bmFramingInfo @30
        answer = bytearray(34)
        answer[0:8] = hx("01 00 01 01 0a 8b 02 00")
        answer[18:22] = hx("00 e2 04 00")
        answer[22:26] = hx("00 80 00 00")
        answer[26:30] = hx("80 d1 f0 08")
        answer[30] = 0x03
        p = uvc.unpack_probe(bytes(answer))
        self.assertEqual((p.hint, p.format_index, p.frame_index, p.frame_interval), (1, 1, 1, 166666))
        self.assertEqual((p.max_video_frame_size, p.max_payload, p.clock_hz, p.framing_info), (320000, 32768, 150000000, 3))
        self.assertEqual(p.raw, bytes(answer))
        self.assertEqual(uvc.pack_probe(p, 34), bytes(answer))

    def test_round_trips_of_bytes(self):
        for n in (26, 34, 48):
            raw = bytes((i * 37 + 11) & 0xFF for i in range(n))
            with self.subTest(n=n):
                p = uvc.unpack_probe(raw)
                self.assertEqual(uvc.pack_probe(p, n), raw)
                self.assertEqual(uvc.unpack_probe(uvc.pack_probe(p, n)), p)

    def test_round_trips_of_fields(self):
        p = uvc.Probe(hint=1, format_index=1, frame_index=1, frame_interval=166666, key_frame_rate=2,
                      p_frame_rate=3, comp_quality=4, comp_window_size=5, delay=6, max_video_frame_size=320000,
                      max_payload=32768, clock_hz=150000000, framing_info=3, preferred_version=1, min_version=2,
                      max_version=3)
        for n in (26, 34, 48):
            with self.subTest(n=n):
                q = uvc.unpack_probe(uvc.pack_probe(p, n))
                expected = p if n >= 34 else dataclasses.replace(p, clock_hz=0, framing_info=0, preferred_version=0,
                                                                 min_version=0, max_version=0)
                self.assertEqual(dataclasses.replace(q, raw=b""), expected)
        self.assertEqual(uvc.pack_probe(p, 48)[34:], bytes(14))            # no raw: the UVC 1.5 fields are zero
        long_answer = uvc.pack_probe(p, 34) + bytes(range(1, 15))
        self.assertEqual(uvc.pack_probe(uvc.unpack_probe(long_answer), 48), long_answer)
        with self.assertRaises(ValueError):
            uvc.pack_probe(p, 30)

    def test_short_answer_is_zero_padded(self):
        p = uvc.unpack_probe(hx("01 00 01 01"))
        self.assertEqual((p.hint, p.format_index, p.frame_index, p.frame_interval, p.max_payload), (1, 1, 1, 0, 0))
        self.assertEqual(p.raw, hx("01 00 01 01"))

    def test_request_probe(self):
        head = hx("01 00 01 01 0a 8b 02 00")
        self.assertEqual(uvc.request_probe(None, 34, 1, 1, 166666), head + bytes(26))    # A.3, no base
        base = bytes(range(100, 134))
        self.assertEqual(uvc.request_probe(base, 34, 1, 1, 166666), head + base[8:])     # A.3, from the base
        self.assertEqual(uvc.request_probe(base[:20], 34, 1, 1, 166666), head + base[8:20] + bytes(14))
        self.assertEqual(uvc.request_probe(bytes(range(48)), 26, 1, 1, 166666), head + bytes(range(8, 26)))
        self.assertEqual(len(uvc.request_probe(None, 48, 1, 1, 166666)), 48)
        self.assertEqual(uvc.request_probe(None, 26, 2, 3, 333333)[:8], hx("01 00 02 03 15 16 05 00"))

    def test_fix_probe(self):
        base = uvc.unpack_probe(uvc.request_probe(None, 34, 1, 1, 166666))

        def fix(**kw):
            return uvc.fix_probe(dataclasses.replace(base, **kw), 320000)

        p, w = fix(max_video_frame_size=320000, max_payload=32768)
        self.assertEqual(((p.max_video_frame_size, p.max_payload), w), ((320000, 32768), []))
        p, w = fix(max_video_frame_size=320000, max_payload=0)
        self.assertEqual((p.max_payload, len(w)), (32768, 1))
        self.assertIn("32768", w[0])
        p, w = fix(max_video_frame_size=320000, max_payload=0xFFFF8000)
        self.assertEqual((p.max_payload, len(w)), (32768, 1))
        self.assertIn("0xffff8000", w[0])
        p, w = fix(max_video_frame_size=320000, max_payload=0xFFFF0000)    # masked to 0, then the default
        self.assertEqual((p.max_payload, len(w)), (32768, 2))
        p, w = fix(max_video_frame_size=320000, max_payload=1048577)
        self.assertEqual((p.max_payload, len(w)), (32768, 1))
        p, w = fix(max_video_frame_size=320000, max_payload=1048576)       # exactly 1 MiB is kept
        self.assertEqual((p.max_payload, w), (1048576, []))
        p, w = fix(max_video_frame_size=614400, max_payload=16384)        # frame size from the descriptor
        self.assertEqual((p.max_video_frame_size, p.max_payload, len(w)), (320000, 16384, 1))
        self.assertIn("614400", w[0])
        self.assertEqual(base.max_payload, 0, "fix_probe returns a new Probe")
        p, w = uvc.fix_probe(dataclasses.replace(base, max_video_frame_size=5, max_payload=32768), 0)
        self.assertEqual((p.max_video_frame_size, w), (5, []))             # no descriptor size: keep


class Headers(unittest.TestCase):
    @staticmethod
    def strict_ok(hl, fl, n):
        return hl == 12 and n >= 12 and fl & 0x0C == 0x0C and fl & 0x30 == 0

    @staticmethod
    def lenient_ok(hl, fl, n):
        return 2 <= hl <= n and hl >= 2 + (4 if fl & 0x04 else 0) + (6 if fl & 0x08 else 0)

    def test_every_flag_combination(self):
        tail = hx("11 22 33 44 55 66 77 88 99 aa") + bytes(20)
        for hl in range(0, 14):
            for fl in range(256):
                buf = bytes([hl, fl]) + tail
                for n in (2, 6, 8, 11, 12, len(buf)):
                    for strict in (True, False):
                        h = uvc.parse_header(buf, 0, n, strict)
                        ok = self.strict_ok(hl, fl, n) if strict else self.lenient_ok(hl, fl, n)
                        if not ok:
                            if h is not None:
                                self.fail("accepted hl=%d fl=%#x n=%d strict=%s" % (hl, fl, n, strict))
                            continue
                        if h is None:
                            self.fail("rejected hl=%d fl=%#x n=%d strict=%s" % (hl, fl, n, strict))
                        pts = 0x44332211 if fl & 0x04 else None
                        scr = (0x88776655 if fl & 0x04 else 0x44332211) if fl & 0x08 else None
                        self.assertEqual(h, uvc.Header(hl, fl, fl & 1, bool(fl & 2), bool(fl & 0x40), pts, scr))

    def test_strict_accepts_err_and_eoh(self):
        buf = hx("0c cd 01 00 00 00 02 00 00 00 03 00")            # EOH | ERR | SCR | PTS | FID
        h = uvc.parse_header(buf, 0, 12, True)
        self.assertEqual((h.err, h.fid, h.eof, h.pts, h.scr), (True, 1, False, 1, 2))
        self.assertIsNone(uvc.parse_header(hx("0c 1c") + bytes(10), 0, 12, True))     # RES set
        self.assertIsNone(uvc.parse_header(hx("0c 2c") + bytes(10), 0, 12, True))     # STI set
        self.assertIsNone(uvc.parse_header(hx("0c 0c") + bytes(10), 0, 11, True))     # payload shorter than 12

    def test_appendix_a4_examples(self):
        first = hx("0c 0d") + struct.pack("<IIH", 0x01020304, 0x0A0B0C0D, 0x0123)
        last = hx("0c 0f") + struct.pack("<IIH", 0x01020304, 0x0A0B0C0E, 0x0124)
        h = uvc.parse_header(first, 0, len(first), True)
        self.assertEqual((h.length, h.flags, h.fid, h.eof, h.err, h.pts, h.scr), (12, 0x0D, 1, False, False,
                                                                                  0x01020304, 0x0A0B0C0D))
        h = uvc.parse_header(last, 0, len(last), True)
        self.assertEqual((h.fid, h.eof, h.pts), (1, True, 0x01020304))

    def test_inside_a_buffer_and_memoryview(self):
        payload = hx("0c 0e") + struct.pack("<IIH", 7, 8, 9) + b"data"
        urb = bytes(32768) + payload
        for buf in (urb, bytearray(urb), memoryview(urb), memoryview(bytearray(urb))[0:]):
            h = uvc.parse_header(buf, 32768, len(payload), True)
            self.assertEqual((h.fid, h.eof, h.pts, h.scr), (0, True, 7, 8))
        self.assertIsNone(uvc.parse_header(urb, len(urb) - 1, 1, False))
        self.assertIsNone(uvc.parse_header(urb, len(urb), 2, False))
        self.assertIsNone(uvc.parse_header(b"", 0, 0, False))


if __name__ == "__main__":
    unittest.main()
