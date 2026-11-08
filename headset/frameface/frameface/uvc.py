# SPDX-License-Identifier: MIT
"""UVC control requests, the tracker's extension-unit (XU) commands, probe/commit and payload
headers (spec §4.5, §4.8, §4.10). Pure: builders and parsers only, no I/O.

Setups are the 8-byte USB SETUP packets '<BBHHH'. XU requests address unit 4, selector 2 on the
VideoControl interface 0 (wIndex = unit << 8 | interface, wValue = selector << 8); probe/commit
address the VideoStreaming interface 1.

XU commands are zero-padded to the activation's XU length n (GET_LEN, or 384 on rung S5); only
MAGIC carries the trailer [254]=0x53 [255]=0x54, and only when n >= 256. A GET_CUR reply starts
with 0x55 (busy, poll again), 0x56 (done: bytes 1.. echo the command; byte 17 holds the value of
a register read) or an error code.
"""
import dataclasses
from dataclasses import dataclass
import enum
import struct

__all__ = [
    "SET_CUR", "GET_CUR", "GET_MIN", "GET_MAX", "GET_RES", "GET_LEN", "GET_INFO", "GET_DEF",
    "VS_PROBE", "VS_COMMIT", "XU_UNIT", "XU_SELECTOR", "CTRL_TIMEOUT_MS", "ACK_BUSY", "ACK_DONE",
    "MAGIC", "STREAM_OFF", "STREAM_ON", "IR_REG_ADDRS", "FID", "EOF", "PTS", "SCR", "ERR", "EOH",
    "PROBE_FORMATS", "Setup", "xu_setup", "vs_setup", "vc_error_code_setup", "set_interface_setup",
    "pad", "command_length", "magic", "stream_cmd", "reg_write", "reg_read", "ir_regs", "Ack",
    "parse_ack", "done_reply", "probe_length", "Probe", "pack_probe", "unpack_probe",
    "request_probe", "fix_probe", "Header", "parse_header",
]

# bmRequestType values: class request to an interface, OUT / IN; standard request to an interface
RT_CLASS_OUT, RT_CLASS_IN, RT_STD_INTERFACE = 0x21, 0xA1, 0x01
# UVC request codes (bRequest)
SET_CUR, GET_CUR, GET_MIN, GET_MAX, GET_RES, GET_LEN, GET_INFO, GET_DEF = (
    0x01, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87)
SET_INTERFACE = 0x0B                    # standard request (raw fallback of the SETINTERFACE ioctl)
VS_PROBE, VS_COMMIT = 1, 2              # VideoStreaming control selectors
VC_REQUEST_ERROR_CODE = 2               # VideoControl selector, read after a STALL
VC_INTERFACE, VS_INTERFACE = 0, 1
XU_UNIT, XU_SELECTOR = 4, 2
CTRL_TIMEOUT_MS = 1000

ACK_BUSY, ACK_DONE = 0x55, 0x56

MAGIC = b"\x51\x52"
MAGIC_TRAILER = b"\x53\x54"             # at [254], [255] when n >= 256
STREAM_OFF = b"\x50\x14\x00\x00"
STREAM_ON = b"\x50\x14\x00\x01"
_REG_HEAD = b"\xab\x60\x01\x01\x00\x00\x00"      # target 0xab, I2C address 0x60, widths 1/1, address bytes
_REG_MID = b"\x90\x01\x00\x01\x00\x00\x00"       # (only the low address byte and the value byte vary)
REG_CMD_LEN = 17
IR_REG_ADDRS = (0x00, 0x08, 0x70, 0x02, 0x03, 0x04, 0x0E, 0x05, 0x06, 0x07, 0x0F)

# bmHeaderInfo bits of a payload header
FID, EOF, PTS, SCR, RES, STI, ERR, EOH = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80

PROBE_FORMATS = {26: "<HBBIHHHHHII", 34: "<HBBIHHHHHIIIBBBB", 48: "<HBBIHHHHHIIIBBBBBBBBHQ"}


