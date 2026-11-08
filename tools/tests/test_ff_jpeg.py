# SPDX-License-Identifier: MIT
"""JPEG (spec §4.16; §11.3 test_ff_jpeg; task T3).

Everywhere: a mocked libturbojpeg proves the library search, tj3 selection, the tj2 fallback, the
warning-accepted / fatal-dropped rule and the NOREALLOC size reset; StubJpegEncoder and
validate_jpeg. Linux with libturbojpeg (container, require_native): the real tj3 path (and the real
tj2 API of the same library) writes SOI first, one EOI, JFIF APP0, and decodes the preprocessed
gradient frame at PSNR >= 35 dB in less than 40 KB at q92.
"""
import ffutil  # first: installs and self-checks the port guard before anything else

import ctypes
import math
import struct
import unittest
from unittest import mock

ffutil.setup_path()
from frameface import jpeg  # noqa: E402
from frameface import preprocess as pp  # noqa: E402

C = ctypes
SPEC_TJ3_COMPRESS8 = ((C.c_void_p, C.c_char_p, C.c_int, C.c_int, C.c_int, C.c_int, C.POINTER(C.c_void_p),
                       C.POINTER(C.c_size_t)), C.c_int)
SPEC_TJ2_COMPRESS2 = ((C.c_void_p, C.c_char_p, C.c_int, C.c_int, C.c_int, C.c_int, C.POINTER(C.c_void_p),
                       C.POINTER(C.c_ulong), C.c_int, C.c_int, C.c_int), C.c_int)


class FakeFn:
    """A library symbol: callable, with settable argtypes/restype like a ctypes function."""

    def __init__(self, name, impl, log):
        self.name, self.impl, self.log = name, impl, log
        self.argtypes = self.restype = None

    def __call__(self, *args):
        self.log.append((self.name, args))
        return self.impl(*args)


class FakeTurbo:
    """libturbojpeg stand-in. Compression writes a small marker-valid JPEG into the caller's
    buffer, records the incoming *jpegSize and buffer address, and returns what `script` says:
    a list of (rc, error code, message) consumed per compress call, (0, 0, "") when empty."""
    HANDLE, CAP3, CAP2 = 0x5150, 71234, 65432
    TJ3 = ("tj3Init", "tj3Set", "tj3JPEGBufSize", "tj3Alloc", "tj3Free", "tj3Destroy", "tj3Compress8",
           "tj3GetErrorCode", "tj3GetErrorStr")
    TJ2 = ("tjInitCompress", "tjBufSize", "tjAlloc", "tjFree", "tjDestroy", "tjCompress2",
           "tjGetErrorCode", "tjGetErrorStr2")

    def __init__(self, tj3=True, tj2=True, omit=(), init_handle=HANDLE):
        self.log, self.script, self.sizes_in, self.bufs_in, self.srcs = [], [], [], [], []
        self.buffers, self.code, self.message = {}, 0, ""
        self.init_handle = init_handle
        for name in (self.TJ3 if tj3 else ()) + (self.TJ2 if tj2 else ()):
            if name not in omit:
                setattr(self, name, FakeFn(name, getattr(self, "_" + name), self.log))

    def calls(self, name):
        return [args for n, args in self.log if n == name]

    # -- allocation
    def _alloc(self, n):
        buf = ctypes.create_string_buffer(int(n))
        self.buffers[ctypes.addressof(buf)] = buf
        return ctypes.addressof(buf)

    def _free(self, addr):
        self.buffers.pop(addr)

    # -- compression shared by both APIs
    def _compress(self, src, w, pitch, h, pf, pbuf, psize):
        self.sizes_in.append(psize.contents.value)
        self.bufs_in.append(pbuf.contents.value)
        self.srcs.append(bytes(src))
        n = len(self.sizes_in)
        data = b"\xff\xd8" + b"FAKE%04d" % n + b"\xff\xd9"
        ctypes.memmove(pbuf.contents.value, data, len(data))
        psize.contents.value = len(data)      # what NOREALLOC leaves behind
        rc, self.code, self.message = self.script.pop(0) if self.script else (0, 0, "")
        return rc

    def _tj3Init(self, init_type):
        return self.init_handle

    def _tj3Set(self, h, param, value):
        return 0

    def _tj3JPEGBufSize(self, w, h, samp):
        return self.CAP3

    def _tj3Alloc(self, n):
        return self._alloc(n)

    def _tj3Free(self, addr):
        self._free(addr)

    def _tj3Destroy(self, h):
        return None

    def _tj3Compress8(self, h, src, w, pitch, hh, pf, pbuf, psize):
        return self._compress(src, w, pitch, hh, pf, pbuf, psize)

    def _tj3GetErrorCode(self, h):
        return self.code

    def _tj3GetErrorStr(self, h):
        return self.message.encode()

    def _tjInitCompress(self):
        return self.init_handle

    def _tjBufSize(self, w, h, samp):
        return self.CAP2

    def _tjAlloc(self, n):
        return self._alloc(n)

    def _tjFree(self, addr):
        self._free(addr)

    def _tjDestroy(self, h):
        return 0

    def _tjCompress2(self, h, src, w, pitch, hh, pf, pbuf, psize, samp, q, flags):
        return self._compress(src, w, pitch, hh, pf, pbuf, psize)

    def _tjGetErrorCode(self, h):
        return self.code

    def _tjGetErrorStr2(self, h):
        return self.message.encode()


