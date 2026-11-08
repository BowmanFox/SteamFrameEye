# SPDX-License-Identifier: MIT
"""Descriptor walker and the expected fixtures (spec §4.4, Appendix E; §11.3 test_ff_descriptors,
task T1). The Appendix E bytes are written out here line by line, independently of the code."""
import ffutil  # first: installs the port guard

import hashlib
import unittest

ffutil.setup_path()
from frameface import descriptors as D  # noqa: E402

HS_LINES = [
    "12 01 10 02 EF 02 01 40 B4 0B 21 03 00 01 01 02 00 01",                                   # device
    "09 02 D2 00 02 01 00 80 FA",                                                              # config
    "08 0B 00 02 0E 03 00 02",                                                                 # IAD
    "09 04 00 00 01 0E 01 00 02",                                                              # if0
    "0D 24 01 10 01 4F 00 80 D1 F0 08 01 01",                                                  # VC header
    "12 24 02 01 01 02 00 00 00 00 00 00 00 00 03 00 00 00",                                   # IT
    "09 24 03 02 01 01 00 04 00",                                                              # OT
    "0D 24 05 03 01 00 00 03 00 00 00 02 00",                                                  # PU
    "1A 24 06 04 DA 0B CB 2C 31 63 DB 4F 85 0E 79 05 4D BD 56 71 02 01 03 01 03 02",          # XU
    "07 05 86 03 40 00 09",                                                                    # EP 0x86
    "05 25 03 40 00",                                                                          # CS EP
    "09 04 01 00 01 0E 02 00 00",                                                              # if1
    "0E 24 01 01 4D 00 81 00 02 00 00 00 01 00",                                               # VS header
    "1B 24 04 01 01 59 55 59 32 00 00 10 00 80 00 00 AA 00 38 9B 71 10 01 00 00 00 00",       # format
    "1E 24 05 01 00 90 01 90 01 00 C0 27 09 00 C0 27 09 00 E2 04 00 0A 8B 02 00 01 0A 8B 02 00",  # frame
    "06 24 0D 01 01 04",                                                                       # color
    "07 05 81 02 00 02 00",                                                                    # EP 0x81
]
SS_LINES = (["12 01 00 03 EF 02 01 09 B4 0B 21 03 00 01 01 02 00 01", "09 02 DE 00 02 01 00 80 40"]
            + HS_LINES[2:10] + ["06 30 00 00 00 00"] + HS_LINES[10:16]
            + ["07 05 81 02 00 04 00", "06 30 0F 00 00 00"])
HS = b"".join(bytes.fromhex(line) for line in HS_LINES)
SS = b"".join(bytes.fromhex(line) for line in SS_LINES)
XU_GUID = bytes.fromhex("da 0b cb 2c 31 63 db 4f 85 0e 79 05 4d bd 56 71")
YUY2 = bytes.fromhex("59 55 59 32 00 00 10 00 80 00 00 aa 00 38 9b 71")


def offset_of(blob, line_index, lines=HS_LINES):
    return sum(len(bytes.fromhex(line)) for line in lines[:line_index])


def mutate(blob, line_index, at, value, lines=HS_LINES):
    """`blob` with byte `at` of descriptor line `line_index` replaced by value (int or bytes)."""
    out = bytearray(blob)
    off = offset_of(blob, line_index, lines) + at
    if isinstance(value, int):
        out[off] = value
    else:
        out[off:off + len(value)] = value
    return bytes(out)


XU_LINE, OT_LINE, VS_HDR_LINE, FORMAT_LINE, FRAME_LINE, EP81_LINE = 8, 6, 12, 13, 14, 16


class Fixtures(unittest.TestCase):
    def test_build_expected_is_appendix_e(self):
        self.assertEqual(D.build_expected("hs"), HS)
        self.assertEqual(D.build_expected(), HS)
        self.assertEqual(D.build_expected("ss"), SS)
        self.assertEqual((len(HS), len(SS)), (228, 240))
        self.assertEqual((len(HS) - 18, len(SS) - 18), (210, 222))
        self.assertEqual((int.from_bytes(HS[20:22], "little"), int.from_bytes(SS[20:22], "little")), (210, 222))
        with self.assertRaises(ValueError):
            D.build_expected("fs")

    def test_class_specific_totals(self):
        vc = sum(len(bytes.fromhex(line)) for line in HS_LINES[4:9])
        vs = sum(len(bytes.fromhex(line)) for line in HS_LINES[12:16])
        self.assertEqual((vc, vs), (79, 77))                     # VC/VS header wTotalLength 0x4F / 0x4D

    def test_walk_covers_every_byte(self):
        for blob, lines in ((HS, HS_LINES), (SS, SS_LINES)):
            descs, end = D.walk(blob)
            self.assertEqual(end, len(blob))
            self.assertEqual([bytes(d) for _, d in descs], [bytes.fromhex(line) for line in lines])
            self.assertEqual([off for off, _ in descs], [offset_of(blob, i, lines) for i in range(len(lines))])