def _u8(name, v):
    if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 0xFF:
        raise ValueError("%s must be a byte value, not %r" % (name, v))
    return v


# -- SETUP packets

@dataclass(frozen=True)
class Setup:
    request_type: int
    request: int
    value: int
    index: int
    length: int

    def pack(self) -> bytes:
        """The 8-byte SETUP packet: bmRequestType, bRequest, wValue, wIndex, wLength (LE)."""
        return struct.pack("<BBHHH", self.request_type, self.request, self.value, self.index, self.length)

    @property
    def is_in(self) -> bool:
        return bool(self.request_type & 0x80)


def _class_type(request):
    return RT_CLASS_IN if request & 0x80 else RT_CLASS_OUT


def xu_setup(request: int, n: int, unit: int = XU_UNIT, selector: int = XU_SELECTOR) -> Setup:
    """An XU request on the VideoControl interface: GET_LEN (n=2), GET_INFO (n=1), SET_CUR/GET_CUR (n)."""
    return Setup(_class_type(request), request, selector << 8, unit << 8 | VC_INTERFACE, n)


def vs_setup(request: int, control: int, n: int, interface: int = VS_INTERFACE) -> Setup:
    """A VideoStreaming probe/commit request (control VS_PROBE=1 or VS_COMMIT=2) of n bytes."""
    return Setup(_class_type(request), request, control << 8, interface, n)


def vc_error_code_setup() -> Setup:
    """VC REQUEST_ERROR_CODE (A1 81 00 02 00 00 01 00), read after a STALL; 1 byte."""
    return Setup(RT_CLASS_IN, GET_CUR, VC_REQUEST_ERROR_CODE << 8, VC_INTERFACE, 1)


def set_interface_setup(interface: int = VS_INTERFACE, alt: int = 0) -> Setup:
    """The raw SET_INTERFACE request (01 0B 00 00 01 00 00 00), the ioctl's fallback."""
    return Setup(RT_STD_INTERFACE, SET_INTERFACE, alt, interface, 0)


# -- XU commands

def pad(cmd: bytes, n: int) -> bytes:
    """`cmd` zero-padded to n bytes; ValueError if it does not fit."""
    if not isinstance(n, int) or isinstance(n, bool) or n < len(cmd) or n > 0xFFFF:
        raise ValueError("an XU command of %d bytes does not fit length %r" % (len(cmd), n))
    return bytes(cmd) + bytes(n - len(cmd))


def command_length(cmd: bytes) -> int:
    """The unpadded length of an XU command (padded or not): 2 for MAGIC, 4 for STREAM, 17 for a
    register write/read, else the length without trailing zero bytes."""
    head = bytes(cmd[:2])
    if head == MAGIC:
        return 2
    if head == STREAM_OFF[:2]:
        return 4
    if len(head) == 2 and head[0] in (0x50, 0x51) and head[1] == 0xAB:
        return REG_CMD_LEN
    return len(bytes(cmd).rstrip(b"\0"))


def magic(n: int) -> bytes:
    """MAGIC: 51 52 padded to n, plus [254]=53 [255]=54 when n >= 256."""
    out = bytearray(pad(MAGIC, n))
    if n >= 256:
        out[254:256] = MAGIC_TRAILER
    return bytes(out)


def stream_cmd(on: bool, n: int) -> bytes:
    """STREAM_ON (50 14 00 01) or STREAM_OFF (50 14 00 00) padded to n."""
    return pad(STREAM_ON if on else STREAM_OFF, n)


def reg_write(addr: int, value: int, n: int) -> bytes:
    """REG_WRITE(a, v) = 50 ab 60 01 01 00 00 00 a 90 01 00 01 00 00 00 v, padded to n."""
    return pad(b"\x50" + _REG_HEAD + bytes([_u8("addr", addr)]) + _REG_MID + bytes([_u8("value", value)]), n)


def reg_read(addr: int, n: int) -> bytes:
    """REG_READ(a) = 51 ab 60 01 01 00 00 00 a 90 01 00 01 00 00 00 00, padded to n (probe only)."""
    return pad(b"\x51" + _REG_HEAD + bytes([_u8("addr", addr)]) + _REG_MID + b"\x00", n)


