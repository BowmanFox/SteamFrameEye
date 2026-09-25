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
frameeye_overlay.py, the SteamVR overlay application, through SenderRuntime.

Data layout and the OSC contract follow konsti219/frameeyeosc (Rust, MIT License,
Copyright (c) 2026 konsti219): https://github.com/konsti219/frameeyeosc
Dependency-free Python re-implementation so it runs on stock SteamOS.
"""
import argparse
import logging
import math
import mmap
import os
import socket
import struct
import sys
import threading
import time

MMAP_PATH = "/dev/shm/eye-server.mmap"
SEQ_OFF = 0x38                # u32 sequence counter, bumped by the producer per sample
EYE_DATA_OFF = 0x152          # start of the EyeDataMmap record (packed)
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
DEFAULT_POLL_MS = 4.0
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


def parse_record(mm):
    f = struct.unpack(EYE_DATA_FMT, mm[EYE_DATA_OFF:EYE_DATA_OFF + EYE_DATA_LEN])
    return dict(producer_state=f[0], sample_flag=f[1], sample_time=f[2],
                gaze=(f[3:6], f[6:9]), cov=(f[9:12], f[12:15]), fixation=f[15:18],
                pre=(f[18:21], f[21:24]), pre_cov=(f[24:27], f[27:30]),
                openness=f[30:32], extra=f[32:40])


def record_valid(r):
    if r["producer_state"] != 1:
        return False
    vals = list(r["gaze"][0]) + list(r["gaze"][1]) + list(r["fixation"]) + list(r["openness"])
    return all(math.isfinite(v) for v in vals)


def derived_values(r):
    """(lx, ly, rx, ry, cx, cy, lid_l, lid_r) as sent in the v2 parameters."""
    lx, ly = gaze_angles(r["gaze"][0])
    rx, ry = gaze_angles(r["gaze"][1])
    cx, cy = gaze_angles(r["fixation"])
    return (lx, ly, rx, ry, cx, cy, clamp(r["openness"][0], 0.0, 1.0), clamp(r["openness"][1], 0.0, 1.0))


def build_osc(r, prefix):
    lx, ly, rx, ry, cx, cy, lid_l, lid_r = vals = derived_values(r)
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


def open_mmap(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size
        if size < EYE_DATA_OFF + EYE_DATA_LEN:
            raise OSError("unexpected mmap size %d" % size)
        return mmap.mmap(fd, size, access=mmap.ACCESS_READ)
    finally:
        os.close(fd)


def build_parser():
    ap = argparse.ArgumentParser(description="Steam Frame eye tracking -> network")
    ap.add_argument("--raw", action="append", default=[], metavar="HOST:PORT",
                    help="binary feed for the VRCFT module on the PC (repeatable)")
    ap.add_argument("--target", default=None, metavar="HOST:PORT",
                    help="direct VRChat OSC v2 parameters (when no VRCFT is used)")
    ap.add_argument("--prefix", default="/FT", help="avatar parameter prefix for --target, must start with '/'")
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
    return out


class SenderRuntime:
    """Reads the eye server's shared memory in a thread and sends every new sample to the targets."""

    def __init__(self, args):
        if not args.prefix.startswith("/"):
            raise ValueError("--prefix must start with '/'")
        self.raw_targets = [parse_hostport(r) for r in args.raw if r and r != "none"]
        self.osc_target = parse_hostport(args.target) if args.target and args.target != "none" else None
        self.prefix = args.prefix
        self.poll_s = max(0.001, args.poll_ms / 1000.0)
        self.mmap_path = args.mmap
        self.verbose = args.verbose
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._stats = dict(state="no-source", seq=None, samples=0, valid=0, rate=0.0, last_change=None,
                           values=None, valid_last=False, opens=0, send_errors=0, started=time.monotonic())

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
        return s

    # -- one sample

    def send_record(self, mm, seq):
        r = parse_record(mm)
        valid = record_valid(r)
        errors = 0
        for target in self.raw_targets:
            try:
                self.sock.sendto(build_raw(r, seq, valid), target)
            except OSError:
                errors += 1
        vals = derived_values(r) if valid else None
        if valid and self.osc_target is not None:
            msgs, _ = build_osc(r, self.prefix)
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
        last_seq = None
        rate = 0.0
        last_sample_t = None
        while not self._stop.is_set():
            if mm is None:
                try:
                    mm = open_mmap(self.mmap_path)
                    last_seq = None
                    self._set(state="idle", opens=self._stats["opens"] + 1)
                    log.info("opened %s -> raw %s osc %s", self.mmap_path,
                             ["%s:%d" % t for t in self.raw_targets], self.osc_target)
                except OSError as e:
                    if self._stats["state"] != "no-source":
                        log.info("eye server not available (%s); retrying", e)
                    self._set(state="no-source")
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
            self._stop.wait(self.poll_s)
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
            mm = open_mmap(args.mmap)
        except OSError as e:
            sys.exit("cannot open %s: %s" % (args.mmap, e))
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
