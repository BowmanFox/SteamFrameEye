#!/usr/bin/env python3
"""
frameeye_osc.py - Steam Frame eye tracking -> network (headset side).

Reads the eye-tracking server's shared-memory object /dev/shm/eye-server.mmap
(per-eye gaze direction, fixation point, eyelid openness) and forwards it:

  --raw HOST:PORT     compact binary packet per sample, for the SteamFrameEyeModule
                      VRCFaceTracking module on the PC (repeatable)
  --target HOST:PORT  VRCFT "v2" avatar parameters as OSC straight to VRChat
                      (use when no VRCFT is running)

Runs stand-alone (CLI, e.g. from a systemd user unit) or embedded in
frameeye_overlay.py (FrameEye Bridge, the SteamVR dashboard app) through SenderRuntime.

Data layout and the OSC contract follow konsti219/frameeyeosc (Rust, MIT License,
Copyright (c) 2026 konsti219): https://github.com/konsti219/frameeyeosc
Dependency-free Python re-implementation so it runs on stock SteamOS.
"""
import argparse
import collections
import ctypes
import logging
import math
import mmap
import os
import platform
import socket
import struct
import sys
import threading
import time

from eye_smoothing import DirectOscSmoother

MMAP_PATH = "/dev/shm/eye-server.mmap"
VERSION_OFF = 0x00            # u32 ABI version of the shared-memory object
INIT_OFF = 0x04               # u32, 1 once the producer has set the object up
SEQ_OFF = 0x38                # u32 sequence counter, bumped by the producer per sample
HEADER_LEN = 0x40
# Known ABI versions -> (object size, offset of the EyeDataMmap record). The header up to the
# sequence counter and the record itself are unchanged between them; version 5 grew the control
# block in between by 5 bytes (frameeyeosc "add support for protocol 5").
EYE_SERVER_ABI = {
    4: (0x4F21A, 0x152),      # eye server build 20260921 (SteamOS 0.4 before the 0.4.3 beta)
    5: (0x4F21F, 0x157),      # eye server build 20260930 (SteamOS 0.4.3 beta)
}
# Object size minus record offset, the same in versions 4 and 5. An unknown version gets its record
# offset guessed from this, and only plausible samples are used then (see record_valid).
RECORD_TAIL = 0x4F0C8
# EyeDataMmap (packed):
#   u32 producer_state; u8 sample_flag; f64 sample_time;
#   f32 gaze_direction[2][3]; f32 gaze_covariance_diag[2][3]; f32 fixation_point[3];
#   f32 pre_fusion_gaze[2][3]; f32 pre_fusion_cov_diag[2][3]; f32 openness[2]; f32 estimate_extra[8]
EYE_DATA_FMT = "<IBd6f6f3f6f6f2f8f"
EYE_DATA_LEN = struct.calcsize(EYE_DATA_FMT)   # 137 bytes

# Raw packet for the PC-side VRCFT module (little-endian, 96 bytes):
#   char[4] "SFEY", u16 version=1, u16 flags (bit0 valid), u32 seq, f64 sample_time,
#   f32 gazeL[3], gazeR[3], fixation[3], f32 opennessL, opennessR, f32 covL[3], covR[3], f64 send_time
RAW_FMT = "<4sHHId3f3f3f2f3f3fd"
RAW_VERSION = 1

SCALE = 4.0 / math.pi
DEFAULT_POLL_MS = 4.0         # fallback polling interval when the futex wait is unavailable
FUTEX_TIMEOUT_S = 0.004       # wake at least every poll interval (also covers a producer that never FUTEX_WAKEs)
STALE_SECONDS = 1.5           # no new sample for this long -> "idle" (headset not worn / standby)
REOPEN_SECONDS = 2.0

log = logging.getLogger("frameeye.sender")


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def gaze_angles(v):
    """[x.atan2(-z) * scale, y.atan2(-z) * scale], each clamped to [-1, 1] (as frameeyeosc)."""
    x, y, z = v
    return (clamp(math.atan2(x, -z) * SCALE, -1.0, 1.0),
            clamp(math.atan2(y, -z) * SCALE, -1.0, 1.0))


def osc_pad(b):
    return b + b"\0" * (4 - len(b) % 4)


def osc_float(address, value):
    return osc_pad(address.encode()) + osc_pad(b",f") + struct.pack(">f", value)


def osc_true(address):
    return osc_pad(address.encode()) + osc_pad(b",T")


