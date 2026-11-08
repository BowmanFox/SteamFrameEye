# SPDX-License-Identifier: MIT
"""FakeVft (spec §7.2): the tracker as the fake kernel sees it. Every disputed fact is a VftModel
knob whose comment names its source; the defaults are the HS persona the spec expects.

XU: SET_CUR stores the command; GET_CUR answers 0x55 (busy) ack_busy_polls times (and for ack_delay_s
after the SET_CUR), then 0x56 + the command's first 16 bytes + the register value at byte 17.
Cadence: MAGICs are counted since the last STREAM_ON or since the fd was opened; REG_WRITEs received
after MAGIC number magic_needed-1 are buffered and latch when MAGIC number magic_needed arrives (only
at the programming length, and never during the settle window or while wedged); buffered writes are
discarded at close. Block C sends its 11 OFF writes after its second MAGIC, so they latch only with
teardown_magic3.

Stream: it starts at the first URB after a COMMIT when STREAM_ON was received, the setif and
xu_between rules hold and the URB came within start_deadline_ms; a broken setif rule gives test
bars, the others nothing. Timing is analytic: frame k's wire bytes leave at a constant rate over
90 % of the frame interval and one URB completes when its last byte arrives (or at the payload's
short packet), so there is exactly one completion event per URB, never one per packet.
"""
import bisect
from dataclasses import dataclass
import errno
import itertools
import struct

from .. import uvc
from ..descriptors import build_expected
from .scenes import ANIMATED, FRAME_BYTES, PHASES, make_scene_frame

__all__ = ["VftModel", "FakeVft", "STREAM_RATE_SHARE", "CLOCK_HZ"]

STREAM_RATE_SHARE = 0.9                 # a frame's bytes leave within 90 % of the frame interval
CLOCK_HZ = 150_000_000                  # dwClockFrequency of the device (Appendix E)
_BURST_S = 0.15                         # length of an EPROTO burst (> 50 errors at 60 fps)


