# SPDX-License-Identifier: MIT
"""Baballonia-exact preprocessing of one VIVE Facial Tracker frame (spec §4.15, Appendices B-D, F).

frame (320000 B, 400 x 400 "YUYV", 800 B per row) -> the byte plane of parity `phase` (1 = odd
bytes, Baballonia's YUV2GRAY_Y422) -> columns [col0, col0 + 200) (camera left 0, right 200) ->
OpenCV INTER_LINEAR stretch 200 -> 400 (rows unchanged) -> 5x5 binomial [1 4 6 4 1] blur with
BORDER_REFLECT_101 and (S + 128) >> 8 -> LUT -> 400 x 400 gray (160000 B). No rotation, no flip.

Implementations, all byte-identical:
- preprocess_ref: the literal OpenCV-coefficient oracle (Appendix C), for tests and parity vectors.
- preprocess_swar: big-int SWAR (Appendix B), the stdlib fallback, capped at 30 fps by the service.
- the C helper libffproc.so (csrc/ffproc.c), used only after parity_ok() proved it equal to SWAR.
- StubPreprocessor: tests only, the odd bytes unchanged.
"""
import ctypes
import math
import operator
import os
import threading
from typing import Optional, Protocol

__all__ = [
    "LUT", "FRAME_BYTES", "OUT_BYTES", "FF_ABI", "HELPER_NAME", "lcg_frame", "preprocess_ref",
    "preprocess_swar", "Preprocessor", "SwarPreprocessor", "StubPreprocessor", "CPreprocessor",
    "helper_path", "load_helper", "parity_ok", "make_preprocessor",
]

FRAME_BYTES = 320000          # 400 x 400 x 2
OUT_BYTES = 160000            # 400 x 400 x 1
ROW_BYTES = 800
WIDTH = HEIGHT = 400
COLS = 200                    # columns of one camera
FF_ABI = 1                    # ff_abi() of a usable libffproc.so
HELPER_NAME = "libffproc.so"
SWAR_MAX_FPS = 30

# Appendix F: lut[i] = int((i / 2048) ** 0.4 * 255) (Baballonia, truncation); SHA-256 b2e0b84b...51bbfa.
LUT = bytes((
      0,  12,  15,  18,  21,  22,  24,  26,  27,  29,  30,  31,  32,  33,  34,  35,
     36,  37,  38,  39,  40,  40,  41,  42,  43,  43,  44,  45,  45,  46,  47,  47,
     48,  48,  49,  50,  50,  51,  51,  52,  52,  53,  53,  54,  54,  55,  55,  56,
     56,  57,  57,  58,  58,  59,  59,  60,  60,  60,  61,  61,  62,  62,  62,  63,
     63,  64,  64,  64,  65,  65,  66,  66,  66,  67,  67,  67,  68,  68,  68,  69,
     69,  70,  70,  70,  71,  71,  71,  72,  72,  72,  73,  73,  73,  74,  74,  74,
     74,  75,  75,  75,  76,  76,  76,  77,  77,  77,  78,  78,  78,  78,  79,  79,
     79,  80,  80,  80,  80,  81,  81,  81,  81,  82,  82,  82,  83,  83,  83,  83,
     84,  84,  84,  84,  85,  85,  85,  85,  86,  86,  86,  86,  87,  87,  87,  87,
     88,  88,  88,  88,  89,  89,  89,  89,  90,  90,  90,  90,  91,  91,  91,  91,
     91,  92,  92,  92,  92,  93,  93,  93,  93,  94,  94,  94,  94,  94,  95,  95,
     95,  95,  95,  96,  96,  96,  96,  97,  97,  97,  97,  97,  98,  98,  98,  98,
     98,  99,  99,  99,  99,  99, 100, 100, 100, 100, 100, 101, 101, 101, 101, 101,
    102, 102, 102, 102, 102, 103, 103, 103, 103, 103, 104, 104, 104, 104, 104, 105,
    105, 105, 105, 105, 105, 106, 106, 106, 106, 106, 107, 107, 107, 107, 107, 107,
    108, 108, 108, 108, 108, 109, 109, 109, 109, 109, 109, 110, 110, 110, 110, 110,
))


def lcg_frame(seed: int, n: int = FRAME_BYTES) -> bytes:
    """Deterministic test frame (Appendix D): x = 1664525 x + 1013904223 mod 2^32, byte = x >> 24."""
    x = seed & 0xFFFFFFFF
    out = bytearray(n)
    for i in range(n):
        x = (1664525 * x + 1013904223) & 0xFFFFFFFF
        out[i] = x >> 24
    return bytes(out)