Layout = collections.namedtuple("Layout", "version size record_offset guessed")


class SourceNotReady(OSError):
    """The eye server object exists but is not usable yet (not initialized, or an unknown layout)."""

    def __init__(self, message, state):
        OSError.__init__(self, message)
        self.state = state


def detect_layout(mm, size):
    """Layout of the mapped eye server object from its version word. Raises SourceNotReady."""
    if size < HEADER_LEN:
        raise SourceNotReady("eye server object is only %d bytes" % size, "starting")
    version, initialized = struct.unpack_from("<II", mm, VERSION_OFF)
    if initialized != 1:
        raise SourceNotReady("eye server object not initialized yet (version %d)" % version, "starting")
    known = EYE_SERVER_ABI.get(version)
    if known is not None:
        need, offset = known
        if size < need:
            raise SourceNotReady("eye server version %d needs %d bytes, object has %d" % (version, need, size),
                                 "unsupported")
        return Layout(version, size, offset, False)
    offset = size - RECORD_TAIL
    if not HEADER_LEN <= offset <= 0x1000:
        raise SourceNotReady("unsupported eye server version %d (%d bytes)" % (version, size), "unsupported")
    return Layout(version, size, offset, True)


def describe_layout(layout):
    return "version %d%s, %d bytes, record at 0x%x" % (
        layout.version, " (unknown, record offset guessed)" if layout.guessed else "", layout.size,
        layout.record_offset)


def parse_record(mm, offset):
    f = struct.unpack_from(EYE_DATA_FMT, mm, offset)
    return dict(producer_state=f[0], sample_flag=f[1], sample_time=f[2],
                gaze=(f[3:6], f[6:9]), cov=(f[9:12], f[12:15]), fixation=f[15:18],
                pre=(f[18:21], f[21:24]), pre_cov=(f[24:27], f[27:30]),
                openness=f[30:32], extra=f[32:40])


def record_plausible(r):
    """Unit-length gaze directions and openness near 0..1, which a misaligned read practically never
    produces. Only applied to a guessed layout."""
    for gaze in r["gaze"]:
        if not 0.5 < math.sqrt(sum(v * v for v in gaze)) < 1.5:
            return False
    return all(-0.5 <= o <= 1.5 for o in r["openness"])


def record_valid(r, guessed=False):
    if r["producer_state"] != 1 or not math.isfinite(r["sample_time"]):
        return False
    vals = list(r["gaze"][0]) + list(r["gaze"][1]) + list(r["fixation"]) + list(r["openness"])
    if not all(math.isfinite(v) for v in vals):
        return False
    return record_plausible(r) if guessed else True


def derived_values(r):
    """(lx, ly, rx, ry, cx, cy, lid_l, lid_r) as sent in the v2 parameters."""
    lx, ly = gaze_angles(r["gaze"][0])
    rx, ry = gaze_angles(r["gaze"][1])
    cx, cy = gaze_angles(r["fixation"])
    return (lx, ly, rx, ry, cx, cy, clamp(r["openness"][0], 0.0, 1.0), clamp(r["openness"][1], 0.0, 1.0))


def build_osc(r, prefix, values=None):
    lx, ly, rx, ry, cx, cy, lid_l, lid_r = vals = derived_values(r) if values is None else values
    base = "/avatar/parameters" + prefix
    msgs = [osc_true(base + "/EyeTrackingActive")]
    for suffix, val in (("EyeLeftX", lx), ("EyeLeftY", ly), ("EyeRightX", rx), ("EyeRightY", ry),
                        ("EyeX", cx), ("EyeY", cy), ("EyeLidLeft", lid_l), ("EyeLidRight", lid_r)):
        msgs.append(osc_float(base + "/v2/" + suffix, float(val)))
    return msgs, vals


def build_raw(r, seq, valid):
    return struct.pack(RAW_FMT, b"SFEY", RAW_VERSION, 1 if valid else 0, seq & 0xFFFFFFFF,
                       r["sample_time"], *r["gaze"][0], *r["gaze"][1], *r["fixation"],
                       *r["openness"], *r["cov"][0], *r["cov"][1], time.monotonic())


def parse_hostport(s):
    host, _, port = s.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError("expected HOST:PORT, got %r" % s)
    return (host, int(port))


