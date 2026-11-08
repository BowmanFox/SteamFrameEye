# SPDX-License-Identifier: MIT
"""Configuration ~/.config/frameface/config.json and the service's file paths (spec §4.19, §3.3).

load() never raises: an invalid value falls back to its default (jpeg_quality below 90 is raised
to 90) with one warning, an unknown key gets a warning, unreadable JSON gives all defaults plus a
warning. A missing or invalid token is replaced by a fresh random token (the stream then stays
unreachable until `python3 -I ~/frameface/frameface_main.py init-config` writes one), never by ""
(no token).

The lock path comes from the uid (/run/user/<uid>/frameface.lock if that directory exists, else
the state dir), never from $XDG_RUNTIME_DIR, so the unit and an SSH session agree.
"""
from dataclasses import dataclass, field, asdict
import ipaddress
import itertools
import json
import math
import os
import re
import secrets
import time

from .policy import IR_LEVELS, STRATEGIES, parse_cpus

__all__ = ["FORBIDDEN_PORTS", "DEFAULT_HTTP_PORT", "DEFAULT_ALLOW_PEERS", "TOKEN_RE", "Config", "load",
           "write_default", "new_token", "Paths", "default_paths", "temp_paths", "atomic_write", "ensure_dir"]

# Every port of the research brief §d.6 "ports to avoid": Steam Remote Play UDP 27031-27036 and
# TCP 27036-27037, vrlink 10400/10401, VRChat 9000/9001, Steam Link OSC 9015, Baballonia DFR 9020,
# SFEY 9021, Babble 8888/8889, BFR Link 6767, FCAM 8555, vftstream 8097, vft-stream 8085, BFR camera
# stream 9099, SRanipatch 9337, USB/IP 3240, VirtualHere 7575, SRanibro 5555.
FORBIDDEN_PORTS = frozenset({3240, 5555, 6767, 7575, 8085, 8097, 8555, 8888, 8889, 9000, 9001, 9015, 9020, 9021,
                             9099, 9337, 10400, 10401, *range(27031, 27038)})
DEFAULT_HTTP_PORT = 9023
DEFAULT_ALLOW_PEERS = ("10.35.78.0/24", "127.0.0.0/8")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
INIT_CONFIG = "python3 -I ~/frameface/frameface_main.py init-config"   # the whole command line, as cli.CLI in hints


def new_token() -> str:
    """A 128-bit URL-safe token (22 characters)."""
    return secrets.token_urlsafe(16)


@dataclass
class Config:
    enabled: bool = True
    http_port: int = DEFAULT_HTTP_PORT
    allow_peers: list = field(default_factory=lambda: list(DEFAULT_ALLOW_PEERS))
    token: str = field(default_factory=new_token)    # init-config writes one; "" disables the path secret
    max_stream_clients: int = 2
    jpeg_quality: int = 92
    stream_max_kbps: int = 5000
    stream_fps_max: int = 60
    ip_tos: int = 0
    placeholder: bool = True
    placeholder_ms: int = 1500
    keepalive_s: float = 2.0
    camera: str = "left"
    y_phase: str = "auto"
    preprocess: str = "auto"
    strategy: str = "auto"
    xu_len: object = "auto"                          # "auto" | 64 | 384
    teardown_magic3: bool = False
    n_urbs: int = 16
    urb_bytes: int = 0
    ir_level: str = "auto"
    ir_current: "int | None" = None
    ir_pwm: int = 255
    settle_s: float = 8.0
    enum_settle_s: float = 2.0
    power_policy: str = "auto"
    gate_on_worn: bool = True
    worn_timeout_s: float = 3.0
    unworn_linger_s: float = 60.0
    no_client_linger_s: float = 30.0
    min_battery_pct: int = 5
    allow_port_reset: bool = True
    cpu_io: str = "0-1"
    cpu_work: str = "2-3"
    nice_io: int = -5
    log_level: str = "info"


class _Invalid(ValueError):
    def __init__(self, reason, fallback=None):
        super().__init__(reason)
        self.fallback = fallback


def _boolean(v):
    if not isinstance(v, bool):
        raise _Invalid("not true/false")
    return v


def _integer(lo, hi):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, int):
            raise _Invalid("not an integer")
        if not lo <= v <= hi:
            raise _Invalid("outside %d..%d" % (lo, hi))
        return v
    return check