@dataclass
class VftModel:
    persona: str = "hs"                 # "hs" (480, 512 B bulk, GET_LEN 64) | "ss" (5000, 1024 B, GET_LEN 384); §b.2
    get_len: "int | None" = None        # None = persona default; e.g. 128 for the odd-length case (§4.5 WARN rule)
    ack_busy_polls: int = 2             # GET_CUR answers 0x55 this many times, then 0x56 + echo + reg byte (§b.4)
    ack_delay_s: float = 0.0            # ... and not before this long after the SET_CUR (a slow tracker; the
                                        # stop budget of §4.6 is proven with 0.19 and 0.99)
    ack_never: bool = False             # never leaves 0x55 (stale XU state, §b.15)
    stream_acks: bool = True            # False: STREAM_ON/OFF never acked (implementations disagree, §b.5)
    magic_needed: int = 3               # REG_WRITEs latch only at this MAGIC of the cadence (BITFOXDEN, §b.7)
    native_len_only: bool = True        # writes with wLength != the programming length ack but do not program (§b.4)
    programs_at_len: "int | None" = None    # the programming length (None = get_len); 384 with get_len 64 = "only
                                            # 384 programs" (Liptracker vs BITFOXDEN, §13)
    stall_wrong_len: bool = False       # XU wLength != get_len -> EPIPE (uvcvideo's length check, §4.5)
    rhythm_required: bool = False       # acks only if GET_LEN preceded and followed the SET_CUR (S4, §b.4)
    xu_between_required: bool = False   # stream starts only if Block A came between PROBE SET_CUR and COMMIT (S3)
    setif_required: str = "none"        # "none" | "after_commit" | "before_probe": otherwise test bars (§b.8 step 6)
    payload: int = 32768                # real payload stride on the wire (§b.10)
    probe_payload: "int | None" = None  # dwMaxPayloadTransferSize reported (None = payload); 0 and 0xFFFF8000
                                        # are the quirks uvcvideo fixes up (§b.8)
    frame_w: int = 400                  # frame descriptor size (640x480 -> FAULT(unsupported), §4.4)
    frame_h: int = 400
    fps: float = 60.0                   # real frame rate (BITFOXDEN shows 27 fps, §b.15)
    eof: bool = True                    # False: EOF-less device (frames end by FID/size, §4.10)
    err_every_k_frames: int = 0         # ERR bit in one payload of every k-th frame (§4.10)
    frame_bytes_override: "int | None" = None   # 614400 to emit alt-size frames (BITFOXDEN's fused stereo, §b.16)
    scene: str = "mouth"                # §7.4
    layout: str = "h1_copy"             # §7.4 (h1_chroma80, h2_full, flat_odd: §b.11 hypotheses)
    dark_below_current: "int | None" = None     # frames dark unless IR current <= this (brown-out-like); None = never
    dark_from_s: "float | None" = None          # frames dark from this sim time on (lens covered / heat brown-out);
                                                # with dark_below_current only while the current is above it
    ir_off_dark: bool = True            # PWM 0 or registers not latched -> dark frames (LP5562 off, §b.6)
    settle_required_s: float = 8.0      # re-activation sooner after a teardown -> REG_WRITE acks time out (BITFOXDEN)
    wedge_on_dirty_close: bool = True   # fd closed while streaming without STREAM_OFF -> acks time out until
                                        # replug/reset (BITFOXDEN, §b.13)
    reset_clears_wedge: bool = True     # USBDEVFS_RESET clears a wedge (§b.13, unverified)
    start_deadline_ms: int = 1000       # URBs must arrive within this after commit, else the device idles (§b.9)
    starve_stop_ms: int = 100           # no URB in flight this long while streaming -> stream stops (§b.9)
    fifo_bytes: int = 65536             # device FIFO while no URB is queued (overflow loses bytes)
    usbfs_memory_bytes: int = 16 << 20  # in-flight URB memory limit -> ENOMEM (devio.c usbfs_memory_mb)
    urb_max_bytes: "int | None" = None  # SUBMITURB above this -> EINVAL (host controller limit)
    eproto_burst_at_s: "float | None" = None    # an EPROTO burst (> 50 in 1 s) at this sim time (§b.15)
    epipe_at_s: "float | None" = None           # one STALL of the bulk endpoint at this sim time (§b.9)
    unplug_at_s: "float | None" = None          # the device disappears at this sim time
    replug_after_s: "float | None" = None       # ... and comes back (new devnum) this much later
    brownout_current_above: "int | None" = None  # IR current above this -> EPROTO burst, unplug, re-enumerate after
                                                 # reenumerate_s (§b.15 "error -71")
    reenumerate_s: float = 3.0
    kernel_driver: "str | None" = None  # "uvcvideo": interfaces bound to a kernel driver (§b.9.3)
    other_usbfs_owner: "tuple | None" = None    # (812, "vftstream"): interface 1 held by another usbfs process
    node_mode: str = "rw"               # "rw" | "ro" (EACCES on O_RDWR) | "ro_for:<s>" (udev race, §4.22)
    reg_read_supported: bool = True     # REG_READ answers the register at byte 17 (§b.5); else error 0xFF
    sysname: str = "1-1"                # "1-1.2" = behind a hub (§b.13)
    probe_stall_lens: tuple = ()        # PROBE SET_CUR lengths that STALL (uvcvideo's Quanta quirk, §b.8)
    commit_stall_lens: tuple = ()       # COMMIT SET_CUR lengths that STALL


def _kind(cmd):
    head = bytes(cmd[:4])
    if head[:2] == uvc.MAGIC:
        return "magic"
    if head == uvc.STREAM_OFF:
        return "stream_off"
    if head == uvc.STREAM_ON:
        return "stream_on"
    if head[:2] == b"\x50\xab":
        return "reg_write"
    if head[:2] == b"\x51\xab":
        return "reg_read"
    return "other"


def _epipe(what):
    return OSError(errno.EPIPE, "STALL: %s" % what)


