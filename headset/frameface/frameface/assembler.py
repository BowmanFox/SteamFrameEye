# SPDX-License-Identifier: MIT
"""Bulk payload reassembly, frame assembly, stride learning and the frame pool (spec §4.10).

Wire format: every payload starts with a 12-byte header (0x0C, bmHeaderInfo, PTS, SCR); a
payload ends after max_payload bytes (the negotiated dwMaxPayloadTransferSize) or with a short
transfer, so one URB may hold several payloads or one payload span several URBs (the uvcvideo
bulk rule, generalised). A frame ends with EOF, a FID toggle or (devices without EOF) when it
reaches its size; it is published only if it is exactly frame_bytes long and had no error.

Threads: the reaper feeds BulkReassembler -> FrameAssembler -> FramePool.publish; the pixel thread
calls FramePool.take. FramePool synchronises the two with two `_thread` locks (a mutex and a
"frame ready" signal), so this pure module needs no `threading` import; it starts no thread.
"""
import _thread
import collections
from dataclasses import dataclass

from .uvc import EOF, parse_header

__all__ = ["FrameInfo", "FpsMeter", "FramePool", "FrameAssembler", "BulkReassembler", "StrideLearner", "STAT_KEYS"]

STAT_KEYS = ("frames_ok", "bad_size", "bad_err", "bad_hdr", "overflow", "fid_breaks", "unexpected_size_614400",
             "dropped_busy", "stride_learned")
SIZE_614400 = 614400                    # a 640x480 YUYV frame: counted, never published
LENIENT_AFTER_S, LENIENT_MIN_STARTS = 1.0, 20
LEARN_AFTER_S = 1.0                     # the stride learner runs while frames fail this long with bytes flowing
LEARN_FRAMES = 3                        # frames (distinct PTS) that must show the same stride
HEADER_LEN = 12


def _byte_view(mv):
    """A 'B'-format view of `mv` (ctypes buffers export '<B', which slice assignment rejects)."""
    if isinstance(mv, memoryview) and mv.format != "B":
        return mv.cast("B")
    return mv


@dataclass
class FrameInfo:
    seq: int
    t_mono: float
    pts: "int | None"
    size: int
    why: str                            # "eof" | "fid" | "size"


class FpsMeter:
    """Frame rates of accepted frames (§4.10): fps_in = 1 / EWMA of the arrival intervals (alpha 0.1);
    fps_pts = clock_hz / median PTS step (modulo 2^32) over the last 60 frames (None before two)."""

    def __init__(self, clock_hz: int = 150_000_000, window: int = 60, alpha: float = 0.1):
        self.clock_hz = clock_hz
        self.alpha = alpha
        self._steps = collections.deque(maxlen=window)
        self._t = self._pts = self._dt = None

    def add(self, t_mono: float, pts: "int | None") -> None:
        if self._t is not None and t_mono > self._t:
            dt = t_mono - self._t
            self._dt = dt if self._dt is None else self._dt + self.alpha * (dt - self._dt)
        self._t = t_mono
        if pts is not None:
            if self._pts is not None and (pts - self._pts) & 0xFFFFFFFF:
                self._steps.append((pts - self._pts) & 0xFFFFFFFF)
            self._pts = pts

    @property
    def fps_in(self) -> float:
        return 1.0 / self._dt if self._dt else 0.0

    @property
    def fps_pts(self) -> "float | None":
        if not self._steps:
            return None
        s = sorted(self._steps)
        mid = len(s) // 2
        median = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
        return self.clock_hz / median


class FramePool:
    """Triple buffer: the assembler writes the fill buffer, publish() makes it the ready frame (an
    unconsumed ready frame is dropped and counted in stats["dropped_busy"]), take() hands the
    newest ready frame to the pixel thread as its process buffer. No allocation after __init__."""

    def __init__(self, frame_bytes: int, n: int = 3):
        if n < 3:
            raise ValueError("a FramePool needs at least 3 buffers")
        self.frame_bytes = frame_bytes
        self.buffers = tuple(bytearray(frame_bytes) for _ in range(n))
        self._views = tuple(memoryview(b) for b in self.buffers)
        self._info = [None] * n
        self._fill, self._process, self._ready = 0, 1, None
        self._free = list(range(2, n))
        self._mutex = _thread.allocate_lock()
        self._signal = _thread.allocate_lock()  # held while no frame is ready
        self._signal.acquire()
        self.stats = collections.Counter({"dropped_busy": 0, "published": 0, "taken": 0})

    def fill(self) -> memoryview:
        return self._views[self._fill]

    def publish(self, info: FrameInfo) -> None:
        with self._mutex:
            self._info[self._fill] = info
            first = self._ready is None
            if not first:
                self.stats["dropped_busy"] += 1
                self._free.append(self._ready)
            self._ready = self._fill
            self._fill = self._free.pop()
            self.stats["published"] += 1
            if first:
                self._signal.release()

    def take(self, timeout: float):
        """(process buffer, FrameInfo) of the newest frame, waiting up to `timeout` seconds
        (<= 0: do not wait; None: wait forever), or None. The buffer stays the caller's until
        its next take()."""
        if timeout is None:
            got = self._signal.acquire()
        elif timeout <= 0:
            got = self._signal.acquire(False)
        else:
            got = self._signal.acquire(True, min(float(timeout), _thread.TIMEOUT_MAX))
        if not got:
            return None
        with self._mutex:
            ready = self._ready
            self._free.append(self._process)
            self._process, self._ready = ready, None
            self.stats["taken"] += 1
            return self.buffers[ready], self._info[ready]


