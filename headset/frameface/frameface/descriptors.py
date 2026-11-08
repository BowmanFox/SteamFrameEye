# SPDX-License-Identifier: MIT
"""Descriptor walker for the tracker's sysfs `descriptors` file (the 18-byte device descriptor
followed by the active configuration) and the expected fixtures of spec §4.4 and Appendix E.

parse() discovers the values the driver needs (XU unit, VideoStreaming bulk endpoint, packet
size, YUY2 format and frame); structural problems go to VftLayout.warnings, and
diff_expected() lists every difference from the expected persona as "<field> <found> != <expected>".
The service logs both as WARN and uses the discovered values; unsupported() names the layouts it
cannot serve (FAULT(unsupported)). Pure: no I/O.
"""
from dataclasses import dataclass, field
import hashlib
import struct

__all__ = ["VID", "PID", "FOCUS3_PID", "XU_GUID", "YUY2_GUID", "XuUnit", "VftLayout", "DescriptorError",
           "walk", "parse", "build_expected", "diff_expected", "unsupported", "sha1_hex", "EXPECTED"]

VID, PID, FOCUS3_PID = 0x0BB4, 0x0321, 0x06A1
XU_GUID = bytes.fromhex("da0bcb2c3163db4f850e79054dbd5671")
YUY2_GUID = bytes.fromhex("5955593200001000800000aa00389b71")

DT_DEVICE, DT_INTERFACE, DT_ENDPOINT, CS_INTERFACE = 0x01, 0x04, 0x05, 0x24
CC_VIDEO, SC_VIDEOCONTROL, SC_VIDEOSTREAMING = 0x0E, 0x01, 0x02
VC_HEADER, VC_EXTENSION_UNIT = 0x01, 0x06
VS_INPUT_HEADER, VS_FORMAT_UNCOMPRESSED, VS_FRAME_UNCOMPRESSED = 0x01, 0x04, 0x05


@dataclass
class XuUnit:
    unit_id: int
    guid: bytes
    num_controls: int
    bm_controls: bytes


@dataclass
class VftLayout:
    bcd_usb: int
    vid: int
    pid: int
    max_packet0: int
    bcd_uvc: int
    clock_hz: int
    xu_units: list
    xu_unit: int
    vs_interface: int
    vs_endpoint: int
    max_packet: int
    format_index: int
    format_guid: bytes
    frame_index: int
    width: int
    height: int
    frame_buffer_size: int
    intervals: list
    warnings: list = field(default_factory=list)


class DescriptorError(ValueError):
    """The descriptors lack what streaming needs: a VideoStreaming bulk IN endpoint or a frame."""


def walk(raw: bytes) -> tuple:
    """([(offset, descriptor bytes)], end): the walk stops where bLength < 2 or bLength exceeds the
    remaining bytes; end == len(raw) when every byte was consumed."""
    raw = bytes(raw)
    out, i = [], 0
    while i + 2 <= len(raw):
        bl = raw[i]
        if bl < 2 or bl > len(raw) - i:
            break
        out.append((i, raw[i:i + bl]))
        i += bl
    return out, i


def _u16(d, i):
    return struct.unpack_from("<H", d, i)[0]


def _u32(d, i):
    return struct.unpack_from("<I", d, i)[0]


