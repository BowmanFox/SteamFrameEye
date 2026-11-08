# SPDX-License-Identifier: MIT
"""The MJPEG HTTP server of the face stream (spec §4.17), stdlib sockets, one thread per connection.

Routes: /stream/<token> (multipart/x-mixed-replace), /snap/<token> (the newest live JPEG if it is at
most 2 s old), /status (JSON of the service); with token "" the paths are /stream and /snap. Peers
outside allow_peers are closed without a byte right after accept(); at most 8 connections and
max_stream_clients streams (a new stream from an IP that already holds one evicts that IP's oldest
stream, else 503). Requests: GET only (405), at most 4096 bytes up to the blank line (431), 5 s to
arrive; a Host header must name this machine (an IP literal, localhost or its own host name, with
or without .local; no Host is fine), else 404 on every route, so a web page that rebinds its own
DNS name to an allowed address reads nothing; /status at most 10 requests/s per IP (503; once the
table holds more than 256 peers it drops those idle for >= 1 s, at most one sweep per second);
unknown paths and wrong tokens 404.

Stream loop: the newest live JPEG newer than the last one sent, if the shared TokenBucket
(stream_max_kbps) allows it and the client's 1/stream_fps_max slot has come (else it is skipped and
counted; the slots follow a schedule with one frame of credit, so a jittery 60 fps source is not
halved while the average stays at most stream_fps_max); one
placeholder JPEG if no live part was sent placeholder_ms after connecting; a keepalive part after
keepalive_s without any part. Stream sockets get TCP_NODELAY, a 64 KiB send buffer, IP_TOS (Linux),
keepalive 5/2/2 and TCP_USER_TIMEOUT 5000 where the platform has them, and a 2 s send timeout, so a
dead or stalled peer is dropped alone. Nothing reads from a stream socket after the request.
"""
import collections
import hmac
import ipaddress
import json
import math
import os
import socket
import sys
import threading
import time

from .policy import TokenBucket

__all__ = ["StreamServer", "peer_allowed", "host_allowed", "port_owners", "STREAM_HEAD", "KEEPALIVE", "image_part",
           "error_response", "status_response", "snap_response"]

STREAM_HEAD = (b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=frame\r\n"
               b"Cache-Control: no-cache, no-store\r\nPragma: no-cache\r\nConnection: close\r\n\r\n")
KEEPALIVE = b"--frame\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nka\r\n"
REASONS = {404: "Not Found", 405: "Method Not Allowed", 431: "Request Header Fields Too Large",
           503: "Service Unavailable"}
MAX_CONNECTIONS = 8
REQUEST_CAP = 4096
READ_TIMEOUT_S = 5.0
SEND_TIMEOUT_S = 2.0
STATUS_PER_S = 10
SNAP_MAX_AGE_S = 2.0
BURST = 65536


def image_part(data: bytes, seq: int, t_us: int, placeholder: bool = False) -> bytes:
    head = "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\nX-Frame-Seq: %d\r\nX-Timestamp-Us: %d\r\n" % (
        len(data), 0 if placeholder else seq, t_us)
    if placeholder:
        head += "X-Placeholder: 1\r\n"
    return head.encode("ascii") + b"\r\n" + data + b"\r\n"


def error_response(code: int) -> bytes:
    reason = REASONS[code]
    body = (reason + "\n").encode("ascii")
    return ("HTTP/1.1 %d %s\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % (
        code, reason, len(body))).encode("ascii") + body


def status_response(body: bytes) -> bytes:
    return ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nCache-Control: no-cache\r\n"
            "Connection: close\r\n\r\n" % len(body)).encode("ascii") + body


def snap_response(data: bytes) -> bytes:
    return ("HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\nCache-Control: no-cache, no-store\r\n"
            "Connection: close\r\n\r\n" % len(data)).encode("ascii") + data


def _networks(nets):
    out = []
    for n in nets:
        try:
            out.append(n if isinstance(n, ipaddress.IPv4Network) else ipaddress.IPv4Network(str(n), strict=False))
        except ValueError:
            continue
    return out


