#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Send synthetic Babble face data the way Baballonia does: one OSC message "/<name> ,f <value>" per UDP
datagram, the 45 face shapes of a frame in Baballonia's send order (spec §8.3).

It drives the SteamFrameEye module's face path without a tracker or Baballonia. With VRCFaceTracking running
and "face_source": "babble_osc" the module listens on 127.0.0.1:8888:

    python tools/send_test_face.py --port 8888                       # sweep: one shape after the other
    python tools/send_test_face.py --port 8888 --pattern still --set jawOpen=0.6 --seconds 5

--port is required on purpose: VRCFaceTracking, VRChat, Baballonia and the eye module may be using their
ports on this PC, so nothing is sent until you name one. Close Baballonia first (two senders would fight
over the same shapes). Use osc_monitor.py on a free port to see what this sends.
"""
import argparse
import math
import socket
import struct
import sys
import time

# Baballonia's send order = the rows of the module's FaceMapping (spec §8.3).
NAMES = (
    "cheekPuffLeft", "cheekPuffRight", "cheekSuckLeft", "cheekSuckRight", "jawOpen", "jawForward", "jawLeft",
    "jawRight", "noseSneerLeft", "noseSneerRight", "mouthFunnel", "mouthPucker", "mouthLeft", "mouthRight",
    "mouthRollUpper", "mouthRollLower", "mouthShrugUpper", "mouthShrugLower", "mouthClose", "mouthSmileLeft",
    "mouthSmileRight", "mouthFrownLeft", "mouthFrownRight", "mouthDimpleLeft", "mouthDimpleRight",
    "mouthUpperUpLeft", "mouthUpperUpRight", "mouthLowerDownLeft", "mouthLowerDownRight", "mouthPressLeft",
    "mouthPressRight", "mouthStretchLeft", "mouthStretchRight", "tongueOut", "tongueUp", "tongueDown",
    "tongueLeft", "tongueRight", "tongueRoll", "tongueBendDown", "tongueCurlUp", "tongueSquish", "tongueFlat",
    "tongueTwistLeft", "tongueTwistRight",
)
PATTERNS = ("sweep", "wave", "still")


def osc_string(text):
    """An OSC string: ASCII text, a NUL, NUL padding to a multiple of 4 bytes."""
    data = text.encode("ascii") + b"\0"
    return data + b"\0" * (-len(data) % 4)


def osc_float(address, value):
    """One OSC message with a single float32 argument (big-endian), as Baballonia sends it."""
    return osc_string(address) + osc_string(",f") + struct.pack(">f", value)


def frame_values(pattern, t, slot, fixed):
    """The 45 values (0..1) at time t seconds. sweep: one shape at a time rises to 1 and falls back over `slot`
    seconds, in send order; wave: every shape follows a cosine with its own phase, period `slot`; still: all 0.
    `fixed` ({row: value}) pins rows."""
    values = [0.0] * len(NAMES)
    if pattern == "sweep":
        row = int(t // slot) % len(NAMES)
        values[row] = math.sin(math.pi * ((t % slot) / slot))
    elif pattern == "wave":
        values = [0.5 - 0.5 * math.cos(2 * math.pi * (t / slot + i / len(NAMES))) for i in range(len(NAMES))]
    for row, value in fixed.items():
        values[row] = value
    return values


def _port(text):
    try:
        port = int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError("not a port number: %r" % text)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("a port is 1-65535, not %d" % port)
    return port


def _positive(text):
    value = float(text)
    if not (math.isfinite(value) and value > 0):
        raise argparse.ArgumentTypeError("must be a positive number: %r" % text)
    return value


def _seconds(text):
    value = float(text)
    if not (math.isfinite(value) and value >= 0):
        raise argparse.ArgumentTypeError("must be 0 (until Ctrl+C) or more: %r" % text)
    return value


def _pin(text):
    """NAME=VALUE for --set: a Babble shape name (any case, optional leading '/') and a finite value."""
    name, sep, value = text.partition("=")
    lower = [n.lower() for n in NAMES]
    key = name.strip().lstrip("/").lower()
    if not sep or key not in lower:
        raise argparse.ArgumentTypeError("expected NAME=VALUE with NAME one of the 45 Babble shapes, e.g. jawOpen=0.6")
    try:
        number = float(value)
    except ValueError:
        number = float("nan")
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("not a finite value: %r" % value)
    try:
        struct.pack(">f", number)       # it is sent as a float32
    except OverflowError:
        raise argparse.ArgumentTypeError("not a float32 value: %r" % value)
    return lower.index(key), number


def _prefix(text):
    if text and (not text.startswith("/") or text.endswith("/")):
        raise argparse.ArgumentTypeError("an OSC prefix starts with '/' and does not end with one, e.g. /babble")
    return text


def build_parser():
    parser = argparse.ArgumentParser(
        prog="send_test_face.py",
        description="Send Baballonia-style Babble OSC face data (45 messages per frame, one per datagram).",
        epilog="Example: python tools/send_test_face.py --port 8888 --pattern still --set jawOpen=0.6")
    parser.add_argument("--port", type=_port, required=True,
                        help="UDP port to send to (required; the SteamFrameEye module listens on 8888 by default)")
    parser.add_argument("--host", default="127.0.0.1", help="destination address (default 127.0.0.1)")
    parser.add_argument("--seconds", type=_seconds, default=10.0, help="how long to send (default 10; 0 = until Ctrl+C)")
    parser.add_argument("--rate", type=_positive, default=60.0, help="frames per second (default 60, one per inference)")
    parser.add_argument("--pattern", choices=PATTERNS, default="sweep",
                        help="sweep (default): one shape after the other; wave: all shapes; still: all 0")
    parser.add_argument("--slot", type=_positive, default=1.0, help="seconds per shape (sweep) or per cycle (wave)")
    parser.add_argument("--set", dest="pins", type=_pin, action="append", default=[], metavar="NAME=VALUE",
                        help="pin a shape to a value, e.g. jawOpen=0.6 (repeatable)")
    parser.add_argument("--prefix", type=_prefix, default="", help="OSC prefix as in Baballonia's settings (default none)")
    parser.add_argument("-q", "--quiet", action="store_true", help="print only the final line")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    fixed = dict(args.pins)
    frames_total = math.ceil(args.seconds * args.rate - 1e-9) if args.seconds > 0 else None
    addresses = [args.prefix + "/" + name for name in NAMES]
    destination = (args.host, args.port)
    if not args.quiet:
        print("sending %d Babble OSC messages per frame at %g fps to %s:%d (%s%s), %s" % (
            len(NAMES), args.rate, args.host, args.port, args.pattern,
            "".join(", %s=%g" % (NAMES[r], v) for r, v in sorted(fixed.items())),
            "for %g s" % args.seconds if frames_total else "until Ctrl+C"), flush=True)
    frames = 0
    shown = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    start = time.monotonic()
    try:
        while frames_total is None or frames < frames_total:
            t = frames / args.rate
            delay = start + t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            values = frame_values(args.pattern, t, args.slot, fixed)
            for address, value in zip(addresses, values):
                sock.sendto(osc_float(address, value), destination)
            frames += 1
            if args.pattern == "sweep" and not args.quiet:
                row = int(t // args.slot) % len(NAMES)
                if row != shown:
                    shown = row
                    print("  %5.1f s  %s" % (t, NAMES[row]), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    print("sent %d frames (%d messages) to %s:%d" % (frames, frames * len(NAMES), args.host, args.port), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
