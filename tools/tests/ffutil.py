# SPDX-License-Identifier: MIT
"""Shared helpers of the FrameFace tests (spec §11.2; frozen API of task T0).

Every tools/tests/test_ff_*.py file imports this module before any other non-stdlib module.
Importing it wraps socket.socket.bind, connect, connect_ex, sendto and sendmsg so that a call
aimed at a port of FORBIDDEN_TEST_PORTS raises AssertionError("forbidden port <p>") before it
reaches the OS, and then checks that the wrappers are in place: a broken guard fails the import
instead of letting a test talk to VRCFaceTracking, VRChat, Babble, Steam Link or the FrameEye
module that may be running on the same PC.

    setup_path()                       headset/frameface first on sys.path (import frameface)
    require_native(*tools, reason="")  Linux and every tool present, else unittest.SkipTest
                                       (AssertionError with FRAMEFACE_REQUIRE_NATIVE=1)
    free_port(kind="udp")              a free ephemeral loopback port, "udp" or "tcp"
    fps_asserts()                      False when FRAMEFACE_NO_FPS_ASSERT=1
    FORBIDDEN_TEST_PORTS, forbidden(address), install_port_guard(), guard_installed(),
    extra_forbidden(ports)             the port guard (extra_forbidden: tests of the guard only)

The guard covers the in-process socket API (socket.create_connection, socketserver, http.client
and ssl all go through these methods). It cannot see child processes, the C type
_socket.socket used directly, or asyncio's Windows proactor, which calls ConnectEx/WSASendTo
itself; no FrameFace test may use those to reach a port.
"""
import _socket
import contextlib
import operator
import os
from pathlib import Path
import shutil
import socket
import sys
import threading
import unittest

__all__ = [
    "REPO", "FRAMEFACE_DIR", "FORBIDDEN_TEST_PORTS", "GUARDED_METHODS", "setup_path",
    "require_native", "free_port", "fps_asserts", "forbidden", "install_port_guard",
    "guard_installed", "extra_forbidden",
]

REPO = Path(__file__).resolve().parents[2]
FRAMEFACE_DIR = REPO / "headset" / "frameface"

# Ports of software that runs live on the developer's PC (§d.6): Babble OSC 8888/8889
# (VRCFT-Babble, Baballonia), VRChat OSC 9000/9001, Steam Link OSC 9015, Baballonia DFR 9020,
# FrameEye SFEY 9021. No test may bind, connect or send to them.
FORBIDDEN_TEST_PORTS = frozenset({8888, 8889, 9000, 9001, 9015, 9020, 9021})
GUARDED_METHODS = ("bind", "connect", "connect_ex", "sendto", "sendmsg")


# -- paths and environment

def setup_path():
    """Put headset/frameface at sys.path[0] (once) so that `import frameface` finds the
    package of this checkout; return that path."""
    path = str(FRAMEFACE_DIR)
    if sys.path[:1] != [path]:
        while path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
    return path


def fps_asserts():
    """True unless FRAMEFACE_NO_FPS_ASSERT=1 (set for QEMU and slow runners): frame-rate
    thresholds are asserted only when this is True."""
    return os.environ.get("FRAMEFACE_NO_FPS_ASSERT", "").strip() != "1"


def _turbojpeg():
    """The TurboJPEG library name that loads, searched like jpeg.py does (§4.16), or None."""
    import ctypes
    import ctypes.util

    def loads(name):
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            return False
        return hasattr(lib, "tj3Init") or hasattr(lib, "tjInitCompress")

    for name in ("libturbojpeg.so.0", "libturbojpeg.so"):
        if loads(name):
            return name
    name = ctypes.util.find_library("turbojpeg")
    return name if name and loads(name) else None


def _linux_syscalls():
    """'libc' if libc's ioctl, poll and syscall are callable through ctypes, else None."""
    import ctypes
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except (OSError, TypeError):
        return None
    return "libc" if all(hasattr(libc, n) for n in ("ioctl", "poll", "syscall")) else None


# Capability names of require_native(); every other name is a program looked up on PATH.
_CAPABILITIES = {"turbojpeg": _turbojpeg, "linux-syscalls": _linux_syscalls}


def require_native(*tools, reason=""):
    """Raise unittest.SkipTest unless this is Linux and every tool is available; return
    {tool: where it was found}.

    A tool is a program found by shutil.which ("gcc", "bash", ...) or a capability name
    ("turbojpeg": the TurboJPEG library loads; "linux-syscalls": libc's ioctl/poll/syscall).
    Any other platform never qualifies and PATH is not even consulted there, so a bash.exe of
    WSL or Git Bash on Windows never runs a Linux-only test. With FRAMEFACE_REQUIRE_NATIVE=1
    (the arm64 container) an unmet requirement is an AssertionError, i.e. a failure.
    """
    for tool in tools:
        if not isinstance(tool, str) or not tool:
            raise TypeError("require_native() takes tool names, not %r" % (tool,))
    found, problems = {}, []
    if not sys.platform.startswith("linux"):
        problems.append("platform %s is not Linux" % sys.platform)
    else:
        for tool in tools:
            probe = _CAPABILITIES.get(tool)
            where = probe() if probe is not None else shutil.which(tool)
            if where:
                found[tool] = where
            elif probe is not None:
                problems.append("capability %s is unavailable" % tool)
            else:
                problems.append("%s is not on PATH" % tool)
    if not problems:
        return found
    message = "needs Linux%s%s: %s" % (
        " with " + ", ".join(tools) if tools else "",
        " (%s)" % reason if reason else "",
        "; ".join(problems))
    if os.environ.get("FRAMEFACE_REQUIRE_NATIVE", "").strip() == "1":
        raise AssertionError(message + " [FRAMEFACE_REQUIRE_NATIVE=1]")
    raise unittest.SkipTest(message)