def peer_allowed(addr: str, nets: list) -> bool:
    """True if the IPv4 address `addr` (an IPv4-mapped IPv6 address counts as IPv4) lies in one of
    the CIDRs `nets` (strings or IPv4Network)."""
    try:
        ip = ipaddress.ip_address(str(addr).split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6:
        if ip.ipv4_mapped is None:
            return False
        ip = ip.ipv4_mapped
    return any(ip in net for net in _networks(nets))


def host_allowed(value: str) -> bool:
    """True if a Host header value names this machine: an IPv4 or IPv6 literal (port and [] stripped),
    "localhost", or socket.gethostname() with or without ".local" (case-insensitive). An address
    cannot be rebound; any other DNS name may be a web page's own name pointed at this address."""
    v = value.strip()
    if v.startswith("["):                       # [IPv6]:port
        host = v[1:v.find("]")] if "]" in v else ""
    else:
        host = v.rsplit(":", 1)[0] if v.count(":") == 1 else v
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return True
    except ValueError:
        pass
    name, me = host.rstrip(".").lower(), socket.gethostname().rstrip(".").lower()
    me = me[:-len(".local")] if me.endswith(".local") else me
    return name == "localhost" or (bool(me) and name in (me, me + ".local"))


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def _fd_target(path):
    if os.path.islink(path):
        try:
            return os.readlink(path)
        except OSError:
            return None
    text = _read(path)                          # fake /proc trees: a file holding the link target
    return text.strip() if text else None


def port_owners(port: int, proc_root: str = "/proc") -> list:
    """[(pid, comm)] of the processes with a LISTEN socket on TCP `port`: the socket inodes of the
    LISTEN rows (state 0A) of <proc>/net/tcp and tcp6, then the /proc/<pid>/fd links socket:[inode]."""
    inodes = set()
    for name in ("tcp", "tcp6"):
        text = _read(os.path.join(proc_root, "net", name))
        for line in (text or "").splitlines()[1:]:
            f = line.split()
            if len(f) < 10 or f[3] != "0A":
                continue
            try:
                local_port = int(f[1].rsplit(":", 1)[1], 16)
            except (IndexError, ValueError):
                continue
            if local_port == port and f[9] != "0":
                inodes.add("socket:[%s]" % f[9])
    if not inodes:
        return []
    try:
        pids = sorted((p for p in os.listdir(proc_root) if p.isdigit()), key=int)
    except OSError:
        return []
    out = []
    for pid in pids:
        fd_dir = os.path.join(proc_root, pid, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        if any(_fd_target(os.path.join(fd_dir, fd)) in inodes for fd in fds):
            out.append((int(pid), (_read(os.path.join(proc_root, pid, "comm")) or "?").strip() or "?"))
    return out


def _setopt(sock, level, name, value):
    if name is None:
        return
    try:
        sock.setsockopt(level, name, value)
    except OSError:
        pass


class _Stream:
    __slots__ = ("sock", "peer", "since", "sent", "skipped", "closed")

    def __init__(self, sock, peer, since):
        self.sock, self.peer, self.since = sock, peer, since
        self.sent = self.skipped = 0
        self.closed = False


class StreamServer:
    def __init__(self, cfg, jpegs, status_fn, clock, placeholder, log, bind=("0.0.0.0", 9023)):
        self.cfg, self.jpegs, self.status_fn, self.clock, self.log = cfg, jpegs, status_fn, clock, log
        self.placeholder = placeholder          # bytes of a decodable black JPEG, or None
        self.bind = tuple(bind)
        self.nets = _networks(cfg.allow_peers)
        self.bucket = TokenBucket(cfg.stream_max_kbps * 125.0, BURST)
        self.port = None
        self.stats = collections.Counter(parts=0, placeholders=0, keepalives=0, bytes=0, rejected=0, bad_token=0,
                                         bad_host=0, evicted=0, refused=0, dropped=0)
        self._lock = threading.Lock()
        self._sock = self._thread = None
        self._stopping = threading.Event()
        self._conns = set()
        self._streams = []                      # stream clients, oldest first
        self._status_hits = {}                  # peer -> deque of its /status times within the last second
        self._status_swept = -math.inf
        self._window = collections.deque()      # (t, bytes, live) of the last second

    # -- lifecycle

    def start(self) -> int:
        """Bind and listen (OSError on failure; nothing is left open), then serve in a thread."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if os.name == "nt":                 # Windows' SO_REUSEADDR would share a port in use
                _setopt(sock, socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", None), 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(self.bind)
            sock.listen(16)
            sock.settimeout(0.2)
        except OSError:
            sock.close()
            raise
        self._stopping.clear()
        self._sock, self.port = sock, sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, name="http-accept", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        self._stopping.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        with self._lock:
            conns = list(self._conns)
        for c in conns:
            self._close(c)
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(1.0)

    def clients(self) -> list:
        now = self.clock.monotonic()
        with self._lock:
            return [{"peer": s.peer, "since_s": round(now - s.since, 1), "sent": s.sent, "skipped": s.skipped}
                    for s in self._streams]

    def stream_count(self) -> int:
        with self._lock:
            return len(self._streams)

    def rates(self) -> dict:
        """kbps (every stream byte) and fps_out (live image parts) over the last second."""
        now = self.clock.monotonic()
        with self._lock:
            while self._window and now - self._window[0][0] > 1.0:
                self._window.popleft()
            nbytes = sum(b for _, b, _ in self._window)
            live = sum(1 for _, _, x in self._window if x)
        return {"kbps": int(round(nbytes * 8 / 1000.0)), "fps_out": float(live)}

    # -- connections

    @staticmethod
    def _close(conn):
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass

    def _accept_loop(self):
        sock = self._sock
        while not self._stopping.is_set():
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    return
                self._stopping.wait(0.05)
                continue
            peer = addr[0]
            if not peer_allowed(peer, self.nets):
                self.stats["rejected"] += 1
                self.log.w("http", "rejected", peer=peer, reason="not_allowed")
                conn.close()                    # not a single byte
                continue
            with self._lock:
                full = len(self._conns) >= MAX_CONNECTIONS
                if not full:
                    self._conns.add(conn)
            if full:
                self.stats["refused"] += 1
                self._close(conn)
                continue
            threading.Thread(target=self._serve, args=(conn, peer), name="http-conn", daemon=True).start()

    def _serve(self, conn, peer):
        try:
            route = self._request(conn, peer)
            if route is not None:
                route[0](conn, peer, *route[1:])
        except OSError:
            pass
        finally:
            with self._lock:
                self._conns.discard(conn)
            self._close(conn)

    def _request(self, conn, peer):
        """The handler for this request, or None after an error response was sent."""
        deadline = time.monotonic() + READ_TIMEOUT_S
        buf = b""
        while True:
            end = buf.find(b"\r\n\r\n")
            if end >= 0:
                if end + 4 > REQUEST_CAP:
                    conn.sendall(error_response(431))
                    return None
                break
            if len(buf) > REQUEST_CAP:
                conn.sendall(error_response(431))
                return None
            rest = deadline - time.monotonic()
            if rest <= 0:
                return None
            conn.settimeout(rest)
            try:
                chunk = conn.recv(1024)
            except socket.timeout:
                return None
            if not chunk:
                return None
            buf += chunk
        lines = buf[:buf.find(b"\r\n\r\n")].decode("latin-1").split("\r\n")
        parts = lines[0].split()
        if len(parts) != 3 or parts[0] != "GET" or not parts[2].startswith("HTTP/1."):
            conn.sendall(error_response(405))
            return None
        hosts = [value for name, _, value in (h.partition(":") for h in lines[1:]) if name.strip().lower() == "host"]
        if not all(host_allowed(h) for h in hosts):     # every route: a rebound DNS name gets nothing, not even /status
            self.stats["bad_host"] += 1
            self.log.w("http", "bad host", peer=peer)   # never the value (any page can choose it)
            conn.sendall(error_response(404))
            return None
        path = parts[1].split("?", 1)[0]
        if path == "/status":
            return (self._status,)
        tok = self.cfg.token
        for name, handler in (("stream", self._stream), ("snap", self._snap)):
            base = "/" + name
            if tok == "":
                if path == base:
                    return (handler,)
            elif path == base or path.startswith(base + "/"):
                given = path[len(base) + 1:].encode("latin-1", "replace")
                if path.startswith(base + "/") and hmac.compare_digest(given, tok.encode("ascii")):
                    return (handler,)
                self.stats["bad_token"] += 1
                self.log.w("http", "bad token", peer=peer)      # never the path or the token
                break
        conn.sendall(error_response(404))
        return None

    def _status(self, conn, peer):
        now = time.monotonic()
        with self._lock:
            hits = self._status_hits.setdefault(peer, collections.deque())
            while hits and now - hits[0] >= 1.0:
                hits.popleft()
            limited = len(hits) >= STATUS_PER_S
            if not limited:
                hits.append(now)
            if len(self._status_hits) > 256 and now - self._status_swept >= 1.0:   # O(n) under the lock: once a second
                self._status_swept = now
                for ip in [ip for ip, h in self._status_hits.items() if not h or now - h[-1] >= 1.0]:
                    del self._status_hits[ip]               # idle for 1 s: its rate window is empty anyway
        if limited:
            conn.sendall(error_response(503))
            return
        try:
            body = json.dumps(self.status_fn(), separators=(",", ":")).encode("utf-8")
        except Exception as e:                  # a broken status document must not kill the server
            self.log.e("http", "status failed", error=repr(e))
            conn.sendall(error_response(503))
            return
        conn.sendall(status_response(body))

    def _snap(self, conn, peer):
        got = self.jpegs.latest()
        if got is None or self.clock.monotonic() - got[2] > SNAP_MAX_AGE_S:
            conn.sendall(error_response(503))
            return
        conn.sendall(snap_response(got[1].data))

    # -- streams

    def _admit(self, conn, peer):
        """A _Stream for this connection, evicting the peer's oldest stream if the slots are full;
        None (503) when they are full with other peers' streams."""
        now = self.clock.monotonic()
        victim = None
        with self._lock:
            if len(self._streams) >= self.cfg.max_stream_clients:
                same = [s for s in self._streams if s.peer == peer]
                if not same:
                    return None
                victim = same[0]
                self._streams.remove(victim)
                victim.closed = True
            st = _Stream(conn, peer, now)
            self._streams.append(st)
        if victim is not None:
            self.stats["evicted"] += 1
            self.log.i("http", "evicted stale stream", peer=peer, age_s=round(now - victim.since, 1))
            self._close(victim.sock)
        return st

    def _tune(self, conn):
        _setopt(conn, socket.IPPROTO_TCP, getattr(socket, "TCP_NODELAY", None), 1)
        _setopt(conn, socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
        if sys.platform.startswith("linux"):
            _setopt(conn, socket.IPPROTO_IP, getattr(socket, "IP_TOS", None), int(self.cfg.ip_tos))
        _setopt(conn, socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", 5), ("TCP_KEEPINTVL", 2), ("TCP_KEEPCNT", 2), ("TCP_USER_TIMEOUT", 5000)):
            _setopt(conn, socket.IPPROTO_TCP, getattr(socket, name, None), value)
        conn.settimeout(SEND_TIMEOUT_S)

    def _send(self, st, data, live=False, kind=None):
        st.sock.sendall(data)
        now = self.clock.monotonic()
        with self._lock:
            self.stats["bytes"] += len(data)
            if kind:
                self.stats[kind] += 1
            w = self._window
            w.append((now, len(data), live))
            while w and now - w[0][0] > 1.0:
                w.popleft()

    def _stream(self, conn, peer):
        st = self._admit(conn, peer)
        if st is None:
            self.stats["refused"] += 1
            conn.sendall(error_response(503))
            return
        self._tune(conn)
        self.log.i("http", "client connected", peer=peer, route="stream")
        cfg, clock = self.cfg, self.clock
        interval = 1.0 / cfg.stream_fps_max
        placeholder_due = st.since + cfg.placeholder_ms / 1000.0
        want_placeholder = bool(cfg.placeholder and self.placeholder)
        last_seq = self.jpegs.seq               # only frames published after connecting
        last_any, due = st.since, -math.inf     # due: when the next live part may go (1/stream_fps_max)
        live_sent = False
        try:
            self._send(st, STREAM_HEAD)
            while not st.closed and not self._stopping.is_set():
                now = clock.monotonic()
                wake = last_any + cfg.keepalive_s
                if want_placeholder and not live_sent:
                    wake = min(wake, placeholder_due)
                got = self.jpegs.wait_newer(last_seq, min(0.25, max(0.005, wake - now)))
                now = clock.monotonic()
                if got is not None and not st.closed:
                    last_seq, jpg = got
                    if now >= due - 0.001 and self.bucket.allow(now, len(jpg.data)):
                        self._send(st, image_part(jpg.data, jpg.seq, jpg.t_us), live=True, kind="parts")
                        st.sent += 1
                        last_any, live_sent = now, True
                        due = max(due + interval, now - interval)   # one frame of credit for jitter
                    else:
                        st.skipped += 1
                if want_placeholder and not live_sent and now >= placeholder_due:
                    want_placeholder = False
                    self._send(st, image_part(self.placeholder, 0, int(now * 1e6), placeholder=True),
                               kind="placeholders")
                    last_any = now
                if now - last_any >= cfg.keepalive_s:
                    self._send(st, KEEPALIVE, kind="keepalives")
                    last_any = now
        except (OSError, ValueError):
            self.stats["dropped"] += 1          # send timeout or a dead peer: this client only
        finally:
            with self._lock:
                if st in self._streams:
                    self._streams.remove(st)
            self.log.i("http", "client gone", peer=peer, seconds=round(clock.monotonic() - st.since, 1),
                       sent=st.sent, skipped=st.skipped)