class _Stream:
    """The wire of one stream: frames of payloads, produced analytically from t0."""

    def __init__(self, vft, t0, mode):
        m = vft.m
        self.vft, self.t0, self.mode = vft, t0, mode
        self.T = 1.0 / m.fps
        data = m.payload - 12
        nfull, last = divmod(vft.frame_bytes, data)
        self.sizes = [m.payload] * nfull + ([12 + last] if last else [])
        self.bounds = list(itertools.accumulate(self.sizes))
        self.W = self.bounds[-1]
        self.short = [s % vft.packet != 0 for s in self.sizes]
        self.cursor = 0
        self.stopped = False
        self.idle_since = None
        self.lost = 0
        self.frames_built = 0
        self._wire = {}
        self.pts_step = int(round(CLOCK_HZ / m.fps))

    def produced(self, t):
        if t <= self.t0:
            return 0
        k, rem = divmod(t - self.t0, self.T)
        frac = rem / (STREAM_RATE_SHARE * self.T)
        return int(k) * self.W + (self.W if frac >= 1 else int(frac * self.W))

    def time_of_end(self, e):
        k, b = divmod(e - 1, self.W)
        return self.t0 + k * self.T + STREAM_RATE_SHARE * self.T * (b + 1) / self.W

    def plan(self, size):
        """The end position of a URB of `size` bytes starting at the cursor (full, or a short packet)."""
        pos, got, W = self.cursor, 0, self.W
        while got < size:
            k, b = divmod(pos, W)
            j = bisect.bisect_right(self.bounds, b)
            end = k * W + self.bounds[j]
            if end - pos <= size - got:
                got += end - pos
                pos = end
                if self.short[j]:
                    break
            else:
                pos += size - got
                break
        return pos

    def resume(self, now):
        """Flow resumes after no URB was receiving: starve stop, or FIFO overflow loses bytes."""
        if self.idle_since is None:
            return
        gap, self.idle_since = now - self.idle_since, None
        if gap * 1000.0 >= self.vft.m.starve_stop_ms:
            self.stopped = True
            self.vft.starve_stops += 1
            return
        excess = self.produced(now) - self.cursor - self.vft.m.fifo_bytes
        if excess > 0:
            self.cursor += excess
            self.lost += excess

    def wire(self, k):
        w = self._wire.get(k)
        if w is None:
            w = self._build(k)
            self._wire[k] = w
            for old in [x for x in self._wire if x < k - 2]:
                del self._wire[old]
        return w

    def _build(self, k):
        m, vft = self.vft.m, self.vft
        self.frames_built += 1
        vft.frames_emitted += 1
        img = vft.frame_image(k, self.t0 + k * self.T, self.mode)
        pts = (k * self.pts_step + 0x1000) & 0xFFFFFFFF
        scr = (pts + 4500) & 0xFFFFFFFF
        err_payload = len(self.sizes) // 2 if m.err_every_k_frames and k % m.err_every_k_frames == \
            m.err_every_k_frames - 1 else -1
        parts, off, last = [], 0, len(self.sizes) - 1
        for j, size in enumerate(self.sizes):
            flags = 0x0C | (k & 1) | (0x02 if j == last and m.eof else 0) | (0x40 if j == err_payload else 0)
            parts.append(struct.pack("<BBIIH", 12, flags, pts, scr, (k * 16 + j) & 0x7FF))
            parts.append(img[off:off + size - 12])
            off += size - 12
        return b"".join(parts)

    def take(self, end):
        """The wire bytes from the cursor to `end`; the cursor moves there."""
        c, out = self.cursor, []
        while c < end:
            k, b = divmod(c, self.W)
            stop = min(end, (k + 1) * self.W)
            out.append(self.wire(k)[b:b + stop - c])
            c = stop
        self.cursor = end
        return out[0] if len(out) == 1 else b"".join(out)