def open_mmap(path, writable=False):
    """Maps the eye server object and returns (mmap, Layout, inode). The futex wait needs the address
    of the sequence word, which ctypes only hands out for a writable mapping; nothing is ever written
    to it. Raises SourceNotReady while the object is not initialized or has an unknown layout."""
    fd = os.open(path, os.O_RDWR if writable else os.O_RDONLY)
    try:
        st = os.fstat(fd)
        if st.st_size < HEADER_LEN:
            raise SourceNotReady("eye server object is only %d bytes" % st.st_size, "starting")
        mm = mmap.mmap(fd, st.st_size, access=mmap.ACCESS_WRITE if writable else mmap.ACCESS_READ)
    finally:
        os.close(fd)
    try:
        layout = detect_layout(mm, st.st_size)
        if layout.record_offset + EYE_DATA_LEN > st.st_size:
            raise SourceNotReady("eye server object too small for its record", "unsupported")
    except OSError:
        mm.close()
        raise
    return mm, layout, st.st_ino


def source_changed(path, mm, layout, inode):
    """True when the eye server re-created its object or changed its version since it was mapped."""
    try:
        if os.stat(path).st_ino != inode:
            return True
        return struct.unpack_from("<I", mm, VERSION_OFF)[0] != layout.version
    except (OSError, ValueError):
        return True


def source_info(path=MMAP_PATH):
    """One line about the eye server object, for --status and --once."""
    try:
        mm, layout, _ = open_mmap(path)
    except SourceNotReady as e:
        return "eye server: %s" % e
    except OSError as e:
        return "eye server: %s not available (%s)" % (path, e.strerror or e)
    mm.close()
    return "eye server: %s" % describe_layout(layout)


# --- futex wait on the producer's sequence word (what the eye server signals on every sample) -------

_SYS_FUTEX = {"aarch64": 98, "x86_64": 202}.get(platform.machine())
_FUTEX_WAIT = 0
_EAGAIN, _EINTR, _ETIMEDOUT = 11, 4, 110


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


class SeqWaiter:
    """Blocks until the u32 at SEQ_OFF changes (FUTEX_WAIT with a timeout). Falls back to sleeping
    when the syscall is not available or refuses the address."""

    def __init__(self, mm, poll_s):
        self.poll_s = poll_s
        self.word = None
        self.addr = None
        self.libc = None
        self.usable = False
        if _SYS_FUTEX is not None and sys.platform.startswith("linux"):
            try:
                self.word = ctypes.c_uint32.from_buffer(mm, SEQ_OFF)   # keeps the mapping referenced
                self.addr = ctypes.addressof(self.word)
                self.libc = ctypes.CDLL(None, use_errno=True)
                self.libc.syscall.restype = ctypes.c_long
                self.usable = True
            except (TypeError, ValueError, OSError):
                self.usable = False

    def wait(self, expected, timeout_s=FUTEX_TIMEOUT_S):
        """Returns when the value differs from expected, on a wake-up, or after timeout_s."""
        if not self.usable:
            time.sleep(self.poll_s)
            return
        ts = _Timespec(int(timeout_s), int((timeout_s % 1.0) * 1e9))
        r = self.libc.syscall(_SYS_FUTEX, ctypes.c_void_p(self.addr), ctypes.c_int(_FUTEX_WAIT),
                              ctypes.c_uint32(expected), ctypes.byref(ts), None, ctypes.c_uint32(0))
        if r == -1:
            err = ctypes.get_errno()
            if err not in (_EAGAIN, _EINTR, _ETIMEDOUT):
                # unexpected (e.g. EFAULT/EINVAL on this mapping): stop using the futex path
                self.usable = False
                time.sleep(self.poll_s)


def build_parser():
    ap = argparse.ArgumentParser(description="Steam Frame eye tracking -> network")
    ap.add_argument("--raw", action="append", default=[], metavar="HOST:PORT",
                    help="binary feed for the VRCFT module on the PC (repeatable)")
    ap.add_argument("--target", default=None, metavar="HOST:PORT",
                    help="direct VRChat OSC v2 parameters (when no VRCFT is used)")
    ap.add_argument("--prefix", default="/FT", help="avatar parameter prefix for --target, must start with '/'")
    ap.add_argument("--no-osc-smoothing", dest="osc_smoothing", action="store_false",
                    help="disable gaze/eyelid smoothing for direct OSC (raw SFEY is always unfiltered)")
    ap.add_argument("--poll-ms", type=float, default=DEFAULT_POLL_MS, help="poll interval for the sequence counter")
    ap.add_argument("--mmap", default=MMAP_PATH, help="eye server shared memory object")
    ap.add_argument("--once", action="store_true", help="send the current record once and exit")
    ap.add_argument("--verbose", action="store_true", help="print every sample")
    return ap