def ir_regs(pwm: int = 0xFF, current: int = 0xB2) -> tuple:
    """The 11 (register, value) writes in protocol order; PWM on 0x02-0x04, current on 0x05-0x07.
    ON = ir_regs(0xFF, current of the IR level); OFF = ir_regs(0x00, current of the IR level)."""
    pwm, current = _u8("pwm", pwm), _u8("current", current)
    values = (0x40, 0x01, 0x00, pwm, pwm, pwm, 0x00, current, current, current, 0x03)
    return tuple(zip(IR_REG_ADDRS, values))


class Ack(str, enum.Enum):
    BUSY = "busy"
    DONE = "done"
    ERROR = "error"

    def __str__(self):                  # "done" in logs on every Python version (3.11 changed str/format)
        return self.value

    __format__ = str.__format__


def parse_ack(cmd: bytes, reply: bytes) -> tuple:
    """Classify a GET_CUR reply to `cmd` (padded or not): (Ack, reply[17] or None, echo16_ok).

    0x55 -> BUSY. 0x56 with reply[1:1+k] == cmd[0:k], k = min(16, unpadded length) -> DONE.
    Anything else -> ERROR: an empty reply, another code (XuError(code)), or a 0x56 whose echo
    does not match or is cut short. echo16_ok = reply[1:17] == the padded command's first 16 bytes.
    """
    reply = bytes(reply)
    cmd = bytes(cmd)
    value = reply[17] if len(reply) > 17 else None
    echo16 = reply[1:17] == (cmd[:16] + bytes(16))[:16]
    if not reply:
        return Ack.ERROR, value, echo16
    if reply[0] == ACK_BUSY:
        return Ack.BUSY, value, echo16
    k = min(16, command_length(cmd))
    if reply[0] == ACK_DONE and len(reply) >= 1 + k and reply[1:1 + k] == cmd[:k]:
        return Ack.DONE, value, echo16
    return Ack.ERROR, value, echo16


def done_reply(cmd: bytes, n: int, value: int = 0) -> bytes:
    """A device's "done" GET_CUR reply to `cmd`: 56, cmd[0:16], the value at byte 17, padded to n."""
    head = b"\x56" + (bytes(cmd[:16]) + bytes(16))[:16] + bytes([_u8("value", value)])
    return (head + bytes(n))[:n]


# -- probe / commit (§4.8)

def probe_length(bcd_uvc: int) -> int:
    """Probe/commit length: 26 B before UVC 1.1, 34 B before UVC 1.5, else 48 B."""
    return 26 if bcd_uvc < 0x0110 else 34 if bcd_uvc < 0x0150 else 48


@dataclass
class Probe:
    hint: int
    format_index: int
    frame_index: int
    frame_interval: int
    key_frame_rate: int
    p_frame_rate: int
    comp_quality: int
    comp_window_size: int
    delay: int
    max_video_frame_size: int
    max_payload: int
    clock_hz: int = 0
    framing_info: int = 0
    preferred_version: int = 0
    min_version: int = 0
    max_version: int = 0
    raw: bytes = b""


_PROBE_FIELDS = ("hint", "format_index", "frame_index", "frame_interval", "key_frame_rate", "p_frame_rate",
                 "comp_quality", "comp_window_size", "delay", "max_video_frame_size", "max_payload",
                 "clock_hz", "framing_info", "preferred_version", "min_version", "max_version")


def pack_probe(p: Probe, n: int) -> bytes:
    """The n-byte (26/34/48) probe block. At 48 B the six UVC 1.5 fields that Probe does not model
    come from p.raw[34:48] (the device's own answer), else zeros."""
    if n not in PROBE_FORMATS:
        raise ValueError("probe length must be 26, 34 or 48, not %r" % (n,))
    if n == 26:
        return struct.pack(PROBE_FORMATS[26], *(getattr(p, f) for f in _PROBE_FIELDS[:11]))
    out = struct.pack(PROBE_FORMATS[34], *(getattr(p, f) for f in _PROBE_FIELDS))
    if n == 48:
        out += bytes(p.raw[34:48]) if len(p.raw) >= 48 else bytes(14)
    return out