def fake_dlopen(libs):
    def dlopen(name):
        if name in libs:
            return libs[name]
        raise OSError("%s: cannot open shared object file" % name)
    return dlopen


class MockedLibraryTest(unittest.TestCase):
    def load(self, libs, found=None, **kw):
        with mock.patch.object(jpeg, "_dlopen", side_effect=fake_dlopen(libs)) as dl, \
                mock.patch.object(jpeg, "_find_library", return_value=found) as fl:
            enc = jpeg.JpegEncoder.load(**kw)
        self.addCleanup(enc.close)
        return enc, dl, fl

    def test_tj3_selected_and_set_up(self):
        lib = FakeTurbo()
        enc, dl, fl = self.load({"libturbojpeg.so.0": lib})
        self.assertEqual((enc.api, enc.name, enc.quality), ("tj3", "libturbojpeg.so.0", 92))
        dl.assert_called_once_with("libturbojpeg.so.0")
        fl.assert_not_called()                                 # asked only when the names fail
        self.assertEqual(lib.calls("tj3Init"), [(0,)])
        self.assertEqual(lib.calls("tj3Set"), [(lib.HANDLE, 3, 92), (lib.HANDLE, 4, 3), (lib.HANDLE, 2, 1),
                                               (lib.HANDLE, 10, 0)])   # QUALITY, SUBSAMP GRAY, NOREALLOC, FASTDCT off
        self.assertEqual(lib.calls("tj3JPEGBufSize"), [(400, 400, 3)])
        self.assertEqual(lib.calls("tj3Alloc"), [(lib.CAP3,)])
        self.assertEqual((lib.tj3Compress8.argtypes, lib.tj3Compress8.restype), SPEC_TJ3_COMPRESS8)
        self.assertEqual(lib.tj3Init.restype, C.c_void_p)
        self.assertEqual(lib.tj3JPEGBufSize.restype, C.c_size_t)
        px = bytes(range(256)) * 625
        data = enc.encode_gray(px)
        self.assertEqual(data, b"\xff\xd8FAKE0001\xff\xd9")
        (h, src, w, pitch, hh, pf, _, _), = lib.calls("tj3Compress8")
        self.assertEqual((h, src, w, pitch, hh, pf), (lib.HANDLE, px, 400, 0, 400, 6))
        self.assertFalse(lib.calls("tjCompress2"))

    def test_search_order_and_find_library(self):
        tj2_only = FakeTurbo(tj3=False)
        enc, dl, fl = self.load({"libturbojpeg.so": tj2_only})
        self.assertEqual((enc.api, enc.name), ("tj2", "libturbojpeg.so"))
        self.assertEqual([c.args[0] for c in dl.call_args_list], ["libturbojpeg.so.0", "libturbojpeg.so"])
        enc, dl, fl = self.load({"/usr/lib/libturbojpeg.so.0.3.0": FakeTurbo()}, found="/usr/lib/libturbojpeg.so.0.3.0")
        self.assertEqual((enc.api, enc.name), ("tj3", "/usr/lib/libturbojpeg.so.0.3.0"))
        fl.assert_called_once_with()
        # a library with neither API is passed over
        enc, dl, fl = self.load({"libturbojpeg.so.0": FakeTurbo(tj3=False, tj2=False), "libturbojpeg.so": FakeTurbo()})
        self.assertEqual(enc.name, "libturbojpeg.so")

    def test_no_library_raises_jpegerror(self):
        with mock.patch.object(jpeg, "_dlopen", side_effect=fake_dlopen({})), \
                mock.patch.object(jpeg, "_find_library", return_value=None):
            with self.assertRaises(jpeg.JpegError) as cm:
                jpeg.JpegEncoder.load()
        self.assertIn("libturbojpeg.so.0", str(cm.exception))
        self.assertIn("libturbojpeg.so:", str(cm.exception))
        with mock.patch.object(jpeg, "_dlopen", side_effect=fake_dlopen({"x.so": FakeTurbo(tj3=False, tj2=False)})), \
                mock.patch.object(jpeg, "_find_library", return_value=None):
            with self.assertRaises(jpeg.JpegError):
                jpeg.JpegEncoder.load(names=("x.so",))

    def test_tj2_fallback(self):
        lib = FakeTurbo(tj3=False)
        enc, _, _ = self.load({"libturbojpeg.so.0": lib}, quality=95)
        self.assertEqual(enc.api, "tj2")
        self.assertEqual(lib.calls("tjInitCompress"), [()])
        self.assertEqual(lib.calls("tjBufSize"), [(400, 400, 3)])
        self.assertEqual(lib.calls("tjAlloc"), [(lib.CAP2,)])
        self.assertEqual((lib.tjCompress2.argtypes, lib.tjCompress2.restype), SPEC_TJ2_COMPRESS2)
        self.assertEqual(lib.tjBufSize.restype, C.c_ulong)
        px = bytearray(160000)
        self.assertEqual(enc.encode_gray(px), b"\xff\xd8FAKE0001\xff\xd9")
        (h, src, w, pitch, hh, pf, _, _, samp, q, flags), = lib.calls("tjCompress2")
        self.assertEqual((h, w, pitch, hh, pf, samp, q, flags), (lib.HANDLE, 400, 0, 400, 6, 3, 95, 1024 | 4096))
        self.assertEqual(lib.srcs, [bytes(px)])                # a bytearray is passed without a copy
        self.assertIsInstance(src, ctypes.Array)

    def test_warning_accepted_fatal_dropped(self):
        for api in ("tj3", "tj2"):
            with self.subTest(api=api):
                lib = FakeTurbo(tj3=api == "tj3")
                enc, _, _ = self.load({"libturbojpeg.so.0": lib})
                lib.script = [(-1, 0, "warning: something odd"), (-1, 1, "fatal: broken"), (0, 0, "")]
                px = bytes(160000)
                self.assertEqual(enc.encode_gray(px), b"\xff\xd8FAKE0001\xff\xd9")    # warning: accepted
                self.assertEqual(enc.stats["jpeg_warn"], 1)
                with self.assertRaises(jpeg.JpegError) as cm:                         # fatal: dropped
                    enc.encode_gray(px)
                self.assertIn("fatal: broken", str(cm.exception))
                self.assertEqual((enc.stats["jpeg_fatal"], enc.last_error), (1, "fatal: broken"))
                self.assertEqual(enc.encode_gray(px), b"\xff\xd8FAKE0003\xff\xd9")    # and it goes on
                self.assertEqual(enc.stats["encoded"], 2)

    def test_tj2_without_error_code_counts_every_failure_as_fatal(self):
        lib = FakeTurbo(tj3=False, omit=("tjGetErrorCode",))
        enc, _, _ = self.load({"libturbojpeg.so.0": lib})
        lib.script = [(-1, 0, "maybe only a warning")]
        with self.assertRaises(jpeg.JpegError) as cm:
            enc.encode_gray(bytes(160000))
        self.assertIn("maybe only a warning", str(cm.exception))

    def test_norealloc_size_reset(self):
        for api, cap in (("tj3", FakeTurbo.CAP3), ("tj2", FakeTurbo.CAP2)):
            with self.subTest(api=api):
                lib = FakeTurbo(tj3=api == "tj3")
                enc, _, _ = self.load({"libturbojpeg.so.0": lib})
                for _ in range(3):
                    enc.encode_gray(bytes(160000))
                # the fake shrinks *jpegSize to 12 after every call; each call must see the capacity again
                self.assertEqual(lib.sizes_in, [cap, cap, cap])
                (buf_addr,) = lib.buffers                       # the one preallocated buffer, every time
                self.assertEqual(lib.bufs_in, [buf_addr] * 3)

    def test_quality_floor_and_per_call_quality(self):
        lib = FakeTurbo()
        enc, _, _ = self.load({"libturbojpeg.so.0": lib}, quality=80)
        self.assertEqual(enc.quality, 90)                       # raised to the floor
        self.assertIn((lib.HANDLE, 3, 90), lib.calls("tj3Set"))
        del lib.log[:]
        enc.encode_gray(bytes(160000), quality=95)
        enc.encode_gray(bytes(160000), quality=95)
        enc.encode_gray(bytes(160000), quality=120)
        enc.encode_gray(bytes(160000))
        self.assertEqual([a for a in lib.calls("tj3Set") if a[1] == 3],
                         [(lib.HANDLE, 3, 95), (lib.HANDLE, 3, 100), (lib.HANDLE, 3, 90)])
        self.assertEqual([jpeg.clamp_quality(q) for q in (0, 89, 90, 92, 100, 101)], [90, 90, 90, 92, 100, 100])

    def test_bad_input_and_close(self):
        lib = FakeTurbo()
        enc, _, _ = self.load({"libturbojpeg.so.0": lib})
        with self.assertRaises(jpeg.JpegError):
            enc.encode_gray(bytes(1000))
        self.assertFalse(lib.calls("tj3Compress8"))
        (buf_addr,) = lib.buffers
        enc.close()
        enc.close()
        self.assertEqual(lib.calls("tj3Free"), [(buf_addr,)])
        self.assertEqual(lib.calls("tj3Destroy"), [(lib.HANDLE,)])
        with self.assertRaises(jpeg.JpegError):
            enc.encode_gray(bytes(160000))

    def test_init_failure(self):
        for api in ("tj3", "tj2"):
            with self.subTest(api=api):
                lib = FakeTurbo(tj3=api == "tj3", init_handle=None)
                lib.message = "no memory"
                with mock.patch.object(jpeg, "_dlopen", side_effect=fake_dlopen({"libturbojpeg.so.0": lib})):
                    with self.assertRaises(jpeg.JpegError) as cm:
                        jpeg.JpegEncoder.load()
                self.assertIn("no memory", str(cm.exception))
                self.assertFalse(lib.buffers)

    def test_decode_gray_without_library(self):
        with mock.patch.object(jpeg, "_dlopen", side_effect=fake_dlopen({})), \
                mock.patch.object(jpeg, "_find_library", return_value=None), \
                mock.patch.object(jpeg, "_decoder_cache", []):
            self.assertIsNone(jpeg.decode_gray(b"\xff\xd8\xff\xd9"))