# -- ports

_KINDS = {"udp": socket.SOCK_DGRAM, "tcp": socket.SOCK_STREAM}


def _ephemeral_port(socktype):
    """Let the OS pick a free loopback port of this socket type (port 0) and return it."""
    with socket.socket(socket.AF_INET, socktype) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def free_port(kind="udp"):
    """A free ephemeral port on 127.0.0.1 for "udp" or "tcp"; never a forbidden port."""
    if not isinstance(kind, str) or kind not in _KINDS:
        raise ValueError("kind must be 'udp' or 'tcp', not %r" % (kind,))
    for _ in range(64):
        port = _ephemeral_port(_KINDS[kind])
        if forbidden(("127.0.0.1", port)) is None:
            return port
    raise RuntimeError("the OS offered only forbidden ports in 64 tries")


_lock = threading.Lock()
_extra = frozenset()        # ports added by the active extra_forbidden() blocks
_extra_entries = []         # (token, ports) per active block
_guards = {}                # name -> every wrapper this module created for socket.socket.<name>


def forbidden(address):
    """The port if `address` is a (host, port, ...) tuple of any family (IPv4, IPv6, host name)
    aimed at a forbidden port, else None. Pure: no socket, no name lookup. The forbidden set is
    FORBIDDEN_TEST_PORTS plus the ports of the active extra_forbidden() blocks."""
    if not isinstance(address, tuple) or len(address) < 2:
        return None
    try:
        port = operator.index(address[1])
    except TypeError:
        return None
    return port if port in FORBIDDEN_TEST_PORTS or port in _extra else None


def _port_number(value):
    port = operator.index(value)
    if not 1 <= port <= 65535:
        raise ValueError("not a port number: %r" % (value,))
    return port


@contextlib.contextmanager
def extra_forbidden(ports):
    """Refuse `ports` (an iterable of ints) as well while the block runs; yields them as a
    frozenset. Only for tests of the guard itself, on free ephemeral ports. Blocks nest; each
    removes its own ports on exit, also when it raises."""
    global _extra
    added = frozenset(_port_number(p) for p in ports)
    token = object()
    with _lock:
        _extra_entries.append((token, added))
        _extra = frozenset().union(*(entry for _, entry in _extra_entries))
    try:
        yield added
    finally:
        with _lock:
            _extra_entries[:] = [e for e in _extra_entries if e[0] is not token]
            _extra = frozenset().union(*(entry for _, entry in _extra_entries))


def _addresses(name, args, kwargs):
    """The arguments of a guarded call that can hold an address."""
    if name == "sendto":            # sendto(data, address) or sendto(data, flags, address)
        picked = args[1:]
    elif name == "sendmsg":         # sendmsg(buffers[, ancdata[, flags[, address]]])
        picked = args[3:]
    else:                           # bind(address), connect(address), connect_ex(address)
        picked = args[:1]
    return list(picked) + list(kwargs.values())


def _make_guard(name, inner):
    def guard(self, *args, **kwargs):
        for address in _addresses(name, args, kwargs):
            port = forbidden(address)
            if port is not None:
                raise AssertionError("forbidden port %d" % port)
        return inner(self, *args, **kwargs)

    guard.__name__ = name
    guard.__qualname__ = "socket." + name
    guard.__doc__ = getattr(inner, "__doc__", None)
    guard.__wrapped__ = inner
    _guards.setdefault(name, []).append(guard)
    return guard


def _is_guard(name, value):
    return any(value is g for g in _guards.get(name, ()))


def install_port_guard():
    """Wrap socket.socket.bind, connect, connect_ex, sendto and sendmsg (where the platform has
    it) with the port check. Idempotent: an attribute that already is our wrapper stays."""
    cls = socket.socket
    with _lock:
        for name in GUARDED_METHODS:
            current = getattr(cls, name, None)
            if current is None or _is_guard(name, current):
                continue
            setattr(cls, name, _make_guard(name, current))


def _unguarded():
    """The guarded method names that socket.socket does not resolve to our wrapper."""
    missing = []
    for name in GUARDED_METHODS:
        current = getattr(socket.socket, name, None)
        if current is None and not hasattr(_socket.socket, name):
            continue                # not provided here (sendmsg on Windows): nothing can send
        if not _is_guard(name, current):
            missing.append(name)
    return missing


def guard_installed():
    """True if every guarded method of socket.socket is this module's wrapper."""
    return not _unguarded()


install_port_guard()
if not guard_installed():
    raise AssertionError("ffutil: port guard missing on socket.socket: " + ", ".join(_unguarded()))
