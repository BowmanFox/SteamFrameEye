# SPDX-License-Identifier: MIT
"""Proof of the shared FrameFace test helpers (spec §11.2, §11.3 test_ff_util, task T0).

The port guard is proven only on free ephemeral loopback ports that a test forbids itself with
ffutil.extra_forbidden(), each with a bound, non-blocking receiver that must stay empty, and
through the pure ffutil.forbidden() predicate. No line here hands a port of
FORBIDDEN_TEST_PORTS to a socket method, not even to show that the call fails; the source scan
at the end keeps it that way for every test_ff_* file.
"""
import ffutil  # first: installs and self-checks the port guard before any socket exists

import _socket
import ast
import contextlib
import gc
import io
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import tokenize
import unittest
from unittest import mock
import warnings

TESTS = Path(__file__).resolve().parent
SPEC_PORTS = frozenset({8888, 8889, 9000, 9001, 9015, 9020, 9021})   # §11.2, plain values
HAS_SENDMSG = hasattr(socket.socket, "sendmsg")                       # not on Windows
UDP, TCP = socket.SOCK_DGRAM, socket.SOCK_STREAM


class SocketTest(unittest.TestCase):
    def sock(self, socktype, family=socket.AF_INET):
        sock = socket.socket(family, socktype)
        self.addCleanup(sock.close)
        return sock

    def bound(self, socktype, family=socket.AF_INET, host="127.0.0.1"):
        """A bound, non-blocking UDP receiver or TCP listener on a free ephemeral port."""
        for _ in range(20):
            sock = self.sock(socktype, family)
            sock.bind((host, 0))
            port = sock.getsockname()[1]
            if port not in ffutil.FORBIDDEN_TEST_PORTS:
                break
        else:
            self.fail("the OS offered only forbidden ports")
        if socktype == TCP:
            sock.listen(8)
        sock.setblocking(False)
        return sock, port

    def blocked(self, call, port):
        with self.assertRaises(AssertionError) as cm:
            call()
        self.assertEqual(str(cm.exception), "forbidden port %d" % port)

    def quiet(self, sock):
        """Nothing arrived: no datagram at a receiver, no pending connection at a listener."""
        self.assertEqual(select.select([sock], [], [], 0.2)[0], [])
        with self.assertRaises(BlockingIOError):
            sock.recv(2048) if sock.type == UDP else sock.accept()

    def arrival(self, sock):
        """The next datagram of a receiver or the next connection of a listener, within 5 s."""
        self.assertEqual(select.select([sock], [], [], 5.0)[0], [sock], "nothing arrived in 5 s")
        if sock.type == UDP:
            return sock.recv(2048)
        conn, _ = sock.accept()
        self.addCleanup(conn.close)
        conn.settimeout(5.0)
        return conn


