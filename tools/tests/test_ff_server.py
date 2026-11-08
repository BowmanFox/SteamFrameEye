# SPDX-License-Identifier: MIT
"""The MJPEG HTTP server (spec §11.3 test_ff_server, task T4) on 127.0.0.1 and free ports only: the
exact bytes of §4.17, the peer allow-list, tokens, stream slots with same-IP eviction, connection and
request limits, the /status rate limit, a stalled client dropped alone, Linux keepalive options,
bind failures, port_owners() on a fake /proc, no token in /status, the token bucket, the placeholder
and the keepalive cadence. A second loopback source address (127.0.0.2) stands in for another peer."""
import ffutil  # first: installs the port guard

import os
import re
import shutil
import socket
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ffutil.setup_path()
from frameface import server  # noqa: E402
from frameface.clock import RealClock  # noqa: E402
from frameface.config import Config, temp_paths  # noqa: E402
from frameface.jpeg import StubJpegEncoder  # noqa: E402
from frameface.logutil import Log  # noqa: E402
from frameface.mailbox import Jpeg, Latest  # noqa: E402
from frameface.preprocess import StubPreprocessor  # noqa: E402
from frameface.server import StreamServer, peer_allowed, port_owners  # noqa: E402
from frameface.service import Service  # noqa: E402
from frameface.sim import SimClock, VftModel, make_rig  # noqa: E402

TOKEN = "Tok3n_for-tests-0123456789"
PLACEHOLDER = b"\xff\xd8\xff\xfe\x00\x0bplacehold\xff\xd9"
HEAD = (b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=frame\r\nCache-Control: no-cache, "
        b"no-store\r\nPragma: no-cache\r\nConnection: close\r\n\r\n")
KEEPALIVE = b"--frame\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nka\r\n"


def error_bytes(code, reason):
    body = reason.encode() + b"\n"
    return b"HTTP/1.1 %d %s\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (
        code, reason.encode(), len(body), body)


def jpeg(n, fill=0x11):
    return b"\xff\xd8" + bytes([fill]) * (n - 4) + b"\xff\xd9"


