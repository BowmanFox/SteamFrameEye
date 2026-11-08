# SPDX-License-Identifier: MIT
"""BabaClient (spec §7.6): an MJPEG client that reads a stream the way Baballonia's "Wireless/IP
Camera" does (research §c.3, as documented): one plain HTTP GET; the body is read in 1024-byte
chunks into a 1 MiB frame buffer; it looks for FF D8 and copies until FF D9, ignoring boundaries,
part headers and Content-Length; a 5 s watchdog is re-armed before every read; a timeout, an error
or the end of the stream reconnects after 2 s. A JPEG counts as a frame if `decode` (optional)
returns without an exception, or, without a decoder, if it is marker-valid. wait_first_frame()
mirrors the 13 s first-frame loop of Baballonia's camera start.

Counters: frames, reconnects (connections after the first), bytes, keepalives (the "ka" parts seen
between frames; a diagnostic only, the parser ignores them), last_jpeg.
"""
import socket
import threading
import time
from urllib.parse import urlsplit

__all__ = ["BabaClient", "CHUNK", "BUFFER_BYTES", "WATCHDOG_S", "RECONNECT_S", "FIRST_FRAME_S"]

CHUNK = 1024
BUFFER_BYTES = 1 << 20
WATCHDOG_S = 5.0
RECONNECT_S = 2.0
FIRST_FRAME_S = 13.0
SOI, EOI = b"\xff\xd8", b"\xff\xd9"
_KA = b"\r\n\r\nka\r\n"


class BabaClient:
    def __init__(self, url: str, decode=None, connect_timeout: float = WATCHDOG_S):
        u = urlsplit(url)
        if u.scheme != "http" or not u.hostname:
            raise ValueError("BabaClient needs an http:// URL")
        self.host, self.port = u.hostname, u.port or 80
        self.path = (u.path or "/") + ("?" + u.query if u.query else "")
        self.decode = decode
        self.connect_timeout = connect_timeout
        self.frames = self.reconnects = self.bytes = self.keepalives = self.decode_errors = 0
        self.last_jpeg = None
        self.first_frame_t = None
        self.frame_times = []
        self.connected = False
        self._stop = threading.Event()
        self._first = threading.Event()
        self._cond = threading.Condition()
        self._sock = None
        self._thread = None
        self.started_t = None

    # -- control

    def start(self) -> "BabaClient":
        self.started_t = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="babaclient", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(WATCHDOG_S + 1.0)

    def wait_first_frame(self, timeout: float = FIRST_FRAME_S) -> bool:
        return self._first.wait(timeout)

    def wait_frames(self, n: int, timeout: float) -> bool:
        """True once `frames` reached n, False after `timeout` seconds."""
        end = time.monotonic() + timeout
        with self._cond:
            while self.frames < n:
                rest = end - time.monotonic()
                if rest <= 0:
                    return False
                self._cond.wait(rest)
            return True

    def frames_between(self, t0: float, t1: float) -> int:
        with self._cond:
            return sum(1 for t in self.frame_times if t0 <= t < t1)

    # -- the reader

    def _run(self):
        attempts = 0
        while not self._stop.is_set():
            if attempts:
                self.reconnects += 1
            attempts += 1
            try:
                self._session()
            except (OSError, ValueError):
                pass
            finally:
                self.connected = False
                sock, self._sock = self._sock, None
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if self._stop.wait(RECONNECT_S):
                return

    def _session(self):
        sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        self._sock = sock
        if self._stop.is_set():
            return
        host = "[%s]" % self.host if ":" in self.host else self.host      # the URL's authority, as a browser sends it
        sock.sendall(("GET %s HTTP/1.1\r\nHost: %s:%d\r\n\r\n" % (self.path, host, self.port)).encode("ascii"))
        self.connected = True
        pending = bytearray()
        in_frame = False
        while not self._stop.is_set():
            sock.settimeout(WATCHDOG_S)         # the watchdog is re-armed before every read
            chunk = sock.recv(CHUNK)
            if not chunk:
                return                          # the server closed the stream
            self.bytes += len(chunk)
            pending += chunk
            while True:
                if not in_frame:
                    k = pending.find(SOI)
                    if k < 0:
                        self.keepalives += pending.count(_KA)
                        tail = pending[-(len(_KA) - 1):]
                        del pending[:]
                        pending += tail         # a marker may straddle two chunks
                        break
                    self.keepalives += pending[:k].count(_KA)
                    del pending[:k]
                    in_frame = True
                e = pending.find(EOI, 2)
                if e < 0:
                    if len(pending) > BUFFER_BYTES:
                        del pending[:]          # longer than the frame buffer: dropped
                        in_frame = False
                    break
                frame = bytes(pending[:e + 2])
                del pending[:e + 2]
                in_frame = False
                self._frame(frame)

    def _frame(self, jpeg):
        if self.decode is not None:
            try:
                self.decode(jpeg)
            except Exception:                   # Baballonia drops undecodable frames silently
                self.decode_errors += 1
                return
        elif not (jpeg[:2] == SOI and jpeg[-2:] == EOI):
            return
        now = time.monotonic()
        with self._cond:
            self.frames += 1
            self.last_jpeg = jpeg
            self.frame_times.append(now)
            if len(self.frame_times) > 20000:
                del self.frame_times[:10000]
            if self.first_frame_t is None:
                self.first_frame_t = now
            self._cond.notify_all()
        self._first.set()