class ForbiddenPredicate(unittest.TestCase):
    def test_the_set_is_the_seven_test_ports(self):
        self.assertIsInstance(ffutil.FORBIDDEN_TEST_PORTS, frozenset)
        self.assertEqual(ffutil.FORBIDDEN_TEST_PORTS, SPEC_PORTS)

    def test_returns_the_port_for_ipv4_ipv6_and_host_name_tuples(self):
        for port in sorted(ffutil.FORBIDDEN_TEST_PORTS):
            index_only = type("IndexOnly", (), {"__index__": lambda _, p=port: p})()   # usable as a port
            for address in (("127.0.0.1", port), ("0.0.0.0", port), ("192.0.2.10", port), ("", port),
                            ("::1", port), ("::1", port, 0, 0), ("fe80::1%eth0", port, 0, 2),
                            ("localhost", port), ("example.invalid", port), (b"127.0.0.1", port),
                            ("127.0.0.1", index_only)):
                with self.subTest(address=address):
                    result = ffutil.forbidden(address)
                    self.assertEqual(result, port)
                    self.assertIs(type(result), int)

    def test_returns_none_for_other_ports(self):
        for port in (0, 1, 80, 8887, 8890, 8999, 9002, 9014, 9016, 9019, 9022, 9023, 18888, 19000, 65535,
                     ffutil.free_port("udp")):
            for address in (("127.0.0.1", port), ("::1", port, 0, 0), ("localhost", port)):
                with self.subTest(address=address):
                    self.assertIsNone(ffutil.forbidden(address))

    def test_returns_none_for_non_tuples(self):
        for port in sorted(ffutil.FORBIDDEN_TEST_PORTS):
            for address in (port, str(port), "127.0.0.1:%d" % port, ["127.0.0.1", port], {"port": port},
                            "/run/user/1000/ff.sock", b"\0abstract", None, (), (port,), ("127.0.0.1",),
                            ("127.0.0.1", str(port)), ("127.0.0.1", float(port)), ("127.0.0.1", None)):
                with self.subTest(address=address):
                    self.assertIsNone(ffutil.forbidden(address))

    def test_is_pure(self):
        refuse = mock.Mock(side_effect=AssertionError("forbidden() must not touch a socket"))
        with mock.patch.object(socket, "socket", refuse), mock.patch.object(socket, "getaddrinfo", refuse), \
                mock.patch.object(socket, "gethostbyname", refuse):
            for port in sorted(ffutil.FORBIDDEN_TEST_PORTS):
                self.assertEqual(ffutil.forbidden(("localhost", port)), port)
            self.assertIsNone(ffutil.forbidden(("localhost", 9022)))
        refuse.assert_not_called()

    def test_extra_forbidden_adds_ports_for_the_block_only(self):
        p, q = ffutil.free_port("udp"), ffutil.free_port("tcp")
        self.assertIsNone(ffutil.forbidden(("127.0.0.1", p)))
        with ffutil.extra_forbidden({p}) as added:
            self.assertEqual(added, frozenset({p}))
            for address in (("127.0.0.1", p), ("::1", p, 0, 0), ("localhost", p)):
                self.assertEqual(ffutil.forbidden(address), p)
            with ffutil.extra_forbidden([q, p]):
                self.assertEqual((ffutil.forbidden(("h", p)), ffutil.forbidden(("h", q))), (p, q))
            self.assertEqual(ffutil.forbidden(("h", p)), p, "the inner block keeps the outer's port")
            if q != p:
                self.assertIsNone(ffutil.forbidden(("h", q)))
            self.assertEqual(ffutil.FORBIDDEN_TEST_PORTS, SPEC_PORTS)
        self.assertIsNone(ffutil.forbidden(("127.0.0.1", p)))
        with self.assertRaises(KeyError):
            with ffutil.extra_forbidden({p}):
                raise KeyError("the block fails")
        self.assertIsNone(ffutil.forbidden(("127.0.0.1", p)), "removed although the block raised")

    def test_extra_forbidden_takes_port_numbers_only(self):
        for bad, error in (({0}, ValueError), ({65536}, ValueError), ([-1], ValueError), (["80"], TypeError),
                           ({1.5}, TypeError), ("9", TypeError), (5, TypeError)):
            with self.subTest(bad=bad), self.assertRaises(error):
                with ffutil.extra_forbidden(bad):
                    pass


class GuardOnUdp(SocketTest):
    def setUp(self):
        self.receiver, self.port = self.bound(UDP)
        self.addr = ("127.0.0.1", self.port)
        self.sender = self.sock(UDP)

    def test_guarded_calls_raise_and_the_receiver_stays_empty(self):
        s, other, addr = self.sender, self.sock(UDP), self.addr
        calls = [("sendto", lambda: s.sendto(b"leak", addr)),
                 ("sendto with flags", lambda: s.sendto(b"leak", 0, addr)),
                 ("sendto a host name", lambda: s.sendto(b"leak", ("localhost", self.port))),
                 ("connect", lambda: s.connect(addr)),
                 ("connect_ex", lambda: s.connect_ex(addr)),
                 ("bind", lambda: other.bind(addr))]
        if HAS_SENDMSG:
            calls.append(("sendmsg", lambda: s.sendmsg([b"leak"], [], 0, addr)))
        with ffutil.extra_forbidden({self.port}):
            for label, call in calls:
                with self.subTest(label):
                    self.blocked(call, self.port)
            self.quiet(self.receiver)
        self.assertRaises(OSError, s.getpeername)       # neither connect reached the OS
        # After the block the port works again; the first datagram is the new one, so none leaked.
        s.sendto(b"after", addr)
        self.assertEqual(self.arrival(self.receiver), b"after")
        s.connect(addr)
        s.send(b"connected")
        self.assertEqual((s.getpeername(), self.arrival(self.receiver)), (addr, b"connected"))
        third = self.sock(UDP)
        self.assertEqual(third.connect_ex(addr), 0)
        third.send(b"connect_ex")
        self.assertEqual(self.arrival(self.receiver), b"connect_ex")
        self.assertRaises(OSError, other.bind, addr)    # reaches the OS now, which reports the port in use
        if HAS_SENDMSG:
            other.sendmsg([b"send", b"msg"], [], 0, addr)
            self.assertEqual(self.arrival(self.receiver), b"sendmsg")
        self.quiet(self.receiver)

    def test_the_check_comes_before_the_os(self):
        closed = socket.socket(socket.AF_INET, UDP)
        closed.close()
        calls = {"sendto": lambda: closed.sendto(b"x", self.addr), "connect": lambda: closed.connect(self.addr),
                 "bind": lambda: closed.bind(self.addr), "connect_ex": lambda: closed.connect_ex(self.addr)}
        if HAS_SENDMSG:
            calls["sendmsg"] = lambda: closed.sendmsg([b"x"], [], 0, self.addr)
        with ffutil.extra_forbidden({self.port}):
            for label, call in calls.items():
                with self.subTest(label):
                    self.blocked(call, self.port)
        for label, call in calls.items():      # outside the block the OS refuses the closed socket
            with self.subTest(label):
                if label == "connect_ex":
                    self.assertNotEqual(call(), 0)
                else:
                    self.assertRaises(OSError, call)
        self.quiet(self.receiver)


