# SPDX-License-Identifier: MIT
"""JPEG for the face stream (spec §4.16): libturbojpeg through ctypes, the TurboJPEG 3 API (tj3*)
when the library has it, else the old tj2 API (tj*, e.g. Ubuntu's 2.1.x). Gray 400 x 400, quality
92 by default (90..100), accurate DCT, JFIF only, a preallocated output buffer (NOREALLOC).

    JpegEncoder.load()      dlopen libturbojpeg.so.0, libturbojpeg.so, then find_library("turbojpeg")
    encode_gray(pixels)     bytes; a TurboJPEG warning is accepted (stats["jpeg_warn"]), a fatal
                            error raises JpegError (the caller drops that frame)
    validate_jpeg(data)     SOI first, the only EOI last, < 1 MiB (what Baballonia's parser needs)
    decode_gray(data)       gray pixels, or None without a TurboJPEG decoder
    StubJpegEncoder         tests only: marker-valid, not decodable
"""
import ctypes
import ctypes.util
import operator
import struct
import threading

__all__ = [
    "JpegError", "JpegEncoder", "StubJpegEncoder", "validate_jpeg", "decode_gray", "clamp_quality",
    "LIB_NAMES", "MAX_JPEG_BYTES", "QUALITY_DEFAULT", "QUALITY_MIN", "QUALITY_MAX",
]

LIB_NAMES = ("libturbojpeg.so.0", "libturbojpeg.so")
MAX_JPEG_BYTES = 1 << 20      # Baballonia's frame buffer (§c.3): a JPEG must be smaller
QUALITY_DEFAULT, QUALITY_MIN, QUALITY_MAX = 92, 90, 100
WIDTH = HEIGHT = 400

# turbojpeg.h (libjpeg-turbo 3.0, research §b.17)
TJINIT_COMPRESS, TJINIT_DECOMPRESS = 0, 1
TJSAMP_GRAY = 3
TJPF_GRAY = 6
TJPARAM_NOREALLOC, TJPARAM_QUALITY, TJPARAM_SUBSAMP = 2, 3, 4
TJPARAM_JPEGWIDTH, TJPARAM_JPEGHEIGHT, TJPARAM_FASTDCT = 5, 6, 10
TJFLAG_NOREALLOC, TJFLAG_ACCURATEDCT = 1024, 4096
TJERR_WARNING, TJERR_FATAL = 0, 1

_C = ctypes
_PTR_PTR = _C.POINTER(_C.c_void_p)
_PROTOTYPES = {               # name: (argtypes, restype)
    "tj3Init": ((_C.c_int,), _C.c_void_p),
    "tj3Set": ((_C.c_void_p, _C.c_int, _C.c_int), _C.c_int),
    "tj3Get": ((_C.c_void_p, _C.c_int), _C.c_int),
    "tj3JPEGBufSize": ((_C.c_int, _C.c_int, _C.c_int), _C.c_size_t),
    "tj3Alloc": ((_C.c_size_t,), _C.c_void_p),
    "tj3Free": ((_C.c_void_p,), None),
    "tj3Destroy": ((_C.c_void_p,), None),
    "tj3Compress8": ((_C.c_void_p, _C.c_char_p, _C.c_int, _C.c_int, _C.c_int, _C.c_int, _PTR_PTR,
                      _C.POINTER(_C.c_size_t)), _C.c_int),
    "tj3DecompressHeader": ((_C.c_void_p, _C.c_char_p, _C.c_size_t), _C.c_int),
    "tj3Decompress8": ((_C.c_void_p, _C.c_char_p, _C.c_size_t, _C.c_void_p, _C.c_int, _C.c_int),
                       _C.c_int),
    "tj3GetErrorCode": ((_C.c_void_p,), _C.c_int),
    "tj3GetErrorStr": ((_C.c_void_p,), _C.c_char_p),
    "tjInitCompress": ((), _C.c_void_p),
    "tjInitDecompress": ((), _C.c_void_p),
    "tjBufSize": ((_C.c_int, _C.c_int, _C.c_int), _C.c_ulong),
    "tjAlloc": ((_C.c_int,), _C.c_void_p),
    "tjFree": ((_C.c_void_p,), None),
    "tjDestroy": ((_C.c_void_p,), _C.c_int),
    "tjCompress2": ((_C.c_void_p, _C.c_char_p, _C.c_int, _C.c_int, _C.c_int, _C.c_int, _PTR_PTR,
                     _C.POINTER(_C.c_ulong), _C.c_int, _C.c_int, _C.c_int), _C.c_int),
    "tjDecompressHeader3": ((_C.c_void_p, _C.c_char_p, _C.c_ulong, _C.POINTER(_C.c_int),
                             _C.POINTER(_C.c_int), _C.POINTER(_C.c_int), _C.POINTER(_C.c_int)), _C.c_int),
    "tjDecompress2": ((_C.c_void_p, _C.c_char_p, _C.c_ulong, _C.c_void_p, _C.c_int, _C.c_int, _C.c_int,
                       _C.c_int, _C.c_int), _C.c_int),
    "tjGetErrorCode": ((_C.c_void_p,), _C.c_int),
    "tjGetErrorStr2": ((_C.c_void_p,), _C.c_char_p),
    "tjGetErrorStr": ((), _C.c_char_p),
}


