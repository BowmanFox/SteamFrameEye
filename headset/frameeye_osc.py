#!/usr/bin/env python3
"""
frameeye_osc.py - Steam Frame eye tracking -> network (headset side).

Reads the eye-tracking server's shared-memory object /dev/shm/eye-server.mmap
(per-eye gaze direction, fixation point, eyelid openness) and forwards it:

  --raw HOST:PORT     compact binary packet per sample, for the SteamFrameEyeModule
                      VRCFaceTracking module on the PC (recommended: VRCFT stays the hub)
  --target HOST:PORT  VRCFT "v2" avatar parameters as OSC straight to VRChat
                      (use when no VRCFT is running; "none" disables)

Data layout and the OSC contract follow konsti219/frameeyeosc (Rust, MIT License,
Copyright (c) 2026 konsti219): https://github.com/konsti219/frameeyeosc
Dependency-free Python re-implementation so it runs on stock SteamOS.
"""
import argparse
import math
import mmap
import os
import socket
import struct
import sys
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


def build_osc(r, prefix):
    lx, ly = gaze_angles(r["gaze"][0])
    rx, ry = gaze_angles(r["gaze"][1])
    cx, cy = gaze_angles(r["fixation"])
    lid_l = clamp(r["openness"][0], 0.0, 1.0)
    lid_r = clamp(r["openness"][1], 0.0, 1.0)
    base = "/avatar/parameters" + prefix
    msgs = [osc_true(base + "/EyeTrackingActive")]
    for suffix, val in (("EyeLeftX", lx), ("EyeLeftY", ly), ("EyeRightX", rx), ("EyeRightY", ry),
                        ("EyeX", cx), ("EyeY", cy), ("EyeLidLeft", lid_l), ("EyeLidRight", lid_r)):
        msgs.append(osc_float(base + "/v2/" + suffix, float(val)))
    return msgs, (lx, ly, rx, ry, cx, cy, lid_l, lid_r)


def build_raw(r, seq, valid):
    return struct.pack(RAW_FMT, b"SFEY", RAW_VERSION, 1 if valid else 0, seq & 0xFFFFFFFF,
                       r["sample_time"], *r["gaze"][0], *r["gaze"][1], *r["fixation"],
                       *r["openness"], *r["cov"][0], *r["cov"][1], time.monotonic())


def parse_hostport(s):
    host, _, port = s.rpartition(":")
    return (host, int(port))


def open_mmap():
    fd = os.open(MMAP_PATH, os.O_RDONLY)
    try:
        size = os.fstat(fd).st_size
        if size < EYE_DATA_OFF + EYE_DATA_LEN:
            raise RuntimeError("unexpected mmap size %d" % size)
        return mmap.mmap(fd, size, access=mmap.ACCESS_READ)
    finally:
        os.close(fd)


def main():
    ap = argparse.ArgumentParser(description="Steam Frame eye tracking -> network")
    ap.add_argument("--raw", default="none", help="HOST:PORT for the binary feed to the VRCFT module (or 'none')")
    ap.add_argument("--target", default="none", help="HOST:PORT for direct VRChat OSC v2 parameters (or 'none')")
    ap.add_argument("--prefix", default="/FT", help="avatar parameter prefix for --target, must start with '/'")
    ap.add_argument("--poll-ms", type=float, default=4.0, help="poll interval for the sequence counter")
    ap.add_argument("--once", action="store_true", help="send the current record once and exit")
    ap.add_argument("--verbose", action="store_true", help="print the values being sent")
    args = ap.parse_args()
    if not args.prefix.startswith("/"):
        sys.exit("--prefix must start with '/'")
    raw_target = None if args.raw == "none" else parse_hostport(args.raw)
    osc_target = None if args.target == "none" else parse_hostport(args.target)
    if raw_target is None and osc_target is None:
        sys.exit("nothing to do: give --raw HOST:PORT and/or --target HOST:PORT")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print("raw ->", raw_target, " osc ->", osc_target, flush=True)

    mm = None
    last_seq = None
    last_report = 0.0
    sent = 0
    while True:
        if mm is None:
            try:
                mm = open_mmap()
                last_seq = None
                print("opened", MMAP_PATH, flush=True)
            except OSError as e:
                if args.once:
                    sys.exit("cannot open %s: %s" % (MMAP_PATH, e))
                time.sleep(2.0)
                continue
        try:
            seq = struct.unpack_from("<I", mm, SEQ_OFF)[0]
        except ValueError:
            mm = None
            continue
        if seq != last_seq or args.once:
            last_seq = seq
            r = parse_record(mm)
            valid = record_valid(r)
            if raw_target is not None:
                try:
                    sock.sendto(build_raw(r, seq, valid), raw_target)
                except OSError:
                    pass
            if valid and osc_target is not None:
                msgs, _ = build_osc(r, args.prefix)
                for m in msgs:
                    try:
                        sock.sendto(m, osc_target)
                    except OSError:
                        pass
            if valid:
                sent += 1
            if args.verbose:
                _, vals = build_osc(r, args.prefix)
                print("seq=%d valid=%d t=%.3f L=(%.3f,%.3f) R=(%.3f,%.3f) C=(%.3f,%.3f) lids=(%.2f,%.2f)"
                      % ((seq, int(valid), r["sample_time"]) + vals), flush=True)
            if args.once:
                return
        now = time.monotonic()
        if now - last_report > 60.0:
            last_report = now
            print("alive: %d valid samples sent, last seq %s" % (sent, last_seq), flush=True)
        time.sleep(args.poll_ms / 1000.0)


if __name__ == "__main__":
    main()
