#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Print the OSC messages that arrive on a local UDP port: Baballonia's face data, the module's status keeper
bools, or what send_test_face.py sends.

    python tools/osc_monitor.py --port 9999                       # one line per message, summary at the end
    python tools/osc_monitor.py --port 9999 --json --seconds 5    # JSON lines (one per message), then a summary

--port is required: VRCFaceTracking (8888, 9001), VRChat (9000), Baballonia (8889) and the SteamFrameEye
module (9021) may be holding their ports on this PC. To watch Baballonia, point its OSC Send Port at the port
you monitor while VRCFaceTracking is closed (and set it back to 8888 afterwards); to watch the status keeper,
set "status_keeper_port" to it.
"""
import argparse
import json
import math
import socket
import struct
import sys
import time


class OscError(ValueError):
    """A malformed OSC packet."""


def _string(data, pos):
    """An OSC string at pos: (text, position after its NUL padding)."""
    end = data.find(b"\0", pos)
    if end < 0:
        raise OscError("unterminated string at byte %d" % pos)
    nxt = end + 1 + (-(end + 1 - pos) % 4)
    if nxt > len(data) or any(data[end:nxt]):
        raise OscError("bad string padding at byte %d" % pos)
    try:
        return data[pos:end].decode("ascii"), nxt
    except UnicodeDecodeError:
        raise OscError("non-ASCII string at byte %d" % pos)


def _take(data, pos, size):
    if pos + size > len(data):
        raise OscError("truncated argument at byte %d" % pos)
    return data[pos:pos + size], pos + size


def _message(data):
    address, pos = _string(data, 0)
    if not address.startswith("/"):
        raise OscError("address does not start with '/'")
    if pos == len(data):
        raise OscError("no type tag string")
    tags, pos = _string(data, pos)
    if not tags.startswith(","):
        raise OscError("type tag string does not start with ','")
    args = []
    for tag in tags[1:]:
        if tag == "f":
            raw, pos = _take(data, pos, 4)
            args.append(struct.unpack(">f", raw)[0])
        elif tag == "i":
            raw, pos = _take(data, pos, 4)
            args.append(struct.unpack(">i", raw)[0])
        elif tag == "d":
            raw, pos = _take(data, pos, 8)
            args.append(struct.unpack(">d", raw)[0])
        elif tag == "h":
            raw, pos = _take(data, pos, 8)
            args.append(struct.unpack(">q", raw)[0])
        elif tag == "s":
            text, pos = _string(data, pos)
            args.append(text)
        elif tag == "b":
            raw, pos = _take(data, pos, 4)
            size = struct.unpack(">i", raw)[0]
            if size < 0:
                raise OscError("negative blob size")
            blob, pos = _take(data, pos, size + (-size % 4))
            args.append(blob[:size].hex())
        elif tag in "TF":
            args.append(tag == "T")
        elif tag in "NI":
            args.append(None)
        else:
            raise OscError("unsupported type tag %r" % tag)
    if pos != len(data):
        raise OscError("%d bytes after the arguments" % (len(data) - pos))
    return address, tags[1:], args


def parse_packet(data, max_depth=2, _level=0):
    """[(address, type tags without the comma, [arguments])] of one datagram: a message or a bundle
    ("#bundle\\0", 8-byte time tag, elements [int32 size][content]) nested at most `max_depth` levels."""
    data = bytes(data)
    if not data.startswith(b"#bundle\0"):
        return [_message(data)]
    if _level + 1 > max_depth:
        raise OscError("bundles nested deeper than %d" % max_depth)
    if len(data) < 16:
        raise OscError("bundle without time tag")
    messages, pos = [], 16
    while pos < len(data):
        raw, pos = _take(data, pos, 4)
        size = struct.unpack(">i", raw)[0]
        if size <= 0 or size % 4:
            raise OscError("bad bundle element size %d" % size)
        element, pos = _take(data, pos, size)
        messages.extend(parse_packet(element, max_depth, _level + 1))
    return messages


def _value_text(value):
    if isinstance(value, float):
        return "%.6g" % value if math.isfinite(value) else str(value)
    return json.dumps(value)


def format_message(address, tags, args):
    return "%s ,%s %s" % (address, tags, " ".join(_value_text(a) for a in args))


class Monitor:
    """A UDP listener on host:port that parses every datagram as OSC."""

    def __init__(self, host, port):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 22)
        except OSError:
            pass
        try:
            self.sock.bind((host, port))
        except OSError:
            self.sock.close()
            raise
        self.address = self.sock.getsockname()
        self.messages = self.datagrams = self.malformed = 0
        self.counts = {}

    def close(self):
        self.sock.close()

    def run(self, seconds=0.0, count=0, emit=None):
        """Receive until `seconds` passed (0 = no limit) or `count` messages arrived (0 = no limit). emit(record)
        gets one dict per message ({"t", "from", "address", "types", "args"}) or malformed datagram ({"t", "from",
        "error"}); t is seconds since run() started."""
        start = time.monotonic()
        self.sock.settimeout(0.1)
        while True:
            now = time.monotonic() - start
            if (seconds and now >= seconds) or (count and self.messages >= count):
                return
            try:
                data, peer = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue        # Windows reports an earlier ICMP "port unreachable" here; not ours to handle
            t = round(time.monotonic() - start, 6)
            self.datagrams += 1
            try:
                parsed = parse_packet(data)
            except OscError as e:
                self.malformed += 1
                if emit:
                    emit({"t": t, "from": peer[1], "error": "%s (%d bytes)" % (e, len(data))})
                continue
            for address, tags, args in parsed:
                self.messages += 1
                self.counts[address] = self.counts.get(address, 0) + 1
                if emit:
                    emit({"t": t, "from": peer[1], "address": address, "types": tags, "args": args})

    def summary(self, seconds):
        top = sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        return {"messages": self.messages, "datagrams": self.datagrams, "malformed": self.malformed,
                "addresses": len(self.counts), "seconds": round(seconds, 3),
                "top": [{"address": a, "count": n, "per_s": round(n / seconds, 1) if seconds > 0 else None} for a, n in top]}


def _port(text):
    try:
        port = int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError("not a port number: %r" % text)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("a port is 1-65535, not %d" % port)
    return port


def _limit(kind):
    def parse(text):
        value = kind(text)
        if not (math.isfinite(value) and value >= 0):
            raise argparse.ArgumentTypeError("must be 0 (no limit) or more: %r" % text)
        return value
    return parse


def build_parser():
    parser = argparse.ArgumentParser(
        prog="osc_monitor.py", description="Print the OSC messages that arrive on a local UDP port.",
        epilog="Example: python tools/osc_monitor.py --port 9999 --seconds 10")
    parser.add_argument("--port", type=_port, required=True, help="UDP port to listen on (required; pick a free one)")
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    parser.add_argument("--seconds", type=_limit(float), default=0.0, help="stop after this many seconds (default 0 = Ctrl+C)")
    parser.add_argument("--count", type=_limit(int), default=0, help="stop after this many messages (default 0 = no limit)")
    parser.add_argument("--json", action="store_true", help="one JSON object per line instead of text")
    parser.add_argument("-q", "--quiet", action="store_true", help="print only the summary")
    return parser


def main(argv=None, out=None):
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        monitor = Monitor(args.host, args.port)
    except OSError as e:
        print("cannot listen on %s:%d (%s); is another program (VRCFaceTracking, Baballonia, VRChat) using it?"
              % (args.host, args.port, e), file=sys.stderr)
        return 1

    def emit(record):
        if args.quiet:
            return
        if args.json:
            out.write(json.dumps(record) + "\n")
        elif "error" in record:
            out.write("%9.3f  from %d  malformed: %s\n" % (record["t"], record["from"], record["error"]))
        else:
            out.write("%9.3f  from %d  %s\n" % (record["t"], record["from"],
                                                format_message(record["address"], record["types"], record["args"])))
        out.flush()

    print("listening on %s:%d (Ctrl+C to stop)" % monitor.address, file=sys.stderr, flush=True)
    start = time.monotonic()
    try:
        monitor.run(args.seconds, args.count, emit)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.close()
    summary = monitor.summary(time.monotonic() - start)
    if args.json:
        out.write(json.dumps({"summary": summary}) + "\n")
    else:
        out.write("%d messages in %d datagrams (%d malformed), %d addresses in %.1f s%s\n" % (
            summary["messages"], summary["datagrams"], summary["malformed"], summary["addresses"], summary["seconds"],
            "".join("; %s %s/s" % (t["address"], t["per_s"]) for t in summary["top"][:1])))
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