def _args(frame, phase, col0):
    """Validated (frame as bytes or bytearray, phase, col0); ValueError on anything else."""
    if not isinstance(frame, (bytes, bytearray)):
        frame = bytes(frame)                   # memoryview, array, ...
    if len(frame) != FRAME_BYTES:
        raise ValueError("frame must be %d bytes, got %d" % (FRAME_BYTES, len(frame)))
    phase, col0 = operator.index(phase), operator.index(col0)
    if phase not in (0, 1):
        raise ValueError("phase must be 0 or 1, got %r" % (phase,))
    if col0 not in (0, COLS):
        raise ValueError("col0 must be 0 or %d, got %r" % (COLS, col0))
    return frame, phase, col0


# ------------------------------------------------------------------ oracle (Appendix C)

def _r101(i, n):
    """BORDER_REFLECT_101 index for i in -2..n+1."""
    return -i if i < 0 else (2 * n - 2 - i if i >= n else i)


def _resize_coefficients(src_w=COLS, dst_w=WIDTH):
    """Per output column (sx, sx + 1 clamped, c0, c1) as cv::resize INTER_LINEAR computes them:
    half-pixel centres, fx clamped to 0 at both borders, INTER_RESIZE_COEF_SCALE = 2048."""
    scale = src_w / dst_w
    table = []
    for dx in range(dst_w):
        fx = (dx + 0.5) * scale - 0.5
        sx = math.floor(fx)
        fx -= sx
        if sx < 0:
            fx, sx = 0.0, 0
        if sx >= src_w - 1:
            fx, sx = 0.0, src_w - 1
        c0 = round((1.0 - fx) * 2048)
        table.append((sx, min(sx + 1, src_w - 1), c0, 2048 - c0))
    return tuple(table)


_RESIZE = _resize_coefficients()
_TAPS_X = tuple(tuple(_r101(x + j - 2, WIDTH) for j in range(5)) for x in range(WIDTH))
_TAPS_Y = tuple(tuple(_r101(y + i - 2, HEIGHT) for i in range(5)) for y in range(HEIGHT))


def preprocess_ref(frame: bytes, phase: int = 1, col0: int = 0) -> bytes:
    """The oracle: OpenCV's resize coefficients and bit-exact 5x5 Gaussian taken literally, pixel by
    pixel (~0.1 s per frame). Tests and parity vectors only."""
    frame, phase, col0 = _args(frame, phase, col0)
    hs = []                                    # horizontal [1 4 6 4 1] sums of the stretched rows
    for y in range(HEIGHT):
        start = y * ROW_BYTES + 2 * col0 + phase
        s = frame[start:start + 2 * COLS:2]    # gray(col0 + k, y), k = 0..199
        # horizontal resize pass (coefficients scaled by 2048), then the vertical pass with
        # beta = (2048, 0) because the row scale is 1: (d * 2048 + 2^21) >> 22
        img = [((s[a] * c0 + s[b] * c1) * 2048 + (1 << 21)) >> 22 for a, b, c0, c1 in _RESIZE]
        hs.append([img[i0] + 4 * img[i1] + 6 * img[i2] + 4 * img[i3] + img[i4]
                   for i0, i1, i2, i3, i4 in _TAPS_X])
    out = bytearray(OUT_BYTES)
    for y, (i0, i1, i2, i3, i4) in enumerate(_TAPS_Y):
        out[y * WIDTH:(y + 1) * WIDTH] = bytes(
            LUT[(a + 4 * b + 6 * c + 4 * d + e + 128) >> 8]
            for a, b, c, d, e in zip(hs[i0], hs[i1], hs[i2], hs[i3], hs[i4]))
    return bytes(out)


# ------------------------------------------------------------------ SWAR (Appendix B)

_L1 = 202                     # lanes per row for the stretch: s[-1], s[0..199], s[200]
_P = 404                      # padded width/height for the blur (2 px reflect-101 border)


def _lanes(n, v):
    return int.from_bytes(v.to_bytes(2, "little") * n, "little")


_C2 = _lanes(HEIGHT * _L1, 2)
_M14 = _lanes(HEIGHT * _L1, 0x3FFF)
_C128 = _lanes(_P * _P, 128)
_MALL = _lanes(_P * _P, 0xFFFF)
_ROWB = 16 * _P               # bits per padded row
_ZERO_A = bytes(2 * HEIGHT * _L1)
_ZERO_B = bytes(2 * _P * _P)