class FakeVft:
    """The device behind FakeKernel. The kernel calls control() for every control transfer and the
    stream methods for URBs; `on_xu(kind, phase)` (phase "set" | "done") is a test hook."""

    def __init__(self, model: "VftModel | None" = None, clock=None):
        self.m = model if model is not None else VftModel()
        if self.m.persona not in ("hs", "ss"):
            raise ValueError("persona must be 'hs' or 'ss'")
        self.clock = clock
        ss = self.m.persona == "ss"
        self.get_len = self.m.get_len or (384 if ss else 64)
        self.prog_len = self.m.programs_at_len or self.get_len
        self.packet = 1024 if ss else 512
        self.speed_code, self.speed_mbps = (5, 5000) if ss else (3, 480)
        self.frame_bytes = self.m.frame_bytes_override or self.m.frame_w * self.m.frame_h * 2
        self.descriptors = self._descriptors()
        self.t0 = clock.monotonic() if clock is not None else 0.0
        self.on_xu = None
        self.epipe_done = False         # the epipe_at_s stall happens once per rig
        self.gen = 0                    # bumped whenever planned URB completions become invalid
        self.frames_emitted = self.starve_stops = self.power_ons = 0
        self.pwm_history = []           # (t, pwm) on every latch: the fake's only "LED" record
        self.power_on()

    def _descriptors(self):
        raw = bytearray(build_expected(self.m.persona))
        if (self.m.frame_w, self.m.frame_h) != (400, 400):
            i = raw.find(bytes.fromhex("1e2405"))          # FRAME_UNCOMPRESSED
            struct.pack_into("<HH", raw, i + 5, self.m.frame_w, self.m.frame_h)
            struct.pack_into("<I", raw, i + 17, self.m.frame_w * self.m.frame_h * 2)
        return bytes(raw)

    def now(self):
        return self.clock.monotonic()

    # -- device lifecycle

    def power_on(self, keep_wedge=False):
        """A fresh device (plug, replug, re-enumeration after a reset)."""
        self.power_ons += 1
        self.regs, self.pending_writes = {}, []
        self.magic_count, self.magic1_t = 0, None
        self.wedged = bool(keep_wedge and getattr(self, "wedged", False))
        self.stream_on, self.stream_on_t, self.started_in_session = False, None, False
        self.committed, self.commit_t, self.probe_set_t, self.xu_between_ok = False, None, None, False
        self.setif_times, self.xu_pending, self.last_xu_req = [], None, None
        self.last_close_t, self.last_error, self.halted = None, 0, False
        self.probe = uvc.Probe(1, 1, 1, 166666, 0, 0, 0, 0, 0, self.frame_bytes, self._reported_payload(),
                               CLOCK_HZ, 3 if self.m.eof else 1)
        self.stream = None
        self.brownout_at = None
        self.enum_t = self.now() if self.clock is not None else 0.0
        self.gen += 1

    def _reported_payload(self):
        return self.m.payload if self.m.probe_payload is None else self.m.probe_payload

    def on_open(self):
        self.magic_count, self.magic1_t = 0, None
        self.setif_times, self.committed, self.started_in_session = [], False, False

    def on_close(self, had_xu):
        """The usbfs fd closed: buffered writes are lost; closing a running stream without
        STREAM_OFF wedges the device (wedge_on_dirty_close)."""
        now = self.now()
        if self.stream_on and self.started_in_session and self.m.wedge_on_dirty_close:
            self.wedged = True
        self.pending_writes = []
        self.committed = False
        self.stop_stream()
        if had_xu:
            self.last_close_t = now

    def stop_stream(self):
        if self.stream is not None:
            self.stream.stopped = True
        self.stream = None
        self.gen += 1

    # -- control transfers

    def control(self, rt, rq, wv, wi, wl, data):
        """bytes for IN requests, the length for OUT requests; OSError(EPIPE) for a STALL."""
        iface, entity, sel = wi & 0xFF, wi >> 8, wv >> 8
        if rt == 0x01 and rq == uvc.SET_INTERFACE:
            self.set_interface(iface, wv)
            return 0
        if rt & 0x60 != 0x20:
            raise _epipe("standard request 0x%02x" % rq)
        if iface == 0 and entity == 0 and sel == 2 and rq == uvc.GET_CUR:
            code, self.last_error = self.last_error, 0
            return bytes([code])[:wl]
        if iface == 0 and entity in self.xu_units():
            return self._xu(rq, sel, wl, data)
        if iface == 1:
            return self._vs(rq, sel, wl, data)
        self.last_error = 5                             # invalid unit
        raise _epipe("unit %d interface %d" % (entity, iface))

    def xu_units(self):
        return (4,)

    def _xu(self, rq, sel, wl, data):
        if rq == uvc.GET_LEN:
            if self.xu_pending is not None and self.last_xu_req == "set":
                self.xu_pending["post"] = True          # the SRanipal rhythm's second GET_LEN
            self.last_xu_req = "len"
            return self.get_len.to_bytes(2, "little")[:wl]
        if rq == uvc.GET_INFO:
            return b"\x03"[:wl]
        if sel != 2 or rq not in (uvc.SET_CUR, uvc.GET_CUR):
            self.last_error = 6
            raise _epipe("XU selector %d request 0x%02x" % (sel, rq))
        if self.m.stall_wrong_len and wl != self.get_len:
            self.last_error = 4
            raise _epipe("XU length %d != %d" % (wl, self.get_len))
        if rq == uvc.SET_CUR:
            self._xu_set(bytes(data), wl)
            return wl
        return self._xu_get(wl)

    def _xu_set(self, cmd, wl):
        m, now, kind = self.m, self.now(), _kind(cmd)
        p = {"cmd": cmd, "kind": kind, "polls": m.ack_busy_polls, "never": self.wedged, "value": 0,
             "error": None, "pre": self.last_xu_req == "len", "post": False, "done": False, "t": now}
        self.last_xu_req = "set"
        if not self.wedged:
            if kind == "magic":
                self.magic_count += 1
                if self.magic_count == 1:
                    self.magic1_t = now
                if self.magic_count == m.magic_needed:
                    self._latch(now)
            elif kind == "stream_off":
                self.stream_on = False
                self.stop_stream()
            elif kind == "stream_on":
                self.stream_on, self.stream_on_t = True, now
                self.magic_count, self.pending_writes = 0, []
            elif kind == "reg_write":
                if self.last_close_t is not None and now - self.last_close_t < m.settle_required_s:
                    p["never"] = True                   # re-activated inside the settle window
                elif self.magic_count == m.magic_needed - 1 and (not m.native_len_only or wl == self.prog_len):
                    self.pending_writes.append((cmd[8], cmd[16]))
            elif kind == "reg_read":
                if m.reg_read_supported:
                    p["value"] = self.regs.get(cmd[8], 0)
                else:
                    p["error"] = 0xFF
        self.xu_pending = p
        if self.on_xu is not None:
            self.on_xu(kind, "set")

    def _latch(self, now):
        for a, v in self.pending_writes:
            self.regs[a] = v
        if self.pending_writes:
            self.pwm_history.append((now, self.regs.get(0x02, 0)))
        self.pending_writes = []

    def _xu_get(self, wl):
        p, m = self.xu_pending, self.m
        self.last_xu_req = "get"
        busy = b"\x55" + bytes(max(0, wl - 1))
        if p is None:
            return bytes(wl)
        if p["never"] or self.wedged or m.ack_never:
            return busy
        if p["kind"] in ("stream_on", "stream_off") and not m.stream_acks:
            return busy
        if m.rhythm_required and not (p["pre"] and p["post"]):
            return busy
        if m.ack_delay_s and self.now() - p["t"] < m.ack_delay_s:
            return busy
        if p["polls"] > 0:
            p["polls"] -= 1
            return busy
        if p["error"] is not None:
            return bytes([p["error"]]) + bytes(max(0, wl - 1))
        if not p["done"]:
            p["done"] = True
            if self.on_xu is not None:
                self.on_xu(p["kind"], "done")
        return uvc.done_reply(p["cmd"], wl, p["value"])

    def _vs(self, rq, sel, wl, data):
        now = self.now()
        if sel == uvc.VS_PROBE and rq == uvc.SET_CUR:
            if wl in self.m.probe_stall_lens or wl not in uvc.PROBE_FORMATS:
                self.last_error = 4
                raise _epipe("PROBE SET_CUR length %d" % wl)
            req = uvc.unpack_probe(bytes(data))
            self.probe.frame_interval = req.frame_interval or 166666
            self.probe_set_t = now
            return wl
        if sel in (uvc.VS_PROBE, uvc.VS_COMMIT) and rq in (uvc.GET_CUR, uvc.GET_DEF, uvc.GET_MIN, uvc.GET_MAX):
            if wl not in uvc.PROBE_FORMATS:
                self.last_error = 4
                raise _epipe("probe length %d" % wl)
            return uvc.pack_probe(self.probe, wl)
        if sel == uvc.VS_COMMIT and rq == uvc.SET_CUR:
            if wl in self.m.commit_stall_lens or wl not in uvc.PROBE_FORMATS:
                self.last_error = 4
                raise _epipe("COMMIT length %d" % wl)
            self.committed, self.commit_t = True, now
            self.xu_between_ok = (self.probe_set_t is not None and self.magic1_t is not None
                                  and self.probe_set_t < self.magic1_t and self.stream_on_t is not None
                                  and self.stream_on_t > self.magic1_t)
            return wl
        self.last_error = 7
        raise _epipe("VS selector %d request 0x%02x" % (sel, rq))

    def set_interface(self, iface, alt):
        if iface == 1:
            self.setif_times.append(self.now())
            self.halted = False

    def set_configuration(self):
        self.setif_times, self.halted, self.committed = [], False, False
        self.stop_stream()

    def clear_halt(self, now):
        """CLEAR_HALT of the bulk endpoint: data flows again (bytes beyond the FIFO were lost)."""
        if self.halted:
            self.halted = False
            self.gen += 1
            if self.stream is not None and not self.stream.stopped:
                self.stream.resume(now)

    # -- stream

    def _setif_ok(self):
        rule = self.m.setif_required
        if rule == "after_commit":
            return any(t >= self.commit_t for t in self.setif_times)
        if rule == "before_probe":
            return self.probe_set_t is not None and any(t <= self.probe_set_t for t in self.setif_times)
        return True

    def urb_submitted(self, now):
        """Called for every SUBMITURB: the first one after a COMMIT decides whether a stream starts."""
        if self.committed:
            self.committed = False
            late = (now - self.commit_t) * 1000.0 > self.m.start_deadline_ms
            if late or not self.stream_on or (self.m.xu_between_required and not self.xu_between_ok):
                self.stop_stream()
                return
            self.stream = _Stream(self, now, "scene" if self._setif_ok() else "bars")
            self.started_in_session = True
            self.gen += 1
            return
        if self.stream is not None and not self.stream.stopped:
            self.stream.resume(now)

    def flow_idle(self, now):
        """No URB is receiving any more (queue empty or endpoint halted)."""
        if self.stream is not None and self.stream.idle_since is None:
            self.stream.idle_since = now

    def plan_urb(self, size, now):
        """(completion time, end position) of the head URB, or None if no data will come."""
        s = self.stream
        if s is None or s.stopped or self.halted:
            return None
        end = s.plan(size)
        return max(now, s.time_of_end(end)), end

    def take_urb(self, end, now):
        """(status, data) of the head URB completing now (one event per URB)."""
        s, m = self.stream, self.m
        data = s.take(end)
        rel = now - self.t0
        for at in (m.eproto_burst_at_s, self.brownout_at):
            if at is not None and at <= rel < at + _BURST_S:
                return -71, b""
        if m.epipe_at_s is not None and not self.epipe_done and rel >= m.epipe_at_s:
            self.epipe_done = True
            self.halted = True
            self.gen += 1
            return -32, b""
        return 0, data

    def frame_image(self, k, t, mode):
        """The frame k image (frame_bytes long) under the programmed IR state at time t."""
        m = self.m
        if mode == "bars":
            scene, gain = "bars", 1.0
        else:
            pwm, cur = self.regs.get(0x02, 0), self.regs.get(0x05, 0)
            rel = t - self.t0
            if m.ir_off_dark:                   # brightness follows the programmed PWM and current
                dark, gain = pwm == 0, (pwm / 255.0) * (cur / 0xB2 if cur else 0.0)
            else:                               # the image ignores the IR registers
                dark, gain = False, 1.0
            above = m.dark_below_current is not None and cur > m.dark_below_current
            if m.dark_from_s is not None:
                dark = dark or (rel >= m.dark_from_s and (m.dark_below_current is None or above))
            elif above:
                dark = True
            scene = "dark" if dark else m.scene
            gain = 1.0 if dark else gain
        img = make_scene_frame(scene, t - self.t0, m.layout, gain)
        if self.frame_bytes != FRAME_BYTES:
            img = (img * (self.frame_bytes // FRAME_BYTES + 1))[:self.frame_bytes]
        if scene in ANIMATED:                   # every frame differs (the frozen check hashes frame[::7])
            img = bytearray(img)
            img[0], img[7], img[14] = k & 0xFF, (k >> 8) & 0xFF, (k * 37) & 0xFF
        return img

    def warm(self):
        """Render (and cache, per process) every image this device may stream at gain 1: each animation
        phase of its scene, the dark scene and the test bars. A real-clock stream then never renders
        inside a completion event, where a first render (about 50 ms here, far more under QEMU) holds
        the kernel lock and lets the URB queue run dry."""
        for scene in (self.m.scene, "dark", "bars"):
            for p in range(PHASES if scene in ANIMATED else 1):
                make_scene_frame(scene, 2.0 * p / PHASES, self.m.layout)

    def brownout_due(self, now):
        """The brown-out knob: a stream at a latched current above brownout_current_above browns out
        1 s after it starts (EPROTO burst, then unplug). Returns the unplug time or None."""
        m = self.m
        if m.brownout_current_above is None or self.regs.get(0x05, 0) <= m.brownout_current_above:
            return None
        self.brownout_at = now + 1.0 - self.t0
        return now + 1.0 + _BURST_S