def parse(raw: bytes) -> VftLayout:
    """Walk the descriptors and pick the tracker's units (spec §4.4). DescriptorError if there is
    no VideoStreaming bulk IN endpoint or no frame descriptor."""
    raw = bytes(raw)
    descs, end = walk(raw)
    warnings = []
    if end != len(raw):
        bl = raw[end] if end < len(raw) else 0
        warnings.append("descriptor walk stopped at offset %d (bLength %d, %d bytes left)" % (end, bl, len(raw) - end))
    dev = {"bcd_usb": 0, "vid": 0, "pid": 0, "max_packet0": 0}
    bcd_uvc = clock_hz = 0
    xus, endpoints, formats = [], [], []
    iface = None                                 # (number, class, subclass) of the current interface
    header_ep = None
    for off, d in descs:
        bl, dt = d[0], d[1]
        if dt == DT_DEVICE and bl >= 12:
            dev = {"bcd_usb": _u16(d, 2), "max_packet0": d[7], "vid": _u16(d, 8), "pid": _u16(d, 10)}
        elif dt == DT_INTERFACE and bl >= 9:
            iface = (d[2], d[5], d[6])
        elif dt == DT_ENDPOINT and bl >= 7:
            if iface is not None and iface[1:] == (CC_VIDEO, SC_VIDEOSTREAMING):
                endpoints.append((iface[0], d[2], d[3], _u16(d, 4)))
        elif dt == CS_INTERFACE and bl >= 3 and iface is not None and iface[1] == CC_VIDEO:
            sub = d[2]
            if iface[2] == SC_VIDEOCONTROL:
                if sub == VC_HEADER and bl >= 11:
                    bcd_uvc, clock_hz = _u16(d, 3), _u32(d, 7)
                elif sub == VC_EXTENSION_UNIT and bl >= 23:
                    p = d[21]
                    n = d[22 + p] if 22 + p < bl else 0
                    if 23 + p + n <= bl:
                        xus.append(XuUnit(d[3], bytes(d[4:20]), d[20], bytes(d[23 + p:23 + p + n])))
                    else:
                        warnings.append("extension unit at offset %d is truncated" % off)
            elif iface[2] == SC_VIDEOSTREAMING:
                if sub == VS_INPUT_HEADER and bl >= 7:
                    header_ep = d[6]
                elif sub == VS_FORMAT_UNCOMPRESSED and bl >= 22:
                    formats.append({"index": d[3], "guid": bytes(d[5:21]), "frames": []})
                elif sub == VS_FRAME_UNCOMPRESSED and bl >= 26:
                    if not formats:
                        warnings.append("frame descriptor at offset %d has no format" % off)
                        continue
                    kind = d[25]
                    if kind:
                        intervals = [_u32(d, 26 + 4 * k) for k in range(kind) if 30 + 4 * k <= bl]
                    else:
                        intervals = []
                        warnings.append("frame %d has continuous intervals" % d[3])
                    formats[-1]["frames"].append({"index": d[3], "w": _u16(d, 5), "h": _u16(d, 7),
                                                  "fbs": _u32(d, 17), "intervals": intervals})
    bulk_in = [e for e in endpoints if e[1] & 0x80 and e[2] & 0x03 == 0x02]
    if not bulk_in:
        raise DescriptorError("no VideoStreaming bulk IN endpoint")
    vs_if, ep, _, w_max = bulk_in[0]
    if len(bulk_in) > 1:
        warnings.append("%d VideoStreaming bulk IN endpoints; using 0x%02x" % (len(bulk_in), ep))
    if header_ep is not None and header_ep != ep:
        warnings.append("VS input header endpoint 0x%02x != bulk endpoint 0x%02x" % (header_ep, ep))
    yuy2 = [f for f in formats if f["guid"] == YUY2_GUID and f["frames"]]
    with_frames = [f for f in formats if f["frames"]]
    if not with_frames:
        raise DescriptorError("no frame descriptor")
    fmt = (yuy2 or with_frames)[0]
    if not yuy2:
        warnings.append("no YUY2 format; using format %d guid %s" % (fmt["index"], fmt["guid"].hex()))
    frames = fmt["frames"]
    frame = next((f for f in frames if (f["w"], f["h"]) == (400, 400)), frames[0])
    tracker = [u for u in xus if u.guid == XU_GUID]
    if tracker:
        xu_unit = 4 if any(u.unit_id == 4 for u in tracker) else tracker[0].unit_id
    else:
        xu_unit = 4 if any(u.unit_id == 4 for u in xus) or not xus else xus[0].unit_id
        warnings.append("no extension unit with the tracker GUID %s; using unit %d" % (XU_GUID.hex(), xu_unit))
    return VftLayout(bcd_usb=dev["bcd_usb"], vid=dev["vid"], pid=dev["pid"], max_packet0=dev["max_packet0"],
                     bcd_uvc=bcd_uvc, clock_hz=clock_hz, xu_units=xus, xu_unit=xu_unit, vs_interface=vs_if,
                     vs_endpoint=ep, max_packet=(w_max & 0x7FF) * (((w_max >> 11) & 3) + 1),
                     format_index=fmt["index"], format_guid=fmt["guid"], frame_index=frame["index"],
                     width=frame["w"], height=frame["h"], frame_buffer_size=frame["fbs"],
                     intervals=list(frame["intervals"]), warnings=warnings)


# -- expected fixtures (Appendix E)

_CLASS = bytes.fromhex(
    "080b0002 0e030002"                                  # IAD
    "09040000 010e0100 02"                               # interface 0: VideoControl
    "0d240110 014f0080 d1f00801 01"                      # VC header: UVC 1.10, 79 B, 150 MHz
    "12240201 01020000 00000000 00000300 0000"           # input terminal (camera)
    "09240302 01010004 00"                               # output terminal, source 4
    "0d240503 01000003 00000002 00"                      # processing unit 3
    "1a240604 da0bcb2c 3163db4f 850e7905 4dbd5671 02010301 0302")      # XU 4, 2 controls