def preprocess_swar(frame: bytes, phase: int = 1, col0: int = 0) -> bytes:
    """Stdlib fallback: 16-bit lanes in one big integer per pass (~4 ms per frame on a PC).
    Lane maxima: stretch sums <= 1022, blur sums <= 65280 + 128 < 65536, so no carry crosses a lane."""
    frame, phase, col0 = _args(frame, phase, col0)
    plane = frame[phase::2]                                       # 160000 B, 400 x 400
    rows = [plane[r * WIDTH + col0: r * WIDTH + col0 + COLS] for r in range(HEIGHT)]
    padded = b"".join(r[:1] + r + r[-1:] for r in rows)           # replicate pad (fx clamped at borders)
    a = bytearray(_ZERO_A)
    a[0::2] = padded
    x = int.from_bytes(a, "little")
    even = (((x << 16) + 3 * x + _C2) >> 2) & _M14                # out[2k]   = (s[k-1] + 3 s[k] + 2) >> 2
    odd = ((3 * x + (x >> 16) + _C2) >> 2) & _M14                 # out[2k+1] = (3 s[k] + s[k+1] + 2) >> 2
    e = even.to_bytes(2 * HEIGHT * _L1, "little")[0::2]
    o = odd.to_bytes(2 * HEIGHT * _L1, "little")[0::2]
    st = bytearray(2 * HEIGHT * _L1)                              # rows of 404: [E0 O0 E1 O1 ... E201 O201]
    st[0::2] = e
    st[1::2] = o                                                  # valid pixels at row offsets 2..401
    st[0::_P] = st[4::_P]                                         # reflect-101: x = -2 -> 2
    st[1::_P] = st[3::_P]                                         #              x = -1 -> 1
    st[402::_P] = st[400::_P]                                     #              x = 400 -> 398
    st[403::_P] = st[399::_P]                                     #              x = 401 -> 397
    img = bytes(st)
    padimg = img[2 * _P:3 * _P] + img[1 * _P:2 * _P] + img + img[398 * _P:399 * _P] + img[397 * _P:398 * _P]
    b = bytearray(_ZERO_B)
    b[0::2] = padimg
    y = int.from_bytes(b, "little")
    hs = (y >> 32) + 4 * (y >> 16) + 6 * y + 4 * (y << 16) + (y << 32)
    v = (hs >> 2 * _ROWB) + 4 * (hs >> _ROWB) + 6 * hs + 4 * (hs << _ROWB) + (hs << 2 * _ROWB)
    r = ((v + _C128) & _MALL).to_bytes(2 * _P * _P, "little")[1::2]   # high byte = (S + 128) >> 8
    out = b"".join(r[k * _P + 2: k * _P + 402] for k in range(2, 402))
    return out.translate(LUT)


# ------------------------------------------------------------------ preprocessors

class Preprocessor(Protocol):
    name: str                                  # "c" | "swar" | "stub"
    max_fps: Optional[int]                     # 30 for swar

    def __call__(self, frame, phase: int, col0: int) -> bytes: ...


class SwarPreprocessor:
    """preprocess_swar as a Preprocessor. `reason` says why the C helper is not used:
    "" (SWAR requested), "load", "abi" or "parity"; `path` is the helper that was tried."""
    name = "swar"
    max_fps = SWAR_MAX_FPS

    def __init__(self, reason: str = "", path: Optional[str] = None):
        self.reason = reason
        self.path = path

    def __call__(self, frame, phase: int = 1, col0: int = 0) -> bytes:
        return preprocess_swar(frame, phase, col0)


class StubPreprocessor:
    """Tests only: the odd bytes of the frame unchanged (160000 B for a full frame)."""
    name = "stub"
    max_fps = None
    reason = ""
    path = None

    def __call__(self, frame, phase: int = 1, col0: int = 0) -> bytes:
        return bytes(frame[1::2])