class StubAndValidationTest(unittest.TestCase):
    def test_stub_encoder(self):
        stub = jpeg.StubJpegEncoder()
        self.assertEqual((stub.api, stub.quality), ("stub", 92))
        px = bytes(160000)
        for seq in (1, 2):
            p = b"FFSTUB seq=%d len=160000" % seq
            data = stub.encode_gray(px)
            self.assertEqual(data, b"\xff\xd8\xff\xfe" + struct.pack(">H", 2 + len(p)) + p + b"\xff\xd9")
            self.assertTrue(jpeg.validate_jpeg(data))
        self.assertTrue(jpeg.validate_jpeg(stub.encode_gray(b"\x01" * 5)))
        stub.close()
        self.assertIsInstance(jpeg.StubJpegEncoder.load(), jpeg.StubJpegEncoder)

    def test_validate_jpeg(self):
        good = b"\xff\xd8" + b"\x00" * 100 + b"\xff\xd9"
        self.assertTrue(jpeg.validate_jpeg(good))
        self.assertTrue(jpeg.validate_jpeg(bytearray(good)))
        self.assertTrue(jpeg.validate_jpeg(memoryview(good)))
        self.assertTrue(jpeg.validate_jpeg(b"\xff\xd8\xff\xd9"))                  # 4 bytes: the minimum
        big_ok = b"\xff\xd8" + b"\x00" * (jpeg.MAX_JPEG_BYTES - 5) + b"\xff\xd9"
        self.assertEqual(len(big_ok), (1 << 20) - 1)
        self.assertTrue(jpeg.validate_jpeg(big_ok))
        bad = {
            "missing SOI": b"\x00\xd8" + b"\x00" * 10 + b"\xff\xd9",
            "SOI not first": b"\x00\xff\xd8" + b"\x00" * 10 + b"\xff\xd9",
            "early EOI": b"\xff\xd8\x00\xff\xd9\x00\x00\xff\xd9",
            "no EOI": b"\xff\xd8" + b"\x00" * 10,
            "bytes after EOI": good + b"\x00",
            "1 MiB": b"\xff\xd8" + b"\x00" * (jpeg.MAX_JPEG_BYTES - 4) + b"\xff\xd9",
            "larger": b"\xff\xd8" + b"\x00" * jpeg.MAX_JPEG_BYTES + b"\xff\xd9",
            "too short": b"\xff\xd8\xd9",
            "empty": b"",
        }
        self.assertEqual(len(bad["1 MiB"]), 1 << 20)
        for why, data in bad.items():
            with self.subTest(why=why):
                self.assertFalse(jpeg.validate_jpeg(data))
        for other in (None, "\xff\xd8\xff\xd9", 1234):
            self.assertFalse(jpeg.validate_jpeg(other))