_EP86 = bytes.fromhex("07058603 400009")                # VC status endpoint (interrupt, 64 B)
_CS_EP = bytes.fromhex("05250340 00")
_VS = bytes.fromhex(
    "09040100 010e0200 00"                               # interface 1: VideoStreaming
    "0e240101 4d008100 02000000 0100"                    # VS input header: 77 B, EP 0x81
    "1b240401 01595559 32000010 00800000 aa00389b 71100100 000000"   # YUY2, 16 bpp
    "1e240501 00900190 0100c027 0900c027 0900e204 000a8b02 00010a8b 0200"   # 400x400, 320000 B, 166666
    "06240d01 0104")                                     # color matching
_DEVICE = {"hs": bytes.fromhex("12011002 ef020140 b40b2103 00010102 0001"),
           "ss": bytes.fromhex("12010003 ef020109 b40b2103 00010102 0001")}
_CONFIG = {"hs": bytes.fromhex("0902d200 02010080 fa"), "ss": bytes.fromhex("0902de00 02010080 40")}
_EP81 = {"hs": bytes.fromhex("07058102 000200"), "ss": bytes.fromhex("07058102 000400")}
_SS_COMPANION_EP86 = bytes.fromhex("06300000 0000")
_SS_COMPANION_EP81 = bytes.fromhex("06300f00 0000")     # bMaxBurst 15

EXPECTED = {
    "hs": {"bcd_usb": 0x0210, "vid": VID, "pid": PID, "max_packet0": 64, "bcd_uvc": 0x0110,
           "clock_hz": 150000000, "xu_unit": 4, "vs_interface": 1, "vs_endpoint": 0x81, "max_packet": 512,
           "format_index": 1, "format_guid": YUY2_GUID, "frame_index": 1, "width": 400, "height": 400,
           "frame_buffer_size": 320000, "intervals": [166666]},
}
EXPECTED["ss"] = dict(EXPECTED["hs"], bcd_usb=0x0300, max_packet0=9, max_packet=1024)


def build_expected(persona: str = "hs") -> bytes:
    """The exact `descriptors` bytes of Appendix E: "hs" (228 B, the Frame) or "ss" (240 B)."""
    if persona not in _DEVICE:
        raise ValueError("persona must be 'hs' or 'ss', not %r" % (persona,))
    if persona == "hs":
        config = _CONFIG["hs"] + _CLASS + _EP86 + _CS_EP + _VS + _EP81["hs"]
    else:
        config = _CONFIG["ss"] + _CLASS + _EP86 + _SS_COMPANION_EP86 + _CS_EP + _VS + _EP81["ss"] + _SS_COMPANION_EP81
    return _DEVICE[persona] + config


_HEX = {"bcd_usb": "0x%04x", "vid": "0x%04x", "pid": "0x%04x", "bcd_uvc": "0x%04x", "vs_endpoint": "0x%02x"}


def _show(name, value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if name in _HEX:
        return _HEX[name] % value
    return str(value)


def diff_expected(layout: VftLayout, persona: str = "hs") -> list:
    """Differences from the expected persona, e.g. "max_packet 1024 != 512"; [] if none."""
    if persona not in EXPECTED:
        raise ValueError("persona must be 'hs' or 'ss', not %r" % (persona,))
    out = []
    for name, want in EXPECTED[persona].items():
        got = getattr(layout, name)
        if got != want:
            out.append("%s %s != %s" % (name, _show(name, got), _show(name, want)))
    unit = next((u for u in layout.xu_units if u.unit_id == layout.xu_unit), None)
    if unit is None or unit.guid != XU_GUID:
        out.append("xu_guid %s != %s" % (unit.guid.hex() if unit else "missing", XU_GUID.hex()))
    return out


def unsupported(layout: VftLayout) -> "str | None":
    """Why this layout cannot be served (service FAULT(unsupported)), or None. Only 400x400 frames
    have the Baballonia-exact pipeline; a 640x480 frame gives the message of spec §4.4."""
    if (layout.vid, layout.pid) == (VID, FOCUS3_PID):
        return "VIVE Focus facial tracker is not supported"
    if (layout.width, layout.height) != (400, 400):
        return "%dx%d format not supported (no Baballonia-exact pipeline)" % (layout.width, layout.height)
    return None


def sha1_hex(raw: bytes) -> str:
    """SHA-1 of the raw descriptors (the key of profile.json)."""
    return hashlib.sha1(bytes(raw)).hexdigest()