class JpegError(Exception):
    """No usable TurboJPEG, or an encode/decode that failed (service: FAULT(library) / drop)."""


def clamp_quality(quality) -> int:
    """JPEG quality limited to 90..100 (lower values are raised to the floor of 90)."""
    q = operator.index(quality)
    return QUALITY_MIN if q < QUALITY_MIN else (QUALITY_MAX if q > QUALITY_MAX else q)


def _dlopen(name):
    return ctypes.CDLL(name)


def _find_library():
    return ctypes.util.find_library("turbojpeg")


def _api_of(lib):
    if hasattr(lib, "tj3Init"):
        return "tj3"
    if hasattr(lib, "tjInitCompress"):
        return "tj2"
    return None


def _open(names=LIB_NAMES):
    """(lib, name, api) of the first library that loads and has tj3Init or tjInitCompress: the
    given names in order, then ctypes.util.find_library("turbojpeg") (asked only if they fail)."""
    tried = []

    def attempt(name):
        try:
            lib = _dlopen(name)
        except OSError as e:
            tried.append("%s: %s" % (name, e))
            return None
        api = _api_of(lib)
        if api is None:
            tried.append("%s: neither tj3Init nor tjInitCompress" % name)
            return None
        return lib, name, api

    for name in names:
        found = attempt(name)
        if found:
            return found
    name = _find_library()
    if name and name not in names:
        found = attempt(name)
        if found:
            return found
    raise JpegError("libturbojpeg not usable (" + "; ".join(tried or ["nothing to try"]) + ")")


def _symbol(lib, name, required=True):
    """lib.<name> with the argtypes/restype of _PROTOTYPES, or None if optional and missing."""
    try:
        fn = getattr(lib, name)
    except AttributeError:
        if required:
            raise JpegError("libturbojpeg lacks %s" % name) from None
        return None
    fn.argtypes, fn.restype = _PROTOTYPES[name]
    return fn


def _source(pixels, n):
    """Pixels as a c_char_p-compatible argument without a copy (bytes, or a bytearray view)."""
    if not isinstance(pixels, (bytes, bytearray)):
        pixels = bytes(pixels)                 # memoryview, array, ...
    if len(pixels) != n:
        raise JpegError("expected %d gray pixels, got %d" % (n, len(pixels)))
    if isinstance(pixels, bytearray):
        return (ctypes.c_char * n).from_buffer(pixels)
    return pixels


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    return "" if value is None else str(value)