def _number(lo, hi):
    def check(v):                       # an int is compared as is (math.isfinite overflows on a huge int)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
            raise _Invalid("not a number")
        if not lo <= v <= hi:
            raise _Invalid("outside %g..%g" % (lo, hi))
        return float(v)
    return check


def _choice(*options):
    def check(v):
        if isinstance(v, bool) or v not in options or type(v) is not type(options[0]):
            raise _Invalid("not one of %s" % ", ".join(str(o) for o in options))
        return v
    return check


def _http_port(v):
    v = _integer(1024, 65535)(v)
    if v in FORBIDDEN_PORTS:
        raise _Invalid("a port of other software (FORBIDDEN_PORTS)")
    return v


def _peers(v):
    if not isinstance(v, list) or not all(isinstance(p, str) for p in v):
        raise _Invalid("not a list of IPv4 CIDR strings")
    out = []
    for p in v:
        try:
            net = ipaddress.IPv4Network(p.strip(), strict=False)
        except ValueError:
            raise _Invalid("%r is not an IPv4 CIDR" % p) from None
        if str(net) not in out:
            out.append(str(net))
    return out


def _token(v):
    if not isinstance(v, str) or (v != "" and not TOKEN_RE.match(v)):
        raise _Invalid('not "" or 16-64 characters of A-Z a-z 0-9 _ -', fallback=new_token())
    return v


def _jpeg_quality(v):
    if isinstance(v, bool) or not isinstance(v, int):
        raise _Invalid("not an integer")
    if v < 90:                          # §4.16: lower values (any of them) are raised to 90
        raise _Invalid("below 90", fallback=90)
    if v > 100:
        raise _Invalid("outside 90..100")
    return v


def _xu_len(v):
    if v == "auto":
        return v
    if isinstance(v, int) and not isinstance(v, bool) and v in (64, 384):
        return v
    if isinstance(v, str) and v.strip() in ("64", "384"):
        return int(v)
    raise _Invalid('not "auto", 64 or 384')


def _ir_current(v):
    return None if v is None else _integer(0, 255)(v)


def _urb_bytes(v):
    v = _integer(0, 65536)(v)
    if v and (v < 4096 or v % 512):
        raise _Invalid("not 0 or a multiple of 512 in 4096..65536")
    return v


def _cpus(v):
    try:
        parse_cpus(v)
    except ValueError:
        raise _Invalid('not "auto" or a cpu list like "0-1,4"') from None
    return v.strip()


_CHECKS = {
    "enabled": _boolean, "http_port": _http_port, "allow_peers": _peers, "token": _token,
    "max_stream_clients": _integer(1, 4), "jpeg_quality": _jpeg_quality, "stream_max_kbps": _integer(500, 20000),
    "stream_fps_max": _integer(1, 60), "ip_tos": _choice(0, 32), "placeholder": _boolean,
    "placeholder_ms": _integer(200, 10000), "keepalive_s": _number(0.5, 4.0), "camera": _choice("left", "right"),
    "y_phase": _choice("auto", "odd", "even"), "preprocess": _choice("auto", "c", "swar"),
    "strategy": _choice("auto", *STRATEGIES), "xu_len": _xu_len, "teardown_magic3": _boolean,
    "n_urbs": _integer(4, 32), "urb_bytes": _urb_bytes, "ir_level": _choice("auto", *IR_LEVELS),
    "ir_current": _ir_current, "ir_pwm": _integer(0, 255), "settle_s": _number(4, 30),
    "enum_settle_s": _number(0.5, 10), "power_policy": _choice("auto", "always"), "gate_on_worn": _boolean,
    "worn_timeout_s": _number(1, 30), "unworn_linger_s": _number(10, 600), "no_client_linger_s": _number(5, 600),
    "min_battery_pct": _integer(0, 50), "allow_port_reset": _boolean, "cpu_io": _cpus, "cpu_work": _cpus,
    "nice_io": _integer(-8, 0), "log_level": _choice("debug", "info", "warning"),
}