class GuardOnTcp(SocketTest):
    def test_connect_raises_and_the_listener_gets_nothing(self):
        listener, port = self.bound(TCP)
        addr, client, other = ("127.0.0.1", port), self.sock(TCP), self.sock(TCP)
        with ffutil.extra_forbidden({port}):
            for label, call in (("connect", lambda: client.connect(addr)),
                                ("connect_ex", lambda: client.connect_ex(addr)),
                                ("connect a host name", lambda: client.connect(("localhost", port))),
                                ("bind", lambda: other.bind(addr))):
                with self.subTest(label):
                    self.blocked(call, port)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)   # it leaks its socket on a non-OSError
                self.blocked(lambda: socket.create_connection(addr, timeout=2), port)
                gc.collect()
            self.quiet(listener)
        self.assertRaises(OSError, client.getpeername)
        # After the block the port works again; the first connection accepted is this client's.
        client.settimeout(5.0)
        client.connect(addr)
        conn = self.arrival(listener)
        client.sendall(b"after")
        self.assertEqual((conn.getpeername(), conn.recv(16)), (client.getsockname(), b"after"))
        second = self.sock(TCP)
        second.settimeout(5.0)
        self.assertEqual(second.connect_ex(addr), 0)
        self.assertEqual(self.arrival(listener).getpeername(), second.getsockname())
        self.quiet(listener)


class GuardInstalled(SocketTest):
    AVAILABLE = [n for n in ffutil.GUARDED_METHODS if hasattr(_socket.socket, n)]

    def test_installed_at_import_and_each_attribute_is_the_wrapper(self):
        self.assertEqual(ffutil.GUARDED_METHODS, ("bind", "connect", "connect_ex", "sendto", "sendmsg"))
        self.assertTrue(ffutil.guard_installed())
        absent = [n for n in ffutil.GUARDED_METHODS if n not in self.AVAILABLE]
        self.assertEqual(absent, ["sendmsg"] if os.name == "nt" else [], "only Windows lacks sendmsg")
        self.assertEqual([n for n in absent if hasattr(socket.socket, n)], [], "no wrapper is invented")
        for name in self.AVAILABLE:
            current = inner = getattr(socket.socket, name)
            while hasattr(inner, "__wrapped__"):
                inner = inner.__wrapped__
            self.assertTrue(any(current is g for g in ffutil._guards[name]), name)
            self.assertIs(inner, getattr(_socket.socket, name))
            self.assertIsNot(current, inner)

    def test_install_is_idempotent(self):
        before = [getattr(socket.socket, n, None) for n in ffutil.GUARDED_METHODS]
        ffutil.install_port_guard()
        ffutil.install_port_guard()
        after = [getattr(socket.socket, n, None) for n in ffutil.GUARDED_METHODS]
        self.assertTrue(all(a is b for a, b in zip(before, after)))
        self.assertTrue(ffutil.guard_installed())

    def test_a_replaced_attribute_is_detected_and_wrapped_again(self):
        port, sock = ffutil.free_port("tcp"), self.sock(TCP)
        for name in self.AVAILABLE:
            fake = mock.Mock(return_value=0)          # a stand-in that never reaches the OS
            with self.subTest(name), mock.patch.object(socket.socket, name, fake):
                self.assertFalse(ffutil.guard_installed())
                ffutil.install_port_guard()
                self.assertTrue(ffutil.guard_installed())
                if name == "connect_ex":
                    with ffutil.extra_forbidden({port}):
                        self.blocked(lambda: sock.connect_ex(("127.0.0.1", port)), port)
                    fake.assert_not_called()
                    self.assertEqual(sock.connect_ex(("127.0.0.1", port)), 0)
                    fake.assert_called_once_with(sock, ("127.0.0.1", port))
            self.assertTrue(ffutil.guard_installed(), "the patch restored the original wrapper")