class Parse(unittest.TestCase):
    HS_VALUES = dict(bcd_usb=0x0210, vid=0x0BB4, pid=0x0321, max_packet0=64, bcd_uvc=0x0110, clock_hz=150000000,
                     xu_unit=4, vs_interface=1, vs_endpoint=0x81, max_packet=512, format_index=1, format_guid=YUY2,
                     frame_index=1, width=400, height=400, frame_buffer_size=320000, intervals=[166666], warnings=[])

    def check(self, layout, **values):
        for name, value in values.items():
            self.assertEqual(getattr(layout, name), value, name)

    def test_hs_values(self):
        layout = D.parse(HS)
        self.check(layout, **self.HS_VALUES)
        self.assertEqual(layout.xu_units, [D.XuUnit(unit_id=4, guid=XU_GUID, num_controls=2, bm_controls=b"\x03")])
        self.assertEqual(D.diff_expected(layout), [])
        self.assertEqual(D.diff_expected(layout, "hs"), [])
        self.assertIsNone(D.unsupported(layout))

    def test_ss_values(self):
        layout = D.parse(SS)
        self.check(layout, **dict(self.HS_VALUES, bcd_usb=0x0300, max_packet0=9, max_packet=1024))
        self.assertEqual(D.diff_expected(layout, "ss"), [])
        self.assertEqual(D.diff_expected(layout), ["bcd_usb 0x0300 != 0x0210", "max_packet0 9 != 64",
                                                   "max_packet 1024 != 512"])

    def test_mutation_xu_unit_5(self):
        blob = mutate(mutate(HS, XU_LINE, 3, 5), OT_LINE, 7, 5)        # XU id 5, output terminal sourced from 5
        layout = D.parse(blob)
        self.check(layout, xu_unit=5, warnings=[])
        self.assertEqual(layout.xu_units[0].unit_id, 5)
        self.assertEqual(D.diff_expected(layout), ["xu_unit 5 != 4"])

    def test_mutation_endpoint_0x82(self):
        blob = mutate(mutate(HS, EP81_LINE, 2, 0x82), VS_HDR_LINE, 6, 0x82)
        layout = D.parse(blob)
        self.check(layout, vs_endpoint=0x82, warnings=[])
        self.assertEqual(D.diff_expected(layout), ["vs_endpoint 0x82 != 0x81"])
        only_ep = D.parse(mutate(HS, EP81_LINE, 2, 0x82))                # header still says 0x81
        self.assertEqual(only_ep.vs_endpoint, 0x82)
        self.assertEqual(only_ep.warnings, ["VS input header endpoint 0x81 != bulk endpoint 0x82"])

    def test_mutation_1024_byte_packets(self):
        layout = D.parse(mutate(HS, EP81_LINE, 4, b"\x00\x04"))
        self.check(layout, max_packet=1024, warnings=[])
        self.assertEqual(D.diff_expected(layout), ["max_packet 1024 != 512"])

    def test_high_bandwidth_packet_size_formula(self):
        # (w & 0x7FF) * (((w >> 11) & 3) + 1): 0x0C00 = 2 x 1024, 0x1400 = 3 x 1024
        self.assertEqual(D.parse(mutate(HS, EP81_LINE, 4, b"\x00\x0c")).max_packet, 2048)
        self.assertEqual(D.parse(mutate(HS, EP81_LINE, 4, b"\x00\x14")).max_packet, 3072)

    def test_mutation_640x480(self):
        blob = mutate(HS, FRAME_LINE, 5, b"\x80\x02\xe0\x01")           # wWidth 640, wHeight 480
        blob = mutate(blob, FRAME_LINE, 17, (614400).to_bytes(4, "little"))
        layout = D.parse(blob)
        self.check(layout, width=640, height=480, frame_buffer_size=614400, frame_index=1, warnings=[])
        self.assertEqual(D.diff_expected(layout), ["width 640 != 400", "height 480 != 400",
                                                   "frame_buffer_size 614400 != 320000"])
        self.assertEqual(D.unsupported(layout), "640x480 format not supported (no Baballonia-exact pipeline)")

    def test_mutation_missing_xu_guid(self):
        layout = D.parse(mutate(HS, XU_LINE, 4, bytes(16)))
        self.assertEqual(layout.xu_unit, 4)
        self.assertEqual(layout.warnings, ["no extension unit with the tracker GUID %s; using unit 4" % XU_GUID.hex()])
        self.assertEqual(D.diff_expected(layout), ["xu_guid %s != %s" % ("00" * 16, XU_GUID.hex())])

    def test_mutation_no_yuy2_format(self):
        layout = D.parse(mutate(HS, FORMAT_LINE, 5, b"NV12"))
        self.assertEqual(layout.format_guid[:4], b"NV12")
        self.assertEqual(layout.warnings, ["no YUY2 format; using format 1 guid %s" % layout.format_guid.hex()])
        self.assertEqual(D.diff_expected(layout), ["format_guid %s != %s" % (layout.format_guid.hex(), YUY2.hex())])

    def test_truncated(self):
        for cut in range(len(HS)):
            with self.subTest(cut=cut), self.assertRaises(D.DescriptorError) as cm:
                D.parse(HS[:cut])
            self.assertIn("bulk IN endpoint", str(cm.exception))
        no_frame = HS[:offset_of(HS, FRAME_LINE)] + HS[offset_of(HS, FRAME_LINE + 1):]
        with self.assertRaises(D.DescriptorError) as cm:
            D.parse(no_frame)
        self.assertEqual(str(cm.exception), "no frame descriptor")
        self.assertTrue(issubclass(D.DescriptorError, ValueError))

    def test_walker_stops_on_bad_blength(self):
        for bad in (b"\x00\x24", b"\x01\x24"):
            blob = HS[:offset_of(HS, VS_HDR_LINE)] + bad + HS[offset_of(HS, VS_HDR_LINE):]
            descs, end = D.walk(blob)
            self.assertEqual(end, offset_of(HS, VS_HDR_LINE))
            self.assertEqual(len(descs), VS_HDR_LINE)
            with self.assertRaises(D.DescriptorError):
                D.parse(blob)                                             # nothing after the bad length is used
        too_long = mutate(HS, EP81_LINE, 0, 9)                            # bLength 9 > the 7 bytes left
        self.assertEqual(D.walk(too_long)[1], offset_of(HS, EP81_LINE))
        with self.assertRaises(D.DescriptorError):
            D.parse(too_long)
        trailing = D.parse(HS + b"\x00\x00")                               # garbage after everything needed
        self.check(trailing, xu_unit=4, vs_endpoint=0x81)
        self.assertEqual(trailing.warnings, ["descriptor walk stopped at offset 228 (bLength 0, 2 bytes left)"])
        self.assertEqual(D.walk(HS + b"\x05")[1], 228)                      # one byte cannot hold a header

    def test_unit_4_preferred_and_other_units(self):
        xu = bytes.fromhex(HS_LINES[XU_LINE])
        xu3 = xu[:3] + b"\x03" + xu[4:]
        xu6 = xu[:3] + b"\x06" + xu[4:]
        at = offset_of(HS, XU_LINE)
        both = HS[:at] + xu3 + HS[at:]                                    # units 3 and 4 carry the GUID
        self.assertEqual(D.parse(both).xu_unit, 4)
        self.assertEqual([u.unit_id for u in D.parse(both).xu_units], [3, 4])
        other = HS[:at] + xu6 + HS[at + len(xu):]                         # only unit 6
        self.assertEqual((D.parse(other).xu_unit, D.parse(other).warnings), (6, []))
        self.assertEqual(D.diff_expected(D.parse(other)), ["xu_unit 6 != 4"])

    def test_focus3_and_sha1(self):
        focus = mutate(HS, 0, 10, b"\xa1\x06")
        self.assertEqual(D.parse(focus).pid, 0x06A1)
        self.assertEqual(D.unsupported(D.parse(focus)), "VIVE Focus facial tracker is not supported")
        self.assertEqual(D.sha1_hex(HS), hashlib.sha1(HS).hexdigest())
        self.assertEqual(len(D.sha1_hex(HS)), 40)
        self.assertNotEqual(D.sha1_hex(HS), D.sha1_hex(SS))
        self.assertEqual(D.sha1_hex(bytearray(HS)), D.sha1_hex(HS))


if __name__ == "__main__":
    unittest.main()