def runtime_args(args):
    """The sender options of a parsed namespace as a command line, for the SteamVR manifest."""
    out = []
    for raw in args.raw:
        out += ["--raw", raw]
    if args.target:
        out += ["--target", args.target]
    if args.prefix != "/FT":
        out += ["--prefix", args.prefix]
    if args.poll_ms != DEFAULT_POLL_MS:
        out += ["--poll-ms", str(args.poll_ms)]
    if args.mmap != MMAP_PATH:
        out += ["--mmap", args.mmap]
    if not args.osc_smoothing:
        out += ["--no-osc-smoothing"]
    return out


class SenderRuntime:
    """Reads the eye server's shared memory in a thread and sends every new sample to the targets."""

    def __init__(self, args):
        if not args.prefix.startswith("/"):
            raise ValueError("--prefix must start with '/'")
        self.raw_targets = [parse_hostport(r) for r in args.raw if r and r != "none"]
        self.osc_target = parse_hostport(args.target) if args.target and args.target != "none" else None
        self.enabled = True           # False: keep tracking state for display, send nothing
        self.osc_smoothing = args.osc_smoothing
        self._osc_smoother = DirectOscSmoother()
        self._osc_mode = None
        self.prefix = args.prefix
        self.poll_s = max(0.001, args.poll_ms / 1000.0)
        self.mmap_path = args.mmap
        self.layout = None            # Layout of the mapped eye server object
        self.verbose = args.verbose
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._stats = dict(state="no-source", seq=None, samples=0, valid=0, rate=0.0, last_change=None,
                           values=None, valid_last=False, opens=0, send_errors=0, wait="-", abi=None,
                           abi_guessed=False, source_error=None, started=time.monotonic())

    # -- lifecycle

    def start(self):
        self._thread = threading.Thread(target=self._run, name="frameeye-sender", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)

    def snapshot(self):
        with self._lock:
            s = dict(self._stats)
        now = time.monotonic()
        s["age"] = None if s["last_change"] is None else now - s["last_change"]
        s["uptime"] = int(now - s["started"])
        s["raw_targets"] = ["%s:%d" % t for t in self.raw_targets]
        s["osc_target"] = "%s:%d" % self.osc_target if self.osc_target else None
        s["enabled"] = self.enabled
        s["osc_smoothing"] = self.osc_smoothing
        return s

    def set_targets(self, raw=None, osc="keep"):
        """Changes the destinations at runtime (strings HOST:PORT). osc=None disables direct OSC."""
        if raw is not None:
            self.raw_targets = [parse_hostport(r) for r in raw if r and r != "none"]
        if osc != "keep":
            self.osc_target = parse_hostport(osc) if osc and osc != "none" else None
        log.info("targets now raw %s osc %s", ["%s:%d" % t for t in self.raw_targets], self.osc_target)

    # -- one sample

    def send_record(self, mm, seq):
        layout = self.layout
        r = parse_record(mm, layout.record_offset)
        valid = record_valid(r, layout.guessed)
        errors = 0
        # Reset on pause/resume, target changes, or toggling the filter. Only this sender thread
        # touches filter history; the dashboard changes the simple configuration attributes.
        mode = (self.enabled, self.osc_target, self.osc_smoothing)
        if mode != self._osc_mode or not valid:
            self._osc_smoother.reset()
            self._osc_mode = mode
        if not self.enabled:
            return valid, (derived_values(r) if valid else None), 0
        for target in self.raw_targets:
            try:
                self.sock.sendto(build_raw(r, seq, valid), target)
            except OSError:
                errors += 1
        vals = derived_values(r) if valid else None
        if valid and self.osc_target is not None:
            if self.osc_smoothing:
                vals = self._osc_smoother.process(r)
            msgs, _ = build_osc(r, self.prefix, vals)
            for m in msgs:
                try:
                    self.sock.sendto(m, self.osc_target)
                except OSError:
                    errors += 1
        if self.verbose:
            log.info("seq=%d valid=%d t=%.3f %s", seq, int(valid), r["sample_time"],
                     "L=(%.3f,%.3f) R=(%.3f,%.3f) C=(%.3f,%.3f) lids=(%.2f,%.2f)" % vals if vals else "")
        return valid, vals, errors

    # -- thread

    def _set(self, **kw):
        with self._lock:
            self._stats.update(kw)

    def _run(self):
        mm = None
        waiter = None
        inode = None
        last_seq = None
        rate = 0.0
        last_sample_t = None
        next_source_check = 0.0
        while not self._stop.is_set():
            if mm is None:
                try:
                    try:
                        mm, layout, inode = open_mmap(self.mmap_path, writable=True)
                    except SourceNotReady:
                        raise
                    except OSError:
                        mm, layout, inode = open_mmap(self.mmap_path)
                    self.layout = layout
                    waiter = SeqWaiter(mm, self.poll_s)
                    last_seq = None
                    self._set(state="idle", opens=self._stats["opens"] + 1, wait="futex" if waiter.usable else "poll",
                              abi=layout.version, abi_guessed=layout.guessed, source_error=None)
                    log.info("opened %s, %s (%s wait) -> raw %s osc %s", self.mmap_path, describe_layout(layout),
                             "futex" if waiter.usable else "polling",
                             ["%s:%d" % t for t in self.raw_targets], self.osc_target)
                    if layout.guessed:
                        log.warning("eye server version %d is not known to this FrameEye Bridge; trying the record "
                                    "offset 0x%x derived from the object size and sending only plausible samples",
                                    layout.version, layout.record_offset)
                except OSError as e:
                    state = getattr(e, "state", "no-source")
                    if self._stats["state"] != state or self._stats["source_error"] != str(e):
                        log.info("eye server not usable (%s); retrying", e)
                    self._set(state=state, source_error=str(e))
                    self._stop.wait(REOPEN_SECONDS)
                    continue
            try:
                seq = struct.unpack_from("<I", mm, SEQ_OFF)[0]
            except ValueError:
                mm = None
                continue
            now = time.monotonic()
            if seq != last_seq:
                if last_seq is not None:      # the very first read is the stale record from before we started
                    valid, vals, errors = self.send_record(mm, seq)
                    if last_sample_t is not None:
                        dt = now - last_sample_t
                        if dt > 0:
                            rate = 1.0 / dt if dt > 1.0 else 0.9 * rate + 0.1 / dt
                    last_sample_t = now
                    with self._lock:
                        st = self._stats
                        st["samples"] += 1
                        st["valid"] += int(valid)
                        st["send_errors"] += errors
                        st["rate"] = rate
                        st["last_change"] = now
                        st["valid_last"] = valid
                        if vals is not None:
                            st["values"] = vals
                        if st["state"] != "streaming":
                            st["state"] = "streaming"
                            log.info("eye data streaming (seq %d)", seq)
                last_seq = seq
                self._set(seq=seq)
            elif self._stats["state"] == "streaming" and last_sample_t is not None and now - last_sample_t > STALE_SECONDS:
                self._set(state="idle", rate=0.0)
                log.info("eye data idle (headset not worn or SteamVR in standby)")
            elif self._stats["state"] != "streaming" and now >= next_source_check:
                # An eye server restart (for example after an update) re-creates the object, possibly
                # with another version; the old mapping would then never change again.
                next_source_check = now + REOPEN_SECONDS
                if source_changed(self.mmap_path, mm, self.layout, inode):
                    log.info("eye server object changed; reopening")
                    mm = waiter = None
                    continue
            if seq == last_seq:
                waiter.wait(seq)
        try:
            self.sock.close()
        except OSError:
            pass


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    if not args.raw and not args.target:
        sys.exit("nothing to do: give --raw HOST:PORT and/or --target HOST:PORT")
    try:
        runtime = SenderRuntime(args)
    except ValueError as e:
        sys.exit(str(e))
    if args.once:
        try:
            mm, runtime.layout, _ = open_mmap(args.mmap)
        except OSError as e:
            sys.exit("cannot open %s: %s" % (args.mmap, e))
        log.info("%s: %s", args.mmap, describe_layout(runtime.layout))
        runtime.verbose = True
        seq = struct.unpack_from("<I", mm, SEQ_OFF)[0]
        runtime.send_record(mm, seq)
        return 0
    runtime.start()
    try:
        last = 0.0
        while True:
            time.sleep(1.0)
            if time.monotonic() - last > 60.0:
                last = time.monotonic()
                s = runtime.snapshot()
                log.info("alive: state=%s samples=%d rate=%.1f/s seq=%s", s["state"], s["samples"], s["rate"], s["seq"])
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