class JpegEncoder:
    """One TurboJPEG compressor handle with a preallocated output buffer. Calls are serialised by a
    lock; the service uses an encoder from the pixels thread only (the placeholder is encoded before
    the threads start)."""

    def __init__(self, lib, api: str, quality: int = QUALITY_DEFAULT, name: str = ""):
        if api not in ("tj3", "tj2"):
            raise JpegError("unknown TurboJPEG API %r" % (api,))
        self.api = api
        self.name = name
        self.quality = clamp_quality(quality)
        self.stats = {"encoded": 0, "jpeg_warn": 0, "jpeg_fatal": 0}
        self.last_error = ""
        self._lib = lib
        self._lock = threading.Lock()
        self._handle = self._buf = None
        self._cap, self._dims, self._q_set = 0, None, None
        self._bufptr = ctypes.c_void_p()
        self._bufptr_p = ctypes.pointer(self._bufptr)
        if api == "tj3":
            self._size = ctypes.c_size_t()
            names = ("tj3Init", "tj3Set", "tj3JPEGBufSize", "tj3Alloc", "tj3Free", "tj3Destroy",
                     "tj3Compress8", "tj3GetErrorCode", "tj3GetErrorStr")
            (self._init, self._set, self._bufsize, self._alloc, self._free, self._destroy,
             self._compress, self._errcode, self._errstr) = (_symbol(lib, n) for n in names)
        else:
            self._size = ctypes.c_ulong()
            names = ("tjInitCompress", "tjBufSize", "tjAlloc", "tjFree", "tjDestroy", "tjCompress2")
            (self._init, self._bufsize, self._alloc, self._free, self._destroy,
             self._compress) = (_symbol(lib, n) for n in names)
            self._errcode = _symbol(lib, "tjGetErrorCode", required=False)
            self._errstr = _symbol(lib, "tjGetErrorStr2", required=False)
            self._errstr_old = None if self._errstr else _symbol(lib, "tjGetErrorStr", required=False)
        self._size_p = ctypes.pointer(self._size)
        try:
            self._open_handle()
            self._ensure_buffer(WIDTH, HEIGHT)
        except Exception:
            self.close()
            raise

    @classmethod
    def load(cls, names=LIB_NAMES, quality: int = QUALITY_DEFAULT) -> "JpegEncoder":
        """Encoder on the first usable libturbojpeg (tj3 if tj3Init exists, else tj2); JpegError if none."""
        lib, name, api = _open(names)
        return cls(lib, api, quality, name)

    # -- setup

    def _open_handle(self):
        if self.api == "tj3":
            handle = self._init(TJINIT_COMPRESS)
            if not handle:
                raise JpegError("tj3Init failed: %s" % self._error(None))
            self._handle = handle
            for param, value in ((TJPARAM_QUALITY, self.quality), (TJPARAM_SUBSAMP, TJSAMP_GRAY),
                                 (TJPARAM_NOREALLOC, 1), (TJPARAM_FASTDCT, 0)):
                if self._set(handle, param, value) != 0:
                    raise JpegError("tj3Set(%d, %d) failed: %s" % (param, value, self._error(handle)))
            self._q_set = self.quality
        else:
            handle = self._init()
            if not handle:
                raise JpegError("tjInitCompress failed: %s" % self._error(None))
            self._handle = handle

    def _ensure_buffer(self, w, h):
        """A buffer of the worst-case JPEG size for w x h gray (re-made only when w x h changes)."""
        if self._dims == (w, h):
            return
        cap = int(self._bufsize(w, h, TJSAMP_GRAY))
        if not 0 < cap < 1 << 31:
            raise JpegError("JPEG buffer size for %dx%d unavailable: %s" % (w, h, self._error(None)))
        buf = self._alloc(cap)
        if not buf:
            raise JpegError("cannot allocate a %d-byte JPEG buffer" % cap)
        if self._buf:
            self._free(self._buf)
        self._buf, self._cap, self._dims = buf, cap, (w, h)

    # -- errors

    def _error(self, handle):
        try:
            if self.api == "tj3":
                return _text(self._errstr(handle))
            if self._errstr is not None:
                return _text(self._errstr(handle))
            if self._errstr_old is not None:
                return _text(self._errstr_old())
        except Exception as e:                 # the error path itself must not raise
            return "error text unavailable (%s)" % e
        return "unknown error"

    def _error_code(self):
        if self._errcode is None:              # TurboJPEG < 2.0: every -1 counts as fatal
            return TJERR_FATAL
        return self._errcode(self._handle)

    # -- encoding

    def encode_gray(self, pixels, w: int = WIDTH, h: int = HEIGHT, quality=None) -> bytes:
        """JPEG of w x h 8-bit gray pixels (bytes or bytearray, row by row). A TurboJPEG warning is
        accepted and counted; a fatal error raises JpegError with the library's message."""
        q = self.quality if quality is None else clamp_quality(quality)
        src = _source(pixels, w * h)
        with self._lock:
            if self._handle is None:
                raise JpegError("encoder is closed")
            self._ensure_buffer(w, h)
            self._size.value = self._cap       # NOREALLOC: buffer size, reset before every call
            self._bufptr.value = self._buf
            if self.api == "tj3":
                if q != self._q_set:
                    if self._set(self._handle, TJPARAM_QUALITY, q) != 0:
                        raise JpegError("tj3Set(QUALITY, %d) failed: %s" % (q, self._error(self._handle)))
                    self._q_set = q
                rc = self._compress(self._handle, src, w, 0, h, TJPF_GRAY, self._bufptr_p, self._size_p)
            else:
                rc = self._compress(self._handle, src, w, 0, h, TJPF_GRAY, self._bufptr_p, self._size_p,
                                    TJSAMP_GRAY, q, TJFLAG_NOREALLOC | TJFLAG_ACCURATEDCT)
            if rc != 0:
                if self._error_code() == TJERR_WARNING:
                    self.stats["jpeg_warn"] += 1
                else:
                    self.stats["jpeg_fatal"] += 1
                    self.last_error = self._error(self._handle)
                    raise JpegError("%s failed: %s" % ("tj3Compress8" if self.api == "tj3" else "tjCompress2",
                                                       self.last_error))
            n, addr = self._size.value, self._bufptr.value
            if not addr or not 0 < n <= self._cap:
                self.stats["jpeg_fatal"] += 1
                raise JpegError("TurboJPEG returned an invalid buffer (size %d)" % n)
            data = ctypes.string_at(addr, n)
            self.stats["encoded"] += 1
            return data

    def close(self) -> None:
        """Free the buffer and destroy the handle (idempotent)."""
        with self._lock:
            if self._buf:
                self._free(self._buf)
                self._buf = None
            if self._handle:
                self._destroy(self._handle)
                self._handle = None
            self._dims = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class StubJpegEncoder:
    """Tests only (Windows-native service runs): FF D8, a COM segment "FFSTUB seq=<n> len=<pixels>",
    FF D9. Passes validate_jpeg; not decodable."""
    api = "stub"
    name = "stub"

    def __init__(self, quality: int = QUALITY_DEFAULT):
        self.quality = clamp_quality(quality)
        self.stats = {"encoded": 0, "jpeg_warn": 0, "jpeg_fatal": 0}
        self.last_error = ""
        self._seq = 0
        self._lock = threading.Lock()

    @classmethod
    def load(cls, names=LIB_NAMES, quality: int = QUALITY_DEFAULT) -> "StubJpegEncoder":
        return cls(quality)

    def encode_gray(self, pixels, w: int = WIDTH, h: int = HEIGHT, quality=None) -> bytes:
        with self._lock:
            self._seq += 1
            self.stats["encoded"] += 1
            n = self._seq
        p = b"FFSTUB seq=%d len=%d" % (n, len(pixels))
        return b"\xff\xd8\xff\xfe" + struct.pack(">H", 2 + len(p)) + p + b"\xff\xd9"

    def close(self) -> None:
        pass