SABOTAGE = r'''
import importlib.util, json, socket, sys, _socket
names = [n for n in ("bind", "connect", "connect_ex", "sendto", "sendmsg") if hasattr(_socket.socket, n)]
real = socket.socket
plain = lambda: [getattr(real, n) is getattr(_socket.socket, n) for n in names]

def load(tag):
    spec = importlib.util.spec_from_file_location("ffutil_" + tag, sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

results = {"names": names, "before": plain()}
for name in names:
    class Refuses(type):                      # silently drops the assignment of one attribute
        def __setattr__(cls, attr, value, _refused=name):
            if attr != _refused:
                type.__setattr__(cls, attr, value)
    socket.socket = Refuses("Sabotaged", (_socket.socket,), {})
    try:
        results[name] = "imported %s" % load(name)
    except AssertionError as e:
        results[name] = str(e)
    socket.socket = real
results["control"] = load("control").guard_installed()
results["after"] = plain()
print(json.dumps(results))
'''


class ImportSelfCheck(unittest.TestCase):
    def test_a_broken_guard_fails_the_import(self):
        run = subprocess.run([sys.executable, "-B", "-I", "-c", SABOTAGE, str(TESTS / "ffutil.py")],
                             capture_output=True, text=True, timeout=300)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        results = json.loads(run.stdout.strip().splitlines()[-1])
        names = results["names"]
        self.assertEqual(names, [n for n in ffutil.GUARDED_METHODS if n != "sendmsg" or HAS_SENDMSG])
        for name in names:
            self.assertEqual(results[name], "ffutil: port guard missing on socket.socket: " + name)
        self.assertEqual((results["before"], results["control"], results["after"]),
                         ([True] * len(names), True, [False] * len(names)), "the import installs the guard")


def outcome(*tools, **kwargs):
    """What require_native() did, as data: a SkipTest escaping into a test would skip the test
    itself and so hide a wrong result."""
    try:
        return "returned", ffutil.require_native(*tools, **kwargs)
    except unittest.SkipTest as e:
        return "skipped", str(e)
    except AssertionError as e:
        return "failed", str(e)