def unpack_probe(b: bytes) -> Probe:
    """Parse a probe answer of any length (shorter than 26 B is zero-padded); raw keeps the bytes."""
    raw = bytes(b)
    n = 48 if len(raw) >= 48 else 34 if len(raw) >= 34 else 26
    data = (raw + bytes(26))[:n]
    values = struct.unpack(PROBE_FORMATS[n], data)[:16]
    return Probe(*values, raw=raw)


def request_probe(base, n: int, fmt: int, frm: int, interval: int) -> bytes:
    """The PROBE SET_CUR block: `base` (GET_CUR or GET_DEF answer; None = zeros) cut or padded to
    n bytes, with bytes 0-7 = bmHint 1, format, frame, interval (01 00 01 01 0a 8b 02 00)."""
    if n not in PROBE_FORMATS:
        raise ValueError("probe length must be 26, 34 or 48, not %r" % (n,))
    buf = bytearray(n)
    if base:
        base = bytes(base[:n])
        buf[:len(base)] = base
    struct.pack_into("<HBBI", buf, 0, 1, fmt, frm, interval)
    return bytes(buf)


def fix_probe(p: Probe, frame_buffer_size: int) -> tuple:
    """uvcvideo's fix-ups: max_video_frame_size := the descriptor's frame buffer size (device
    value reported); max_payload masked with 0xFFFF if its high half is 0xFFFF, and 32768 if it
    is then 0 or above 1 MiB. Returns (new Probe, warnings)."""
    warnings = []
    size = p.max_video_frame_size
    if frame_buffer_size and size != frame_buffer_size:
        warnings.append("probe max_video_frame_size %d != descriptor %d; using %d"
                        % (size, frame_buffer_size, frame_buffer_size))
        size = frame_buffer_size
    v = p.max_payload
    if (v & 0xFFFF0000) == 0xFFFF0000:
        warnings.append("probe max_payload 0x%08x masked to %d" % (v, v & 0xFFFF))
        v &= 0xFFFF
    if v == 0 or v > 1048576:
        warnings.append("probe max_payload %d invalid; using 32768" % v)
        v = 32768
    return dataclasses.replace(p, max_video_frame_size=size, max_payload=v), warnings


# -- payload header (§4.10)

@dataclass(frozen=True)
class Header:
    length: int
    flags: int
    fid: int
    eof: bool
    err: bool
    pts: "int | None"
    scr: "int | None"            # source clock (STC) part of the SCR, u32


def _u32(buf, i):
    return buf[i] | buf[i + 1] << 8 | buf[i + 2] << 16 | buf[i + 3] << 24


def parse_header(buf, off: int, n: int, strict: bool) -> "Header | None":
    """The payload header at buf[off] of a payload with n bytes from off, or None if invalid.

    strict: bHeaderLength == 12, PTS and SCR set, RES and STI clear (ERR allowed, EOH ignored).
    lenient: 2 <= bHeaderLength <= n and bHeaderLength >= 2 + 4*PTS + 6*SCR.
    """
    if n < 2 or off < 0 or off + 2 > len(buf):
        return None
    hl, fl = buf[off], buf[off + 1]
    if strict:
        if hl != 12 or n < 12 or (fl & (PTS | SCR)) != (PTS | SCR) or fl & (RES | STI):
            return None
    elif hl < 2 or hl > n or hl < 2 + (4 if fl & PTS else 0) + (6 if fl & SCR else 0):
        return None
    if off + hl > len(buf):
        return None
    i = off + 2
    pts = scr = None
    if fl & PTS:
        pts = _u32(buf, i)
        i += 4
    if fl & SCR:
        scr = _u32(buf, i)
    return Header(hl, fl, fl & FID, bool(fl & EOF), bool(fl & ERR), pts, scr)