def validate_jpeg(data) -> bool:
    """4 <= len < 1 MiB, SOI (FF D8) first, and the first FF D9 is the last two bytes."""
    if isinstance(data, memoryview):
        data = data.tobytes()
    if not isinstance(data, (bytes, bytearray)):
        return False
    n = len(data)
    return 4 <= n < MAX_JPEG_BYTES and data[:2] == b"\xff\xd8" and data.find(b"\xff\xd9") == n - 2


class _Decoder:
    """Gray decompression with the tj3 or tj2 API; one handle per call."""

    def __init__(self, lib, api):
        self.api = api
        if api == "tj3":
            names = ("tj3Init", "tj3DecompressHeader", "tj3Get", "tj3Decompress8", "tj3Destroy",
                     "tj3GetErrorStr")
        elif api == "tj2":
            names = ("tjInitDecompress", "tjDecompressHeader3", "tjDecompress2", "tjDestroy")
        else:
            raise JpegError("unknown TurboJPEG API %r" % (api,))
        self._fn = {n: _symbol(lib, n) for n in names}
        self._errstr2 = _symbol(lib, "tjGetErrorStr2", required=False) if api == "tj2" else None

    def _why(self, handle):
        fn = self._fn.get("tj3GetErrorStr") or self._errstr2
        return _text(fn(handle)) if fn is not None else "unknown error"

    def decode(self, data) -> bytes:
        data = bytes(data)
        f = self._fn
        if self.api == "tj3":
            handle = f["tj3Init"](TJINIT_DECOMPRESS)
            if not handle:
                raise JpegError("tj3Init(DECOMPRESS) failed")
            try:
                if f["tj3DecompressHeader"](handle, data, len(data)) != 0:
                    raise JpegError("tj3DecompressHeader failed: %s" % self._why(handle))
                w, h = f["tj3Get"](handle, TJPARAM_JPEGWIDTH), f["tj3Get"](handle, TJPARAM_JPEGHEIGHT)
                dst = self._dst(w, h)
                if f["tj3Decompress8"](handle, data, len(data), dst, 0, TJPF_GRAY) != 0:
                    raise JpegError("tj3Decompress8 failed: %s" % self._why(handle))
            finally:
                f["tj3Destroy"](handle)
            return dst.raw
        handle = f["tjInitDecompress"]()
        if not handle:
            raise JpegError("tjInitDecompress failed")
        try:
            w, h, samp, space = (ctypes.c_int() for _ in range(4))
            if f["tjDecompressHeader3"](handle, data, len(data), ctypes.byref(w), ctypes.byref(h),
                                        ctypes.byref(samp), ctypes.byref(space)) != 0:
                raise JpegError("tjDecompressHeader3 failed: %s" % self._why(handle))
            dst = self._dst(w.value, h.value)
            if f["tjDecompress2"](handle, data, len(data), dst, w.value, 0, h.value, TJPF_GRAY, 0) != 0:
                raise JpegError("tjDecompress2 failed: %s" % self._why(handle))
        finally:
            f["tjDestroy"](handle)
        return dst.raw

    @staticmethod
    def _dst(w, h):
        if not (0 < w <= 4096 and 0 < h <= 4096):
            raise JpegError("unexpected JPEG size %dx%d" % (w, h))
        return ctypes.create_string_buffer(w * h)


_decoder_lock = threading.Lock()
_decoder_cache = []           # [] untried, [None] unavailable, [_Decoder]


def _default_decoder():
    with _decoder_lock:
        if not _decoder_cache:
            try:
                lib, _name, api = _open(LIB_NAMES)
                decoder = _Decoder(lib, api)
            except JpegError:
                decoder = None
            _decoder_cache.append(decoder)
        return _decoder_cache[0]


def decode_gray(data: bytes):
    """The gray pixels of a JPEG (w * h bytes, 160000 for the stream), None if no TurboJPEG decoder
    is available; JpegError if the data does not decode cleanly (a warning counts as a failure)."""
    decoder = _default_decoder()
    return None if decoder is None else decoder.decode(data)