class RequireNative(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.require(None)

    def require(self, value):
        """Set FRAMEFACE_REQUIRE_NATIVE to `value`, or remove it for None."""
        if value is None:
            os.environ.pop("FRAMEFACE_REQUIRE_NATIVE", None)
        else:
            os.environ["FRAMEFACE_REQUIRE_NATIVE"] = value

    def on(self, platform):
        """The real platform if it already is `platform`, else sys.platform patched to it."""
        return contextlib.nullcontext() if sys.platform == platform else mock.patch.object(sys, "platform", platform)

    def path_with(self, *names, keep=True):
        """Put a folder holding fake programs `names` (also as .exe) first on PATH; return it."""
        folder = tempfile.mkdtemp(prefix="ffutil tools ")
        self.addCleanup(shutil.rmtree, folder, True)
        for path in [os.path.join(folder, n + ext) for n in names for ext in ("", ".exe")]:
            with open(path, "w") as f:
                f.write("#!/bin/sh\nexit 99\n")
            os.chmod(path, 0o755)
        os.environ["PATH"] = folder + (os.pathsep + os.environ.get("PATH", "") if keep else "")
        return folder

    def test_windows_skips_even_with_a_fake_bash_first_on_path(self):
        folder = self.path_with("bash")
        self.assertEqual(os.path.normcase(os.path.dirname(shutil.which("bash"))), os.path.normcase(folder))
        with self.on("win32"), mock.patch.object(shutil, "which", wraps=shutil.which) as which:
            kind, message = outcome("bash", reason="install.sh")
        which.assert_not_called()
        self.assertEqual(kind, "skipped", message)
        self.assertIn("win32 is not Linux", message)
        self.assertIn("install.sh", message)

    def test_non_linux_fails_with_require_native_set(self):
        self.path_with("bash", "gcc")
        for platform in ("win32", "darwin", "cygwin"):
            for value, expected in ((None, "skipped"), ("0", "skipped"), ("1", "failed")):
                for tools in (("bash",), ("bash", "gcc"), ()):
                    self.require(value)
                    with self.subTest(platform=platform, env=value, tools=tools):
                        with self.on(platform):
                            kind, message = outcome(*tools)
                        self.assertEqual(kind, expected, message)
                        self.assertIn("%s is not Linux" % platform, message)
                        self.assertEqual("FRAMEFACE_REQUIRE_NATIVE=1" in message, value == "1")

    def test_linux_needs_every_tool_on_path(self):
        folder = self.path_with("ff-fake-tool", keep=False)
        with self.on("linux"):
            present, nothing = outcome("ff-fake-tool", reason="demo"), outcome()
            missing = outcome("ff-fake-tool", "ff-missing-tool", reason="demo")
            self.require("1")
            missing_required, present_required = outcome("ff-missing-tool"), outcome("ff-fake-tool")
        self.assertEqual((present[0], list(present[1])), ("returned", ["ff-fake-tool"]))
        self.assertEqual(os.path.normcase(os.path.dirname(present[1]["ff-fake-tool"])), os.path.normcase(folder))
        self.assertEqual(nothing, ("returned", {}))
        self.assertEqual(missing[0], "skipped", missing[1])
        self.assertIn("ff-missing-tool is not on PATH", missing[1])
        self.assertIn("demo", missing[1])
        self.assertEqual(missing_required[0], "failed", missing_required[1])
        self.assertIn("ff-missing-tool is not on PATH", missing_required[1])
        self.assertEqual((present_required[0], list(present_required[1])), ("returned", ["ff-fake-tool"]))
        with self.assertRaises(TypeError):
            ffutil.require_native("")

    def test_capability_names_are_probed_not_looked_up_on_path(self):
        self.path_with("turbojpeg", "linux-syscalls", keep=False)         # decoys that must not count
        probes = {"turbojpeg": mock.Mock(return_value=None), "linux-syscalls": mock.Mock(return_value="libc")}
        with self.on("linux"), mock.patch.dict(ffutil._CAPABILITIES, probes), \
                mock.patch.object(shutil, "which", wraps=shutil.which) as which:
            present, absent = outcome("linux-syscalls"), outcome("turbojpeg")
        which.assert_not_called()
        self.assertEqual(present, ("returned", {"linux-syscalls": "libc"}))
        self.assertEqual(absent[0], "skipped", absent[1])
        self.assertIn("capability turbojpeg is unavailable", absent[1])
        self.assertEqual(set(ffutil._CAPABILITIES), {"turbojpeg", "linux-syscalls"})


class NativeCapabilities(unittest.TestCase):
    """The real probes on Linux. Elsewhere they skip, except with FRAMEFACE_REQUIRE_NATIVE=1 (the
    container), where require_native() must return, so that a wrong skip cannot hide a broken probe."""

    def probe(self, *tools):
        if os.environ.get("FRAMEFACE_REQUIRE_NATIVE", "").strip() != "1":
            return ffutil.require_native(*tools, reason="real probes")
        kind, found = outcome(*tools, reason="real probes")
        self.assertEqual(kind, "returned", found)
        return found

    def test_programs_and_syscalls(self):
        found = self.probe("bash", "linux-syscalls")
        self.assertEqual((os.path.isfile(found["bash"]), found["linux-syscalls"]), (True, "libc"))

    def test_turbojpeg(self):
        self.assertIn("turbojpeg", self.probe("turbojpeg")["turbojpeg"])


class FreePortAndFps(unittest.TestCase):
    def test_free_port_is_never_forbidden(self):
        for kind, socktype in (("udp", UDP), ("tcp", TCP)):
            for port in [ffutil.free_port(kind) for _ in range(32)] + [ffutil.free_port()]:
                self.assertTrue(1024 <= port <= 65535, port)
                self.assertNotIn(port, ffutil.FORBIDDEN_TEST_PORTS)
                self.assertIsNone(ffutil.forbidden(("127.0.0.1", port)))
            self.assertTrue(any(self.binds(socktype, ffutil.free_port(kind)) for _ in range(5)), kind)

    @staticmethod
    def binds(socktype, port):
        with socket.socket(socket.AF_INET, socktype) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:                   # another process may take the port in between
                return False
            return True

    def test_free_port_skips_forbidden_and_extra_ports(self):
        good = ffutil.free_port("tcp")
        offered = sorted(ffutil.FORBIDDEN_TEST_PORTS) + [good]
        with mock.patch.object(ffutil, "_ephemeral_port", side_effect=offered) as ephemeral:
            self.assertEqual(ffutil.free_port("tcp"), good)
        self.assertEqual(ephemeral.call_count, len(offered))
        ephemeral.assert_called_with(TCP)
        with ffutil.extra_forbidden({good}), \
                mock.patch.object(ffutil, "_ephemeral_port", side_effect=[good, good - 1]) as ephemeral:
            self.assertEqual(ffutil.free_port("udp"), good - 1)
        ephemeral.assert_called_with(UDP)
        with mock.patch.object(ffutil, "_ephemeral_port", return_value=min(ffutil.FORBIDDEN_TEST_PORTS)), \
                self.assertRaises(RuntimeError):
            ffutil.free_port()
        for kind in ("sctp", "UDP", "", None, 17, UDP):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                ffutil.free_port(kind)

    def test_fps_asserts(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("FRAMEFACE_NO_FPS_ASSERT", None)
            self.assertIs(ffutil.fps_asserts(), True)
            for value, expected in (("1", False), (" 1\n", False), ("0", True), ("", True), ("yes", True)):
                os.environ["FRAMEFACE_NO_FPS_ASSERT"] = value
                self.assertIs(ffutil.fps_asserts(), expected, repr(value))


class SetupPath(unittest.TestCase):
    def test_puts_headset_frameface_first_once(self):
        expected = TESTS.parents[1] / "headset" / "frameface"
        self.assertEqual((ffutil.REPO, ffutil.FRAMEFACE_DIR), (TESTS.parents[1], expected))
        with mock.patch.object(sys, "path", ["first", str(expected), "last"]):
            path = ffutil.setup_path()
            self.assertEqual((Path(path), sys.path), (expected, [path, "first", "last"]))
            self.assertEqual((ffutil.setup_path(), sys.path), (path, [path, "first", "last"]))


# -- source scan: no socket call with a forbidden port literal (§11.3)

SOCKET_CALLS = frozenset({"bind", "connect", "connect_ex", "sendto", "sendmsg", "create_connection"})
CALL_RE = re.compile(r"\b(?:{0})\s*\(|\.\s*(?:{0})\b".format("|".join(sorted(SOCKET_CALLS))))
LITERAL_RE = re.compile(r"(?<![\w.])(?:0[xXoObB][0-9a-fA-F_]+|[0-9][0-9_]*)(?![\w.])|[bB]?(['\"])\s*[0-9]+\s*\1")
NOISE = (tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT)    # NEWLINE ends a reference


def port_literal(text):
    """The forbidden port a number or string literal spells (9000, 0x2328, 9_000, '9000'), else None."""
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    if isinstance(value, (bytes, str)):
        value = (value.decode("latin-1") if isinstance(value, bytes) else value).strip()
        value = int(value) if value.isascii() and value.isdigit() else None
    return value if type(value) is int and value in ffutil.FORBIDDEN_TEST_PORTS else None


def scan_source(source):
    """[(line, text)] for each line of a socket call, from its name to its closing parenthesis, or of
    a socket method passed as a value (assertRaises(E, s.sendto, ...), Thread(target=s.connect, ...)),
    from the reference to the end of the enclosing call or statement, that holds a forbidden port
    literal; plain values elsewhere, bare names (bind=...), comments and longer strings do not count.
    Source that does not tokenize (a file being written) is checked line by line."""
    lines = source.splitlines()
    try:
        tokens = [t for t in tokenize.generate_tokens(io.StringIO(source).readline) if t.type not in NOISE]
    except (tokenize.TokenError, SyntaxError, ValueError):
        return [(n, s.strip()) for n, s in enumerate(lines, 1)
                if CALL_RE.search(s) and any(port_literal(m.group(0)) for m in LITERAL_RE.finditer(s))]
    literals = {t.start[0] for t in tokens if t.type in (tokenize.NUMBER, tokenize.STRING) and port_literal(t.string)}
    spanned = set()
    for i, tok in enumerate(tokens[:-1]):
        call = tokens[i + 1].string == "("
        if tok.type != tokenize.NAME or tok.string not in SOCKET_CALLS or not (call or tokens[i - 1].string == "."):
            continue
        depth, end = 0, len(lines)
        for t in tokens[i + 1:]:
            if t.type == tokenize.OP:
                depth += (t.string in ("(", "[", "{")) - (t.string in (")", "]", "}"))
            if (call and depth == 0) or depth < 0 or t.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
                end = t.end[0] if t.type == tokenize.OP else t.start[0]
                break
        spanned.update(range(tok.start[0], end + 1))
    return [(n, lines[n - 1].strip()) for n in sorted(spanned & literals)]


class SourceScan(unittest.TestCase):
    def test_no_test_file_calls_a_socket_with_a_forbidden_port(self):
        files = sorted(TESTS.glob("test_ff_*.py")) + [TESTS / "ffutil.py"]
        self.assertIn(Path(__file__).resolve(), [f.resolve() for f in files])
        findings = []
        for path in files:
            try:
                with tokenize.open(str(path)) as f:
                    source = f.read()
            except (SyntaxError, UnicodeDecodeError):
                source = path.read_bytes().decode("utf-8", "replace")
            findings += ["%s:%d: %s" % (path.name, n, text) for n, text in scan_source(source)]
        self.assertEqual(findings, [], "socket calls with a forbidden port literal")

    def test_the_scan_flags_socket_calls_and_allows_plain_values(self):
        for p in sorted(ffutil.FORBIDDEN_TEST_PORTS):
            for source, flagged in (
                    ("s.bind(('127.0.0.1', %d))" % p, [1]),
                    ("s.connect(('localhost', %d))" % p, [1]),
                    ("rc = s.connect_ex(('::1', %d, 0, 0))" % p, [1]),
                    ("s.sendto(b'x', ('127.0.0.1', %s))" % hex(p), [1]),
                    ("s.sendmsg([b'x'], [], 0, ('h', %s))" % format(p, "_"), [1]),
                    ("socket.create_connection(('127.0.0.1', '%d'))" % p, [1]),
                    ("s.sendto(\n    b'x',\n    ('127.0.0.1', %d),\n)" % p, [3]),
                    ("p = %d; s.bind(('127.0.0.1', 0))" % p, [1]),
                    ("def f(s):\n    s.connect(('127.0.0.1', %d)\n" % p, [2]),          # does not tokenize
                    ("self.assertRaises(AssertionError, s.sendto, b'x', ('127.0.0.1', %d))" % p, [1]),
                    ("self.assertRaises(AssertionError, s.connect,\n    ('localhost', %d))\nx = 1" % p, [2]),
                    ("Thread(target=sock.connect_ex, args=(('h', %d),)).start()" % p, [1]),
                    ("def f(s):\n    check(s.sendto, b'x', ('h', %d)\n" % p, [2]),      # does not tokenize
                    ("cfg = {'http_port': %d}" % p, []),                                # plain values from here
                    ("Config(bind='0.0.0.0', http_port=%d)" % p, []),
                    ("f = s.connect\nport = %d" % p, []),
                    ("self.assertIn(%d, FORBIDDEN_PORTS)" % p, []),
                    ("self.assertRaises(ValueError, validate, {'http_port': %d})" % p, []),
                    ("self.assertEqual(ffutil.forbidden(('h', %d)), %d)" % (p, p), []),
                    ("s.bind(('127.0.0.1', 0))  # never %d" % p, []),
                    ("doc = 's.connect((\"h\", %d))'" % p, []),
                    ("s.bind(('127.0.0.1', ffutil.free_port('udp')))\nlimit = %d" % p, []),
                    ("s.sendto(b'x', ('h', %d)); s.bind(('h', %d))" % (p + 10000, p * 10), []),
                    ("def f(\n    cfg = {'http_port': %d}" % p, [])):
                with self.subTest(source=source):
                    self.assertEqual([n for n, _ in scan_source(source)], flagged)


if __name__ == "__main__":
    unittest.main()