class FrameAssembler:
    """Payload -> frame state machine. The reassembler calls begin() at each payload start,
    data() for its image bytes and end() when the payload ends; the driver calls error() for a
    URB transfer error. `on_event(name, kv)`, if set, is called for "lenient_headers" and
    "stride_learned" (kv {"stride": n})."""

    def __init__(self, frame_bytes: int, pool: FramePool, clock, strict: bool = True):
        self.frame_bytes = frame_bytes
        self.pool = pool
        self.clock = clock
        self.mode = "strict" if strict else "lenient"
        self.stats = collections.Counter({k: 0 for k in STAT_KEYS})
        self.seq = 0
        self.last_ok_t = None
        self.fps = FpsMeter()               # fps_in / fps_pts of the accepted frames
        self.eof_seen = False               # the device ends frames with EOF (else: by size or FID)
        self.on_event = None
        self._mv = pool.fill()
        self._pos = self._total = 0         # bytes stored / bytes received (incl. overflow)
        self._err = self._started = self._overflowed = False
        self._fid, self._pts = -1, None
        self._rej_t, self._rej_n = None, 0
        self._fail_t = self._bytes_t = None

    def _event(self, name, **kv):
        if self.on_event is not None:
            self.on_event(name, kv)

    def begin(self, mv, off: int, n: int) -> int:
        """Validate the header at mv[off] (n bytes left in the payload): its length, or -1."""
        h = parse_header(_byte_view(mv), off, n, self.mode == "strict")
        if h is None:
            self.stats["bad_hdr"] += 1
            self._err = True
            now = self.clock.monotonic()
            if self._rej_t is None:
                self._rej_t, self._rej_n = now, 0
            self._rej_n += 1
            if self.mode == "strict" and self._rej_n >= LENIENT_MIN_STARTS and now - self._rej_t >= LENIENT_AFTER_S:
                self.mode = "lenient"
                self._event("lenient_headers")
            return -1
        self._rej_t = None
        if h.fid != self._fid:
            if self._total > 0:
                self.stats["fid_breaks"] += 1
                self._finish("fid")
            else:
                self._started = False           # header-only payloads so far: the new frame's PTS counts
            self._fid = h.fid
        if not self._started:
            self._started = True
            self._pts = h.pts
        if h.err:
            self._err = True
        return h.length

    def data(self, mv, off: int, n: int) -> None:
        if n <= 0:
            return
        pos = self._pos
        self._total += n
        room = self.frame_bytes - pos
        if n > room:
            if not self._overflowed:
                self._overflowed = True
                self.stats["overflow"] += 1
            n = room
            if n <= 0:
                return
        self._mv[pos:pos + n] = _byte_view(mv)[off:off + n]
        self._pos = pos + n

    def end(self, flags: int) -> None:
        """A payload ended: EOF finishes the frame; a device that has not sent EOF yet (EOF-less
        devices) finishes it when it is full ("size"). Once EOF was seen, an oversized frame keeps
        counting until its EOF, so a 614400-byte frame is recognised as such."""
        if flags & EOF:
            self.eof_seen = True
            if self._total > 0:
                self._finish("eof")
        elif not self.eof_seen and self._total >= self.frame_bytes:
            self._finish("size")

    def error(self) -> None:
        """A transfer error: the frame in progress is bad."""
        self._err = True

    def _finish(self, why):
        total = self._total
        if total == SIZE_614400:
            self.stats["unexpected_size_614400"] += 1
        elif self._err:
            self.stats["bad_err"] += 1
        elif total != self.frame_bytes:
            self.stats["bad_size"] += 1
        else:
            self.seq += 1
            now = self.clock.monotonic()
            self.pool.publish(FrameInfo(self.seq, now, self._pts, total, why))
            self._mv = self.pool.fill()
            self.stats["frames_ok"] += 1
            self.stats["dropped_busy"] = self.pool.stats["dropped_busy"]
            self.last_ok_t = now
            self.fps.add(now, self._pts)
            self._fail_t = None
        self._pos = self._total = 0
        self._err = self._started = self._overflowed = False
        self._pts = None

    def failing_s(self, now: float) -> float:
        """Seconds that bytes have been flowing without an accepted frame (0.0 if not failing)."""
        return 0.0 if self._fail_t is None else now - self._fail_t

    def _note_bytes(self, now):
        if self._fail_t is None or (self._bytes_t is not None and now - self._bytes_t > LEARN_AFTER_S):
            self._fail_t = now              # failing starts (again) with the first bytes after a frame or a gap
        self._bytes_t = now