def load(path: str) -> tuple:
    """(Config, warnings) from `path`; never raises."""
    cfg = Config()
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return cfg, ["config: %s not found; using the defaults and a random token (run %s)" % (path, INIT_CONFIG)]
    except (OSError, ValueError, RecursionError) as e:     # RecursionError: absurdly deep nesting
        return cfg, ["config: %s unreadable (%s); using the defaults and a random token" % (path, e)]
    if not isinstance(data, dict):
        return cfg, ["config: %s is not a JSON object; using the defaults and a random token" % path]
    warnings = []
    for key, value in data.items():
        check = _CHECKS.get(key)
        if check is None:
            warnings.append("config: unknown key %r ignored" % (key,))
            continue
        try:
            setattr(cfg, key, check(value))
        except _Invalid as e:
            used = e.fallback if e.fallback is not None else getattr(cfg, key)
            setattr(cfg, key, used)
            if key == "token":                      # never log a token, not even an invalid one
                warnings.append("config: token invalid (%s); using a random token for this run "
                                "(run %s --new-token)" % (e, INIT_CONFIG))
            else:
                warnings.append("config: %s=%s invalid (%s); using %s" % (key, _short(value), e, json.dumps(used)))
    if "token" not in data:
        warnings.append("config: no token; using a random token for this run (run %s)" % INIT_CONFIG)
    return cfg, warnings


def _short(value):
    text = json.dumps(value)
    return text if len(text) <= 60 else text[:57] + "..."


def write_default(path: str, token: "str | None" = None) -> None:
    """Write the default config (every key) with `token` (None: a new random token) atomically;
    file 0600, parent directory 0700."""
    token = new_token() if token is None else token
    _token(token)
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    text = json.dumps(asdict(Config(token=token)), indent=2) + "\n"
    atomic_write(path, text.encode("utf-8"), 0o600)


# -- files

_tmp_counter = itertools.count()


def _replace(tmp, path):
    """os.replace; on Windows (test runs only) a few retries while a virus scanner or the indexer
    still holds the target briefly (PermissionError), at most about 1 s in total."""
    for attempt in range(12):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 11:
                raise
            time.sleep(0.01 * (attempt + 1))


def ensure_dir(path: str, mode: int = 0o700) -> None:
    """Create `path` and missing parents (each with `mode`); on POSIX the leaf gets `mode`."""
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent and parent != path and not os.path.isdir(parent):
            ensure_dir(parent, mode)
        try:
            os.mkdir(path, mode)
        except FileExistsError:
            pass
    if os.name == "posix":
        os.chmod(path, mode)


def atomic_write(path: str, data: bytes, mode: int = 0o600) -> None:
    """Write `data` to a temporary file in the same directory, fsync it and os.replace() it over
    `path` (then fsync the directory on POSIX). On error the temporary file is removed and the
    OSError propagates; `path` keeps its old content."""
    folder = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(folder, ".%s.%d.%d.tmp" % (os.path.basename(path), os.getpid(), next(_tmp_counter)))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(tmp, flags, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.name == "posix":
            os.chmod(tmp, mode)
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if os.name == "posix":
        try:
            dfd = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass


# -- paths (§3.3)

@dataclass
class Paths:
    config_dir: str
    config: str
    disabled_flag: str
    state_dir: str
    runtime: str
    profile: str
    selftest: str
    events: str
    lock: str


def _paths(config_dir, state_dir, lock_dir):
    return Paths(config_dir=config_dir, config=os.path.join(config_dir, "config.json"),
                 disabled_flag=os.path.join(config_dir, "disabled"), state_dir=state_dir,
                 runtime=os.path.join(state_dir, "runtime.json"), profile=os.path.join(state_dir, "profile.json"),
                 selftest=os.path.join(state_dir, "selftest.json"), events=os.path.join(state_dir, "events.jsonl"),
                 lock=os.path.join(lock_dir, "frameface.lock"))


def default_paths(home: "str | None" = None, runtime_dir: "str | None" = None, *, uid: "int | None" = None,
                  run_root: str = "/run/user") -> Paths:
    """The real paths: ~/.config/frameface, ~/.local/state/frameface and the lock in
    <run_root>/<uid> if that directory exists, else in the state dir. `runtime_dir` given
    explicitly is used as is; $XDG_RUNTIME_DIR is never read."""
    home = os.path.expanduser("~") if home is None else home
    config_dir = os.path.join(home, ".config", "frameface")
    state_dir = os.path.join(home, ".local", "state", "frameface")
    if runtime_dir is None:
        if uid is None and hasattr(os, "getuid"):
            uid = os.getuid()
        candidate = None if uid is None else os.path.join(run_root, str(int(uid)))
        runtime_dir = candidate if candidate is not None and os.path.isdir(candidate) else state_dir
    return _paths(config_dir, state_dir, runtime_dir)


def temp_paths(root: str) -> Paths:
    """Every path (config, state, lock) under `root`: selftest, sim and tests."""
    return _paths(os.path.join(root, "config"), os.path.join(root, "state"), root)