class CPreprocessor:
    """ff_preprocess() of a loaded libffproc.so. The output buffer is preallocated; calls are
    serialised by a lock (ctypes releases the GIL during the C call)."""
    name = "c"
    max_fps = None
    reason = ""

    def __init__(self, lib, path: str):
        self.path = path
        self._lib = lib                        # keeps the shared object loaded
        fn = lib.ff_preprocess
        fn.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_int,
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)
        fn.restype = ctypes.c_int
        self._fn = fn
        self._lut = (ctypes.c_ubyte * 256).from_buffer_copy(LUT)
        self._out = (ctypes.c_ubyte * OUT_BYTES)()
        self._lock = threading.Lock()

    def __call__(self, frame, phase: int = 1, col0: int = 0) -> bytes:
        if isinstance(frame, bytearray):
            src = (ctypes.c_char * len(frame)).from_buffer(frame)     # no copy
        else:
            if not isinstance(frame, bytes):
                frame = bytes(frame)
            src = ctypes.c_char_p(frame)                              # no copy
        with self._lock:
            rc = self._fn(src, len(frame), phase, col0, self._lut, self._out, OUT_BYTES)
            if rc != 0:
                raise ValueError("ff_preprocess rejected its arguments (len=%d phase=%r col0=%r)"
                                 % (len(frame), phase, col0))
            return bytes(self._out)


def helper_path(lib_dir: Optional[str] = None) -> str:
    """Where make_preprocessor looks: lib_dir/libffproc.so; without lib_dir $FRAMEFACE_LIBFFPROC
    (the test runner's freshly built helper), else ~/frameface/lib/libffproc.so."""
    if lib_dir:
        return os.path.join(lib_dir, HELPER_NAME)
    env = os.environ.get("FRAMEFACE_LIBFFPROC", "").strip()
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), "frameface", "lib", HELPER_NAME)


def _load(path):
    """(CPreprocessor, "") or (None, reason) with reason "load" or "abi"."""
    try:
        lib = ctypes.CDLL(path)
        abi = lib.ff_abi
        if not hasattr(lib, "ff_preprocess"):
            return None, "load"
    except (OSError, AttributeError, TypeError, ValueError):
        return None, "load"
    abi.argtypes = ()
    abi.restype = ctypes.c_int
    if abi() != FF_ABI:
        return None, "abi"
    return CPreprocessor(lib, path), ""


def load_helper(path: str) -> Optional[Preprocessor]:
    """The C helper at `path` if it loads and ff_abi() == FF_ABI, else None. Not yet trusted:
    callers run parity_ok() before using it (make_preprocessor does)."""
    return _load(path)[0]


_parity_lock = threading.Lock()
_parity_cache = []


def _parity_set():
    """The §4.15 parity set: (frame, phase, col0, SWAR output) for lcg_frame(1), lcg_frame(2) and an
    all-255 frame, phase (0, 1) x col0 (0, 200). Computed once."""
    with _parity_lock:
        if not _parity_cache:
            for frame in (lcg_frame(1), lcg_frame(2), bytes([255]) * FRAME_BYTES):
                for phase in (0, 1):
                    for col0 in (0, COLS):
                        _parity_cache.append((frame, phase, col0, preprocess_swar(frame, phase, col0)))
        return list(_parity_cache)


def parity_ok(candidate: Preprocessor) -> bool:
    """True if candidate(frame, phase, col0) == preprocess_swar(...) byte for byte on the whole
    parity set; any exception counts as a mismatch."""
    for frame, phase, col0, expected in _parity_set():
        try:
            if candidate(frame, phase, col0) != expected:
                return False
        except Exception:                      # a broken helper must never take the service down
            return False
    return True


def _warn_rejected(log, reason):
    """W pixels: c helper rejected reason=<abi|parity|load> (§4.20), through a Log-like object
    (w(component, event, **kv)) or a plain callable taking one line."""
    if log is None:
        return
    warn = getattr(log, "w", None)
    if callable(warn):
        warn("pixels", "c helper rejected", reason=reason)
    elif callable(log):
        log("W pixels: c helper rejected reason=%s" % reason)


def make_preprocessor(lib_dir: Optional[str], mode: str = "auto", log=None) -> Preprocessor:
    """The service's preprocessor for config `preprocess` (auto | c | swar), §4.15.5.

    auto and c load helper_path(lib_dir), check ff_abi() == 1 and parity_ok(); any failure gives
    SWAR with a WARN (never an exception) and SwarPreprocessor.reason set to load, abi or parity."""
    if mode == "swar":
        return SwarPreprocessor()
    if mode not in ("auto", "c"):
        raise ValueError("preprocess mode must be auto, c or swar, not %r" % (mode,))
    path = helper_path(lib_dir)
    helper, reason = _load(path)
    if helper is not None and not parity_ok(helper):
        helper, reason = None, "parity"
    if helper is not None:
        return helper
    _warn_rejected(log, reason)
    return SwarPreprocessor(reason, path)