def connect(port, src=None, rcvbuf=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if rcvbuf:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    if src:
        s.bind((src, 0))
    s.settimeout(5)
    s.connect(("127.0.0.1", port))
    return s


def request(port, raw, src=None):
    """Send raw bytes, read the whole answer (the server closes after a non-stream response)."""
    s = connect(port, src)
    out = b""
    try:
        try:
            s.sendall(raw)
        except ConnectionError:                 # closed by the server before the request got out
            return out
        while True:
            try:
                chunk = s.recv(65536)
            except ConnectionError:             # reset/aborted: Windows' report of an immediate close
                break
            if not chunk:
                break
            out += chunk
        return out
    finally:
        s.close()


def get(port, path, src=None):
    return request(port, b"GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n" % path.encode(), src)


class Reader:
    """A multipart reader that keeps each part's raw bytes and arrival time."""

    def __init__(self, sock):
        self.sock, self.buf, self.head, self.parts, self.eof = sock, b"", None, [], False

    def pump(self, until=None, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self.eof:
            if until is not None and until(self):
                return True
            self.sock.settimeout(max(0.01, min(0.2, end - time.monotonic())))
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            except ConnectionError:
                chunk = b""
            if not chunk:
                self.eof = True
                break
            self.buf += chunk
            self._parse()
        return bool(until(self)) if until is not None else self.eof

    def _parse(self):
        if self.head is None:
            i = self.buf.find(b"\r\n\r\n")
            if i < 0:
                return
            self.head, self.buf = self.buf[:i + 4], self.buf[i + 4:]
        while True:
            i = self.buf.find(b"\r\n\r\n")
            if i < 0:
                return
            n = int(re.search(rb"Content-Length: (\d+)", self.buf[:i]).group(1))
            if len(self.buf) < i + 6 + n:
                return
            raw, self.buf = self.buf[:i + 6 + n], self.buf[i + 6 + n:]
            self.parts.append((raw, time.monotonic()))

    def kinds(self):
        out = []
        for raw, _ in self.parts:
            out.append("ka" if raw == KEEPALIVE else "placeholder" if b"X-Placeholder: 1" in raw else "live")
        return out


class ServerCase(unittest.TestCase):
    def server(self, clock=None, placeholder=PLACEHOLDER, status_fn=None, **cfg):
        cfg.setdefault("token", TOKEN)
        cfg.setdefault("allow_peers", ["127.0.0.0/8"])
        self.cfg = Config(**cfg)
        clock = clock or RealClock()
        self.jpegs = Latest(clock)
        self.log = Log("debug", None, journal=True, stream=None)
        srv = StreamServer(self.cfg, self.jpegs, status_fn or (lambda: {"v": 1, "ok": True}), clock, placeholder,
                           self.log, bind=("127.0.0.1", 0))
        srv.start()
        self.addCleanup(srv.stop)
        return srv

    def stream(self, port, path="/stream/" + TOKEN, src=None, rcvbuf=None, host=b"127.0.0.1"):
        s = connect(port, src, rcvbuf)
        self.addCleanup(s.close)
        s.sendall(b"GET %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (path.encode(), host))
        return Reader(s)

    def producer(self, size, fps, seconds):
        stop = threading.Event()

        def run():
            seq, end = 0, time.monotonic() + seconds
            while not stop.is_set() and time.monotonic() < end:
                seq += 1
                t = time.monotonic()
                self.jpegs.put(Jpeg(seq, t, int(t * 1e6), jpeg(size, seq & 0x7F)))
                stop.wait(1.0 / fps)
        th = threading.Thread(target=run, daemon=True)
        th.start()
        self.addCleanup(stop.set)
        return th


class Bytes(ServerCase):
    def test_stream_head_image_part_and_keepalive_bytes(self):
        srv = self.server(keepalive_s=0.5, placeholder=False)
        r = self.stream(srv.port)
        self.assertTrue(r.pump(lambda r: r.head is not None, 2))
        self.assertEqual(r.head, HEAD)
        data = jpeg(300)
        self.jpegs.put(Jpeg(7, 1.0, 123456, data))
        self.assertTrue(r.pump(lambda r: r.parts, 2))
        self.assertEqual(r.parts[0][0], b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 300\r\n"
                                        b"X-Frame-Seq: 7\r\nX-Timestamp-Us: 123456\r\n\r\n" + data + b"\r\n")
        self.assertTrue(r.pump(lambda r: len(r.parts) >= 2, 2))
        self.assertEqual(r.parts[1][0], KEEPALIVE)

    def test_placeholder_bytes_once_per_connection_after_placeholder_ms(self):
        srv = self.server(placeholder_ms=300, keepalive_s=0.5)
        for _ in range(2):                                  # each connection gets its own, once
            t0 = time.monotonic()
            r = self.stream(srv.port)
            self.assertTrue(r.pump(lambda r: len(r.parts) >= 5, 4))
            raw, t = r.parts[0]
            m = re.fullmatch(rb"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: (\d+)\r\nX-Frame-Seq: 0\r\n"
                             rb"X-Timestamp-Us: (\d+)\r\nX-Placeholder: 1\r\n\r\n(.*)\r\n", raw, re.S)
            self.assertIsNotNone(m, raw)
            self.assertEqual((int(m.group(1)), m.group(3)), (len(PLACEHOLDER), PLACEHOLDER))
            self.assertTrue(0.25 <= t - t0 < 0.9, t - t0)
            self.assertEqual(r.kinds(), ["placeholder", "ka", "ka", "ka", "ka"])
        self.assertEqual(srv.stats["placeholders"], 2)

    def test_no_placeholder_when_a_live_frame_came_first_or_disabled(self):
        srv = self.server(placeholder_ms=300, keepalive_s=0.5)
        r = self.stream(srv.port)
        self.assertTrue(r.pump(lambda r: r.head is not None, 2))
        self.jpegs.put(Jpeg(1, 1.0, 1, jpeg(100)))
        self.assertTrue(r.pump(lambda r: len(r.parts) >= 3, 3))
        self.assertEqual(r.kinds()[:3], ["live", "ka", "ka"])
        srv2 = self.server(placeholder=False, keepalive_s=0.5)
        r2 = self.stream(srv2.port)
        self.assertTrue(r2.pump(lambda r: len(r.parts) >= 2, 3))
        self.assertEqual(r2.kinds()[:2], ["ka", "ka"])

    def test_keepalive_cadence(self):
        srv = self.server(placeholder=False, keepalive_s=0.5)
        r = self.stream(srv.port)
        t0 = time.monotonic()
        self.assertTrue(r.pump(lambda r: len(r.parts) >= 5, 5))
        times = [t - t0 for _, t in r.parts]
        self.assertEqual(r.kinds(), ["ka"] * 5)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(0.4 <= g <= 0.75 for g in gaps), gaps)
        self.assertGreaterEqual(srv.stats["keepalives"], 4)       # counted right after each send

    def test_snap_status_and_error_bytes(self):
        clock = SimClock()
        srv = self.server(clock=clock, status_fn=lambda: {"v": 1, "state": "IDLE"})
        self.assertEqual(get(srv.port, "/snap/" + TOKEN), error_bytes(503, "Service Unavailable"))   # none yet
        data = jpeg(500)
        self.jpegs.put(Jpeg(3, clock.monotonic(), 3, data))
        self.assertEqual(get(srv.port, "/snap/" + TOKEN), b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\n"
                         b"Content-Length: 500\r\nCache-Control: no-cache, no-store\r\nConnection: close\r\n\r\n" + data)
        clock.advance(2.5)                                  # older than 2 s
        self.assertEqual(get(srv.port, "/snap/" + TOKEN), error_bytes(503, "Service Unavailable"))
        body = b'{"v":1,"state":"IDLE"}'
        self.assertEqual(get(srv.port, "/status"), b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         b"Content-Length: %d\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n" % len(body) + body)
        self.assertEqual(get(srv.port, "/nothing"), error_bytes(404, "Not Found"))
        self.assertEqual(request(srv.port, b"POST /status HTTP/1.1\r\n\r\n"), error_bytes(405, "Method Not Allowed"))
        self.assertEqual(request(srv.port, b"GET /status HTTP/1.1\r\nX-Pad: " + b"a" * 5000 + b"\r\n\r\n"),
                         error_bytes(431, "Request Header Fields Too Large"))

    def test_tokens(self):
        srv = self.server()
        self.assertEqual(get(srv.port, "/stream/wrong-token-0123456789"), error_bytes(404, "Not Found"))
        self.assertEqual(get(srv.port, "/snap/x"), error_bytes(404, "Not Found"))
        self.assertEqual(get(srv.port, "/stream"), error_bytes(404, "Not Found"))     # the token is required
        self.assertTrue(self.log.has("W http: bad token peer=127.0.0.1"))
        self.assertFalse(any(TOKEN in line or "wrong-token" in line for line in self.log.lines))
        r = self.stream(srv.port)
        self.assertTrue(r.pump(lambda r: r.head is not None, 2))
        self.assertEqual(r.head, HEAD)
        open_ = self.server(token="")                       # token "": /stream and /snap
        r2 = self.stream(open_.port, "/stream")
        self.assertTrue(r2.pump(lambda r: r.head is not None, 2))
        self.assertEqual(r2.head, HEAD)
        self.assertEqual(get(open_.port, "/snap"), error_bytes(503, "Service Unavailable"))
        self.assertEqual(get(open_.port, "/stream/" + TOKEN), error_bytes(404, "Not Found"))


class Limits(ServerCase):
    def test_allow_list(self):
        nets = ["10.35.78.0/24", "127.0.0.0/8", "192.168.4.31/32"]
        for ip, ok in (("10.35.78.28", True), ("10.35.79.1", False), ("127.0.0.1", True), ("127.9.9.9", True),
                       ("192.168.4.31", True), ("192.168.4.32", False), ("::ffff:10.35.78.5", True), ("::1", False),
                       ("not-an-ip", False)):
            self.assertEqual(peer_allowed(ip, nets), ok, ip)
        srv = self.server(allow_peers=["127.0.0.2/32"])
        self.assertEqual(get(srv.port, "/status"), b"")                 # 127.0.0.1: closed without a byte
        self.assertTrue(self.log.has("W http: rejected peer=127.0.0.1 reason=not_allowed"))
        self.assertTrue(get(srv.port, "/status", src="127.0.0.2").startswith(b"HTTP/1.1 200 OK\r\n"))

    def test_same_ip_eviction_and_503_for_another_ip(self):
        srv = self.server(max_stream_clients=2, keepalive_s=0.5, placeholder=False)
        a = self.stream(srv.port)
        self.assertTrue(a.pump(lambda r: r.head is not None, 2))
        b = self.stream(srv.port)
        self.assertTrue(b.pump(lambda r: r.head is not None, 2))
        c = self.stream(srv.port)                           # the same peer reconnects: its oldest stream goes
        self.assertTrue(c.pump(lambda r: r.head is not None, 2))
        self.assertTrue(a.pump(None, 3))                    # a: closed by the server
        self.assertTrue(self.log.has("I http: evicted stale stream peer=127.0.0.1 age_s="))
        self.jpegs.put(Jpeg(1, 1.0, 1, jpeg(64)))
        self.assertTrue(c.pump(lambda r: "live" in r.kinds(), 2))
        self.assertEqual(len(srv.clients()), 2)
        d = self.stream(srv.port, src="127.0.0.2")          # another peer while both slots are taken
        d.pump(None, 3)
        self.assertEqual(d.head + d.buf, error_bytes(503, "Service Unavailable"))
        self.assertEqual(sorted(c["peer"] for c in srv.clients()), ["127.0.0.1", "127.0.0.1"])

    def test_ninth_connection_is_closed(self):
        srv = self.server()
        idle = [connect(srv.port) for _ in range(8)]        # 8 connections that send nothing yet
        for s in idle:
            self.addCleanup(s.close)
        time.sleep(0.3)
        t0 = time.monotonic()
        ninth = connect(srv.port)
        self.addCleanup(ninth.close)
        try:
            self.assertEqual(ninth.recv(100), b"")
        except ConnectionError:
            pass
        self.assertLess(time.monotonic() - t0, 2.0)
        for s in idle:
            s.close()
        time.sleep(0.3)
        self.assertTrue(get(srv.port, "/status").startswith(b"HTTP/1.1 200 OK"))

    def test_status_rate_limit(self):
        srv = self.server()
        answers = [get(srv.port, "/status")[:15] for _ in range(11)]
        self.assertEqual(answers[:10], [b"HTTP/1.1 200 OK"] * 10)
        self.assertEqual(answers[10], b"HTTP/1.1 503 Se")
        time.sleep(1.05)
        self.assertEqual(get(srv.port, "/status")[:15], b"HTTP/1.1 200 OK")

    def test_status_table_forgets_peers_idle_for_a_second(self):         # datapath-1: one entry per peer ever seen
        srv, first = self.server(), ["127.0.%d.%d" % (1 + i // 250, 1 + i % 250) for i in range(300)]
        for src in first:                                               # 300 distinct allowed sources
            self.assertTrue(get(srv.port, "/status", src).startswith(b"HTTP/1.1 200"))
        time.sleep(1.1)
        for i in range(257):                                            # past 256 entries again: at least one sweep
            self.assertTrue(get(srv.port, "/status", "127.2.%d.%d" % (1 + i // 250, 1 + i % 250)).startswith(b"HTTP/1.1 200"))
        self.assertEqual([ip for ip in first if ip in srv._status_hits], [])  # idle for >= 1 s: gone
        now, conn = [1000.0], types.SimpleNamespace(sendall=lambda data: None)
        idle = StreamServer(Config(token=TOKEN), Latest(), dict, RealClock(), None, self.log, bind=("127.0.0.1", 0))
        with mock.patch.object(server, "time", types.SimpleNamespace(monotonic=lambda: now[0])):     # never started
            for i, t in enumerate([1000.0] * 300 + [1000.5, 1001.2]):
                now[0] = t
                idle._status(conn, "127.1.%d.%d" % (1 + i // 250, 1 + i % 250))
                if t == 1000.5:                                 # the sweep at the 257th peer kept them all (fresh) and
                    self.assertEqual(len(idle._status_hits), 301)   # the next may run only 1 s later
        self.assertEqual(len(idle._status_hits), 2)                 # then the 300 idle for >= 1 s go

    def test_status_table_sweep_rule_is_the_documented_one(self):       # B-3: the docstring said it drops every idle peer
        now, conn = [1000.0], types.SimpleNamespace(sendall=lambda data: None)
        srv = StreamServer(Config(token=TOKEN), Latest(), dict, RealClock(), None, Log("debug", None, stream=None),
                           bind=("127.0.0.1", 0))
        with mock.patch.object(server, "time", types.SimpleNamespace(monotonic=lambda: now[0])):     # never started
            for i in range(200):
                srv._status(conn, "127.1.0.%d" % (1 + i))
            now[0] = 1600.0
            srv._status(conn, "127.2.0.1")
        self.assertEqual(len(srv._status_hits), 201)                    # up to 256 peers the idle ones stay
        self.assertIn("once the table holds more than 256 peers it drops those idle for >= 1 s, at most one sweep per second",
                      " ".join(server.__doc__.split()))

    def test_host_must_be_an_address_or_this_machine(self):            # datapath-2: DNS rebinding
        srv, me = self.server(), socket.gethostname()
        hosts = (("rebind.attacker.example:%d" % srv.port, 404), ("localhost.attacker.example", 404), ("1.2.3.4.nip.io", 404),
                 ("", 404), ("127.0.0.1:%d" % srv.port, 200), ("10.35.78.1:9023", 200), ("LocalHost", 200), ("[::1]:9023", 200),
                 ("%s:9023" % me.upper(), 200), (me + ".local", 200), (None, 200))
        for i, (host, code) in enumerate(hosts):
            line = b"GET /status HTTP/1.0\r\n" if host is None else b"GET /status HTTP/1.1\r\nHost: %s\r\n" % host.encode()
            got = request(srv.port, line + b"\r\n", "127.0.0.%d" % (10 + i))
            if code == 404:
                self.assertEqual(got, error_bytes(404, "Not Found"), host)
                for path in ("/stream/" + TOKEN, "/snap/" + TOKEN):
                    self.assertEqual(request(srv.port, b"GET %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (path.encode(), host.encode()),
                                             "127.0.1.%d" % (10 + i)), error_bytes(404, "Not Found"), (host, path))
            else:
                self.assertTrue(got.startswith(b"HTTP/1.1 200 OK\r\nContent-Type: application/json"), (host, got[:60]))
        self.assertTrue(self.log.has("W http: bad host peer=127.0.0.10"))
        r = self.stream(srv.port, host=b"10.35.78.1:9023")
        self.assertTrue(r.pump(lambda r: r.head is not None, 2))
        self.assertEqual(r.head, HEAD)

    def test_a_stalled_client_is_dropped_alone(self):
        srv = self.server(stream_max_kbps=20000, max_stream_clients=2, placeholder=False)
        stalled = self.stream(srv.port, rcvbuf=4096)        # never reads after the request
        good = self.stream(srv.port)
        self.assertTrue(good.pump(lambda r: r.head is not None, 2))
        self.producer(40000, 25, 8.0)
        t0 = time.monotonic()
        self.assertTrue(good.pump(lambda r: len(srv.clients()) == 1, 7.0), srv.clients())
        dropped_after = time.monotonic() - t0
        self.assertGreaterEqual(dropped_after, 2.0)
        self.assertEqual(srv.stats["dropped"], 1)
        live = [t for (raw, t), k in zip(good.parts, good.kinds()) if k == "live" and t <= t0 + dropped_after]
        self.assertGreater(len(live), 25 * dropped_after * 0.6, (len(live), dropped_after))   # never held up
        stalled.sock.close()

    def test_token_bucket_keeps_the_rate_cap(self):
        srv = self.server(stream_max_kbps=500, placeholder=False)       # 62500 B/s
        r = self.stream(srv.port)
        self.assertTrue(r.pump(lambda r: r.head is not None, 2))
        self.producer(12000, 60, 7.5)
        r.pump(None, 1.0)                                   # the initial 64 KiB burst
        t0 = time.monotonic()
        r.pump(None, 5.0)
        got = sum(len(raw) for raw, t in r.parts if t > t0)
        self.assertLessEqual(got, 62500 * 5 * 1.05)
        self.assertGreaterEqual(got, 62500 * 5 * 0.8)
        self.assertGreater(srv.clients()[0]["skipped"], 100)

    def test_per_client_fps_cap_tolerates_jitter(self):
        srv = self.server(stream_fps_max=20, placeholder=False)
        r = self.stream(srv.port)
        self.assertTrue(r.pump(lambda r: r.head is not None, 2))
        self.producer(500, 80, 3.5)                         # 80 fps offered: about 20 fps go out
        r.pump(None, 3.0)
        live = sum(1 for k in r.kinds() if k == "live")
        self.assertTrue(40 <= live <= 64, live)             # <= 20 fps on average (+ one frame of credit)
        srv2 = self.server(stream_fps_max=60, placeholder=False)
        r2 = self.stream(srv2.port)
        self.assertTrue(r2.pump(lambda r: r.head is not None, 2))
        gaps = iter([0.010, 0.023] * 100)                   # a jittery 60 fps source: 10 ms / 23 ms gaps

        def jittery():
            for seq in range(1, 101):
                t = time.monotonic()
                self.jpegs.put(Jpeg(seq, t, int(t * 1e6), jpeg(500)))
                time.sleep(next(gaps))
        th = threading.Thread(target=jittery, daemon=True)
        th.start()
        r2.pump(lambda r: sum(1 for k in r.kinds() if k == "live") >= 100, 6.0)
        th.join(5)
        self.assertGreaterEqual(sum(1 for k in r2.kinds() if k == "live"), 90)    # not halved by the jitter

    def test_stream_socket_options_on_linux(self):
        ffutil.require_native(reason="TCP keepalive options of stream sockets")
        srv = self.server()
        r = self.stream(srv.port)
        self.assertTrue(r.pump(lambda r: r.head is not None, 2))
        s = srv._streams[0].sock
        tcp = socket.IPPROTO_TCP
        self.assertEqual(s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), 1)
        self.assertEqual([s.getsockopt(tcp, getattr(socket, n)) for n in ("TCP_KEEPIDLE", "TCP_KEEPINTVL",
                                                                           "TCP_KEEPCNT", "TCP_USER_TIMEOUT")],
                         [5, 2, 2, 5000])
        self.assertEqual(s.getsockopt(tcp, socket.TCP_NODELAY), 1)


class BindAndOwners(ServerCase):
    def test_start_on_a_port_in_use_raises(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(holder.close)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        srv = StreamServer(Config(token=TOKEN), Latest(), dict, RealClock(), None, Log("info", None, stream=None),
                           bind=("127.0.0.1", port))
        with self.assertRaises(OSError):
            srv.start()
        self.assertIsNone(srv.port)
        holder.close()
        self.assertEqual(srv.start(), port)                 # nothing was left open by the failure
        srv.stop()

    def test_port_owners_on_a_fake_proc(self):
        root = tempfile.mkdtemp(prefix="ffproc-")
        self.addCleanup(shutil.rmtree, root, True)
        head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        row = "   %d: %s:%04X 00000000:0000 %s 00000000:00000000 00:00000000 00000000  1000        0 %d 1 0 100 0 0 10 0\n"
        os.makedirs(os.path.join(root, "net"))
        with open(os.path.join(root, "net", "tcp"), "w") as f:
            f.write(head + row % (0, "0100007F", 23456, "0A", 111) + row % (1, "00000000", 23456, "01", 333) +
                    row % (2, "00000000", 23457, "0A", 444))
        with open(os.path.join(root, "net", "tcp6"), "w") as f:
            f.write(head + row % (0, "00000000000000000000000000000000", 23456, "0A", 222))
        for pid, comm, inode in ((4242, "holder", 111), (5151, "other", 222), (6000, "client", 333),
                                 (7000, "elsewhere", 444), (8000, "nofd", 0)):
            fd = os.path.join(root, str(pid), "fd")
            os.makedirs(fd)
            with open(os.path.join(root, str(pid), "comm"), "w") as f:
                f.write(comm + "\n")
            target = "socket:[%d]" % inode
            link = os.path.join(fd, "5")
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError):
                with open(link, "w") as f:
                    f.write(target)
        self.assertEqual(port_owners(23456, root), [(4242, "holder"), (5151, "other")])
        self.assertEqual(port_owners(23457, root), [(7000, "elsewhere")])
        self.assertEqual(port_owners(1, root), [])
        self.assertEqual(port_owners(23456, os.path.join(root, "missing")), [])

    def test_status_of_the_service_never_contains_the_token(self):
        rig = make_rig(VftModel())
        self.addCleanup(rig.close)
        paths = temp_paths(tempfile.mkdtemp(prefix="ffsrv-"))
        self.addCleanup(shutil.rmtree, os.path.dirname(paths.state_dir), True)
        svc = Service(Config(token=TOKEN), paths, sys=rig.kernel, clock=rig.clock, sys_root=rig.sys_root,
                      dev_root=rig.dev_root, proc_root=rig.proc_root, encoder=StubJpegEncoder(),
                      pre=StubPreprocessor(), worn=lambda: True, threaded=False, http=True,
                      log=Log("info", paths.events, stream=None, clock=rig.clock), bind=("127.0.0.1", 0))
        svc.client_override = 1
        self.addCleanup(svc.stop)
        for _ in range(500):
            svc.step(0.01)
        self.assertEqual(svc.state, "STREAMING")
        body = get(svc.http_port, "/status")
        self.assertTrue(body.startswith(b"HTTP/1.1 200 OK\r\n"))
        self.assertIn(b'"state":"STREAMING"', body)
        self.assertNotIn(TOKEN.encode(), body)
        with open(paths.events, "rb") as f:
            self.assertNotIn(TOKEN.encode(), f.read())


if __name__ == "__main__":
    unittest.main()