class StrideLearner:
    """Learns the real payload stride from the wire: at every packet boundary of a URB it looks for
    a strict header with the same FID and the same PTS as the last header of the current frame;
    the distance to that header is a candidate. A candidate seen in LEARN_FRAMES frames with
    distinct PTS is returned once (then the learner starts over). Headers with a new FID and a new
    PTS start a frame; other header-like bytes are ignored."""

    def __init__(self, packet: int = 512):
        self.packet = packet
        self.reset()

    def reset(self) -> None:
        self._abs = 0
        self._fid = self._pts = self._last = None
        self._cands = {}

    def observe(self, mv, n: int, urb_len: int) -> "int | None":
        mv = _byte_view(mv)
        base = self._abs
        self._abs += n
        for k in range(0, n, self.packet):
            if mv[k] != HEADER_LEN:
                continue
            h = parse_header(mv, k, n - k, True)
            if h is None:
                continue
            pos = base + k
            if self._pts is not None and h.fid == self._fid and h.pts == self._pts:
                stride, self._last = pos - self._last, pos
                if stride > 0:
                    frames = self._cands.setdefault(stride, set())
                    frames.add(h.pts)
                    if len(frames) >= LEARN_FRAMES:
                        self.reset()
                        return stride
            elif h.fid != self._fid and h.pts != self._pts:
                self._fid, self._pts, self._last = h.fid, h.pts, pos
        return None


class BulkReassembler:
    """Cuts bulk URBs into payloads (spec §4.10): a payload ends after max_payload bytes, at a short
    URB or at a zero-length URB. While frames fail with bytes flowing for LEARN_AFTER_S, the
    StrideLearner watches the URBs and may replace max_payload (`learning` tells the service)."""

    def __init__(self, asm: FrameAssembler, max_payload: int, packet: int = 512):
        if max_payload <= 0 or packet <= 0:
            raise ValueError("max_payload and packet must be positive")
        self.asm = asm
        self.max_payload = max_payload
        self.packet = packet
        self.learner = StrideLearner(packet)
        self.learning = False
        self._open = self._skip = False
        self._in = self._flags = 0

    def feed(self, mv, n: int, urb_len: int) -> None:
        if n == 0:                                  # zero-length packet: ends an open payload
            if self._open:
                self._end()
            return
        mv = _byte_view(mv)
        asm = self.asm
        now = asm.clock.monotonic()
        asm._note_bytes(now)
        if asm.failing_s(now) >= LEARN_AFTER_S:
            self.learning = True
            stride = self.learner.observe(mv, n, urb_len)
            if stride is not None and stride != self.max_payload:
                self.max_payload = stride
                asm.stats["stride_learned"] += 1
                asm._event("stride_learned", stride=stride)
        elif self.learning:
            self.learning = False
            self.learner.reset()
        off = 0
        while off < n:
            hl = 0
            if not self._open:
                hl = asm.begin(mv, off, n - off)
                self._open, self._in = True, 0
                self._skip = hl < 0
                if self._skip:
                    hl = 0
                else:
                    self._flags = mv[off + 1]
            take = self.max_payload - self._in
            if take <= 0:                           # the stride shrank under an open payload
                self._end()
                continue
            if take > n - off:
                take = n - off
            if not self._skip and take > hl:
                asm.data(mv, off + hl, take - hl)
            self._in += take
            off += take
            if self._in >= self.max_payload:
                self._end()
        if n < urb_len and self._open:              # short transfer
            self._end()

    def _end(self):
        if not self._skip:
            self.asm.end(self._flags)
        self._open = self._skip = False
        self._in = 0