def psnr(a, b):
    mse = sum((x - y) * (x - y) for x, y in zip(a, b)) / len(a)
    return float("inf") if mse == 0 else 10 * math.log10(255 * 255 / mse)


def sof_size(data):
    """(height, width, components) from the first SOF0..SOF2 segment."""
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            raise AssertionError("marker expected at %d" % i)
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            return struct.unpack(">HHB", data[i + 5:i + 10])
        i += 2 + length
    raise AssertionError("no SOF segment")


class RealLibraryTest(unittest.TestCase):
    """libturbojpeg on Linux (the container has 3.0.x, so tj3; CI Ubuntu 2.1.x would give tj2)."""

    @classmethod
    def setUpClass(cls):
        ffutil.require_native("turbojpeg", reason="real libturbojpeg")
        cls.lib, cls.libname, cls.api = jpeg._open(jpeg.LIB_NAMES)
        gradient = bytes(((i // 800) * 7 + (i % 800) * 3) & 0xFF for i in range(pp.FRAME_BYTES))
        cls.px = pp.preprocess_swar(gradient, 1, 0)                 # the preprocessed gradient frame

    def check_stream(self, data):
        self.assertTrue(jpeg.validate_jpeg(data))
        self.assertEqual(data[:2], b"\xff\xd8")                       # SOI first
        self.assertEqual(data.count(b"\xff\xd9"), 1)                  # a single EOI ...
        self.assertTrue(data.endswith(b"\xff\xd9"))                   # ... at the end
        self.assertEqual(data[2:4], b"\xff\xe0")                      # JFIF APP0 right after SOI
        self.assertEqual(data[6:11], b"JFIF\x00")
        self.assertNotIn(b"\xff\xe1", data[:data.find(b"\xff\xda")])  # no EXIF/APP1 in the headers
        self.assertEqual(sof_size(data), (400, 400, 1))               # 400 x 400, one component (gray)

    def test_tj3_path(self):
        enc = jpeg.JpegEncoder.load()
        self.addCleanup(enc.close)
        if hasattr(self.lib, "tj3Init"):
            self.assertEqual(enc.api, "tj3")
        data = enc.encode_gray(self.px)
        self.check_stream(data)
        self.assertLess(len(data), 40 * 1024)                         # q92 < 40 KB
        out = jpeg.decode_gray(data)
        self.assertEqual(len(out), 400 * 400)
        value = psnr(self.px, out)
        print("\n%s q92: %d bytes, PSNR %.2f dB" % (enc.api, len(data), value))
        self.assertGreaterEqual(value, 35.0)
        self.assertEqual(enc.stats, {"encoded": 1, "jpeg_warn": 0, "jpeg_fatal": 0})
        self.assertLess(len(enc.encode_gray(self.px, quality=90)), len(enc.encode_gray(self.px, quality=100)))
        self.assertEqual(enc.encode_gray(bytearray(self.px)), data)

    def test_tj2_api_of_the_same_library(self):
        self.assertTrue(hasattr(self.lib, "tjInitCompress"), "libturbojpeg without the tj2 API")
        enc = jpeg.JpegEncoder(self.lib, "tj2", 92, self.libname)
        self.addCleanup(enc.close)
        data = enc.encode_gray(self.px)
        self.check_stream(data)
        out = jpeg._Decoder(self.lib, "tj2").decode(data)
        self.assertGreaterEqual(psnr(self.px, out), 35.0)
        self.assertEqual(jpeg.decode_gray(data), out)

    def test_placeholder_and_bad_streams(self):
        enc = jpeg.JpegEncoder.load()
        self.addCleanup(enc.close)
        black = enc.encode_gray(bytes(160000))                        # the §4.17 placeholder image
        self.check_stream(black)
        self.assertEqual(set(jpeg.decode_gray(black)), {0})
        with self.assertRaises(jpeg.JpegError):                       # the stub is not decodable
            jpeg.decode_gray(jpeg.StubJpegEncoder().encode_gray(self.px))
        with self.assertRaises(jpeg.JpegError):                       # truncated: a warning is a failure
            jpeg.decode_gray(black[:len(black) // 2] + b"\xff\xd9")


if __name__ == "__main__":
    unittest.main()
