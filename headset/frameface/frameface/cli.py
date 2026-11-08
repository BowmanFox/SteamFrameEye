# SPDX-License-Identifier: MIT
"""Command line of FrameFace (spec §4.21): `python3 -I ~/frameface/frameface_main.py <command>`.

run, status [--json], url [--new-token], on, off, restart, start, init-config, probe ..., selftest, sim.
Every command first sets XDG_RUNTIME_DIR=/run/user/<uid> when it is unset (Linux), so systemctl
--user works over SSH (restart, start and url --new-token run it); the lock path never depends on
it (config.default_paths derives it from the uid). `probe` is handed to frameface.probe.main(argv)
unchanged. run, sim and the active probe stop on their signals through watch_signals' thread: the
handler takes no lock.
"""
import argparse
from dataclasses import asdict
import gc
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

from . import __version__
from .config import (DEFAULT_ALLOW_PEERS, TOKEN_RE, Config, atomic_write, default_paths, ensure_dir, load, new_token,
                     temp_paths)
from .policy import display

__all__ = ["main", "build_parser", "setup_env", "url_lines", "wlan0_ipv4", "watch_signals", "HEADSET_AP_IP", "UNIT", "CLI"]

HEADSET_AP_IP = "10.35.78.1"
AP_NET = ipaddress.IPv4Network("10.35.78.0/24")
UNIT, CLI = "frameface.service", "python3 -I ~/frameface/frameface_main.py"     # CLI: as hints name it
EXIT_UNREACHABLE = 3
_ARCHIVE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))      # ~/frameface


def setup_env(environ=None, uid=None) -> None:
    """XDG_RUNTIME_DIR=/run/user/<uid> unless already set (Linux, or whenever a uid is given)."""
    environ = os.environ if environ is None else environ
    if uid is None:
        if not hasattr(os, "getuid"):
            return
        uid = os.getuid()
    environ.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % int(uid))


def wlan0_ipv4():
    """The IPv4 address of wlan0 with its prefix ("192.168.4.50/24", `ip -4 -o addr show dev wlan0`), or None."""
    try:
        proc = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", "wlan0"], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+/\d+)", proc.stdout.decode("utf-8", "replace"))
    return m.group(1) if m else None


def url_lines(cfg, wlan0_ip) -> list:
    """The `url` contract: the AP address first; the wlan0 address second, only if wlan0's network
    ("a.b.c.d/n"; a bare address is a /32) overlaps an allow_peers CIDR that is neither loopback nor
    inside the AP network 10.35.78.0/24 (so "-AllowPeer <PC>/32" on the LAN prints it too)."""
    path = "/stream/%s" % cfg.token if cfg.token else "/stream"
    lines = ["http://%s:%d%s" % (HEADSET_AP_IP, cfg.http_port, path)]
    try:
        own = ipaddress.IPv4Interface(wlan0_ip) if wlan0_ip else None
    except ValueError:
        own = None
    for net in cfg.allow_peers if own is not None else ():
        try:
            n = ipaddress.IPv4Network(net, strict=False)
        except ValueError:
            continue
        if not n.is_loopback and not n.subnet_of(AP_NET) and own.network.overlaps(n):
            lines.append("http://%s:%d%s" % (own.ip, cfg.http_port, path))
            break
    return lines


def _write_out(text):
    """stdout with LF line ends on every platform (the url contract is parsed by scripts)."""
    out = getattr(sys.stdout, "buffer", None)
    if out is not None:
        sys.stdout.flush()
        out.write(text.encode("utf-8"))
        out.flush()
    else:
        sys.stdout.write(text)
        sys.stdout.flush()


def _read_json(path):
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def _write_config(path, data):
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    atomic_write(path, (json.dumps(data, indent=2) + "\n").encode("utf-8"), 0o600)


def _norm_cidr(text):
    return str(ipaddress.IPv4Network(str(text).strip(), strict=False))


def _systemctl(*args):
    """(exit code, stderr text) of `systemctl --user <args>` in this environment (setup_env: XDG_RUNTIME_DIR);
    (None, why) when systemctl is missing or does not finish within 60 s. Its stdout is never passed on."""
    exe = shutil.which("systemctl")
    if exe is None:
        return None, "systemctl not found"
    try:
        p = subprocess.run([exe, "--user"] + list(args), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60,
                           check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    return p.returncode, p.stderr.decode("utf-8", "replace").strip()


# -- commands

def cmd_init_config(args, paths):
    try:
        add = [_norm_cidr(c) for c in args.allow_peer or []]
    except ValueError as e:
        print("init-config: invalid CIDR: %s" % e, file=sys.stderr)
        return 1
    data, changed = None, False
    if os.path.exists(paths.config):
        try:
            data = _read_json(paths.config)
        except (OSError, ValueError) as e:
            if not args.force:
                print("init-config: %s is unreadable (%s); use --force" % (paths.config, e), file=sys.stderr)
                return 1
    if data is None or args.force:
        token = data.get("token") if isinstance(data, dict) else None
        cfg = Config(token=token if isinstance(token, str) and (token == "" or TOKEN_RE.match(token)) else new_token())
        data, changed = asdict(cfg), True
    token = data.get("token")
    if args.new_token or not isinstance(token, str) or (token != "" and not TOKEN_RE.match(token)):
        data["token"], changed = new_token(), True
    if add:
        peers = data.get("allow_peers")
        peers = list(peers) if isinstance(peers, list) else list(DEFAULT_ALLOW_PEERS)
        have = set()
        for p in peers:
            try:
                have.add(_norm_cidr(p))
            except ValueError:
                pass
        for c in add:
            if c not in have:
                peers.append(c)
                have.add(c)
                changed = True
        data["allow_peers"] = peers
    if changed:
        _write_config(paths.config, data)
        print("init-config: wrote %s" % paths.config)
    else:
        print("init-config: %s unchanged" % paths.config)
    return 0


def cmd_url(args, paths):
    try:
        data = _read_json(paths.config)
    except (OSError, ValueError):
        print("url: no readable %s; run %s init-config first" % (paths.config, CLI), file=sys.stderr)
        return 1
    token = data.get("token")
    if args.new_token:
        data["token"] = new_token()
        _write_config(paths.config, data)
        running = _systemctl("is-active", "--quiet", UNIT)[0] == 0        # the service reads the token only at start
        rc, err = _systemctl("try-restart", UNIT)
        if rc != 0:
            said = ", but restarting %s failed (%s): the old token stays valid until it restarts: %s restart" % (
                UNIT, err or "exit %s" % rc, CLI)
        else:
            said = "; %s restarted with it" % UNIT if running else "; %s is not running (it uses it when it starts)" % UNIT
        print("url: new token written" + said, file=sys.stderr)
    elif not isinstance(token, str) or (token != "" and not TOKEN_RE.match(token)):
        print("url: no valid token in %s; run %s init-config" % (paths.config, CLI), file=sys.stderr)
        return 1
    cfg, _ = load(paths.config)
    _write_out("".join(line + "\n" for line in url_lines(cfg, wlan0_ipv4())))
    return 0


def cmd_unit(args, paths_unused):
    """restart / start: `systemctl --user restart|start frameface.service`; its exit code (1 without systemctl)."""
    rc, err = _systemctl(args.cmd, UNIT)
    if rc == 0:
        print("%s: %s %s" % (args.cmd, UNIT, "restarted" if args.cmd == "restart" else "started"), file=sys.stderr)
        return 0
    print("%s: systemctl --user %s %s failed: %s" % (args.cmd, args.cmd, UNIT, err or "exit %s" % rc), file=sys.stderr)
    return 1 if rc is None else rc


def cmd_onoff(args, paths):
    if args.cmd == "on":
        try:
            os.remove(paths.disabled_flag)
        except FileNotFoundError:
            pass
        print("face tracking on")
    else:
        ensure_dir(paths.config_dir)
        fd = os.open(paths.disabled_flag, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        print("face tracking off")
    return 0


def cmd_status(args, paths):
    import http.client
    from .state import Store
    from .clock import RealClock
    cfg, _ = load(paths.config)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", cfg.http_port, timeout=3)
        conn.request("GET", "/status")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        if resp.status != 200:
            raise OSError("HTTP %d" % resp.status)
        st = json.loads(body.decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException) as e:
        rt = Store(paths, RealClock()).load_runtime()
        fault = rt.fault if isinstance(rt.fault, dict) else None
        if fault:
            name, sev, msg, hint = display("FAULT", {"fault": fault.get("kind"), "port": fault.get("port"),
                                                     "owner": fault.get("owner")})
            if args.json:
                print(json.dumps({"reachable": False, "fault": fault, "display": name, "message": msg, "hint": hint}))
            else:
                print("display: %s (%s)\nmessage: %s\nhint: %s\nsince: %s" % (name, sev, msg, hint, fault.get("since")))
        else:
            print("FrameFace is not answering on 127.0.0.1:%d (%s)" % (cfg.http_port, e))
        return EXIT_UNREACHABLE
    if args.json:
        print(json.dumps(st, indent=1))
        return 0
    s = st.get("stream") or {}
    print("display: %s (%s)\nmessage: %s\nhint: %s\nfps: in %s, out %s\nclients: %s" % (
        st.get("display"), st.get("severity"), st.get("message"), st.get("hint") or "-", s.get("fps_in"),
        s.get("fps_out"), (st.get("policy") or {}).get("clients")))
    return 0


def cmd_selftest(args, paths):
    from .selftest import run_selftest
    result = args.result or paths.selftest
    rc = run_selftest(args.lib, quick=args.quick, out=None if args.json else print, result_path=result)
    if args.json:
        with open(result, encoding="utf-8") as f:
            print(f.read().strip())
    return rc


def watch_signals(wake, signals=("SIGTERM", "SIGINT"), note=None):
    """Call wake() on these signals with no lock in the signal handler (run, sim and the active probe). CPython runs
    a Python handler in the main thread between two bytecodes, also while that thread holds a lock that wake() takes
    (stop's lock inside Event.wait(), Service.run's idle wait; a session's abort Event), so a handler calling
    stop.set() can deadlock (then no Block C, SIGKILL after TimeoutStopSec). The handler only notes the signal and
    calls note(signum), which takes no lock either (it may raise); signal.set_wakeup_fd makes CPython's C handler
    write its number to a non-blocking socketpair, and a daemon thread ("stop-signals") reading it calls wake()
    (repeats must be harmless). The wakeup fd comes first: a signal that finds the handler in place has written its
    byte, even one that arrives before the thread runs. Without a socketpair the thread polls the note every 50 ms.
    Returns restore(): the previous handlers and wakeup fd, the thread ended. Outside the main thread nothing is
    installed."""
    noted, done, old, pair, old_fd, thread = [False], [False], {}, None, -1, None

    def handler(signum, frame):
        noted[0] = True                         # no lock, no I/O
        if note is not None:
            note(signum)

    try:
        pair = socket.socketpair()
        pair[1].setblocking(False)
        old_fd = signal.set_wakeup_fd(pair[1].fileno(), warn_on_full_buffer=False)
    except (OSError, ValueError):
        for s in pair or ():
            s.close()
        pair = None
    for name in signals:
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                old[sig] = signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    def watch():
        while not done[0]:
            if pair is None:
                time.sleep(0.05)
            else:
                try:
                    data = pair[0].recv(256)
                except OSError:
                    return
                if not data or done[0]:
                    return
                noted[0] = noted[0] or any(b in old for b in data)
            if noted[0]:
                noted[0] = False
                wake()

    def restore():
        done[0] = True
        for sig, h in old.items():
            try:
                signal.signal(sig, h if h is not None else signal.SIG_DFL)
            except (ValueError, OSError, TypeError):
                pass
        if pair is not None:
            try:
                signal.set_wakeup_fd(old_fd)        # before the socket closes: its fd number may be reused
            except (ValueError, OSError):
                pass
            try:
                pair[1].send(b"\0")             # wakes the thread
            except OSError:
                pass
        if thread is not None:
            thread.join(1.0)
        for s in pair or ():
            s.close()
    if not old:                                 # no handler (not the main thread, no such signal): nothing to keep
        restore()
        return lambda: None
    thread = threading.Thread(target=watch, name="stop-signals", daemon=True)
    thread.start()
    return restore


def _install_stop(stop, svc, signals=("SIGTERM", "SIGINT")):
    """run and sim: stop.set() and svc.request_stop() on these signals (watch_signals: no lock in the handler)."""
    return watch_signals(lambda: (stop.set(), svc.request_stop()), signals)


def cmd_run(args, paths):
    if not sys.platform.startswith("linux"):
        print("run: the service needs Linux usbfs", file=sys.stderr)
        return 1
    import fcntl
    from .logutil import Log
    from .preprocess import make_preprocessor
    from .service import Service
    cfg, warnings = load(paths.config)
    fd = None
    try:
        if not os.path.isdir(os.path.dirname(paths.lock)):      # the state dir; /run/user/<uid> is never chmodded
            ensure_dir(os.path.dirname(paths.lock))
        fd = os.open(paths.lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if fd is not None:
            os.close(fd)
        print("run: lock %s held or unusable (%s); another FrameFace is running" % (paths.lock, e), file=sys.stderr)
        return 1
    sys.setswitchinterval(0.001)
    log = Log(cfg.log_level, paths.events)
    for w in warnings:
        log.w("config", w[len("config: "):] if w.startswith("config: ") else w)
    lib = os.path.join(_ARCHIVE, "lib")
    svc = Service(cfg, paths, log=log, pre=make_preprocessor(lib, cfg.preprocess, log))
    svc.lock_info = "ok"
    stop = threading.Event()
    restore = _install_stop(stop, svc)
    gc.freeze()
    try:
        rc = svc.run(stop)
    finally:
        restore()
    os.close(fd)
    return rc


def _parse_knob(text):
    key, sep, value = text.partition("=")
    if not sep or not key:
        raise ValueError("--knob needs key=value, not %r" % text)
    try:
        return key.strip(), json.loads(value)
    except ValueError:
        return key.strip(), value


def _trace_urb_bytes(path):
    """The URB size a trace was recorded with (its first SUBMITURB's length) as a config urb_bytes; 0 (the
    negotiated payload) if the trace has none or it is not a valid urb_bytes value."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line) if line.strip() else {}
                if isinstance(r, dict) and r.get("op") == "submit" and "err" not in r:
                    n = r.get("len")
                    return n if isinstance(n, int) and 4096 <= n <= 65536 and n % 512 == 0 else 0
    except (OSError, ValueError):
        pass
    return 0


def _build_helper(directory):
    """gcc csrc/ffproc.c -> DIR/libffproc.so (as install.sh builds it); False if that failed."""
    src = os.path.join(_ARCHIVE, "csrc", "ffproc.c")
    if not os.path.isdir(directory):
        ensure_dir(directory, 0o755)
    cmd = ["gcc", "-std=c99", "-O2", "-Wall", "-Wextra", "-fPIC", "-shared", "-o",
           os.path.join(directory, "libffproc.so"), src]
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120,
                              check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def cmd_sim(args, paths_unused):
    """The threaded service on the real clock against FakeKernel + FakeVft, on a temporary tree only
    (config, state, lock, sysfs/dev/proc); worn unknown; prints the URL, and one JSON line at exit. A replay
    uses the trace's own URB size (a recorded completion never exceeds the replaying URB)."""
    import tempfile
    from .clock import RealClock
    from .jpeg import JpegEncoder, JpegError, StubJpegEncoder
    from .logutil import Log
    from .preprocess import make_preprocessor
    from .service import Service
    from .sim import ReplayKernel, VftModel, make_rig
    from .worn import NO_WORN
    try:
        knobs = dict(_parse_knob(k) for k in args.knob or [])
        nets = [_norm_cidr(c) for c in args.allow or []]
    except ValueError as e:
        print("sim: %s" % e, file=sys.stderr)
        return 2
    scene, replay = args.scene, None
    if scene.startswith("replay:"):
        scene, replay = "mouth", os.path.join(scene[len("replay:"):], "trace.jsonl")
        if not os.path.isfile(replay):
            print("sim: no trace at %s" % replay, file=sys.stderr)
            return 2
    root = tempfile.mkdtemp(prefix="ffsim-")
    rig, restore = None, None
    try:
        try:
            model = VftModel(**dict({"scene": scene, "fps": args.fps}, **knobs))
        except TypeError as e:
            print("sim: unknown knob (%s)" % e, file=sys.stderr)
            return 2
        paths = temp_paths(root)
        rig = make_rig(model, clock=RealClock(), tmp=os.path.join(root, "rig"))
        usb = ReplayKernel(replay, rig.clock) if replay else rig.kernel
        lib = args.build_helper if args.build_helper and _build_helper(args.build_helper) else None
        log = Log("info", paths.events, stream=sys.stderr)
        pre = make_preprocessor(lib, "auto", log)
        enc = StubJpegEncoder() if args.stub_jpeg else None
        if enc is None:
            try:
                enc = JpegEncoder.load()
            except JpegError as e:
                print("sim: %s; using stub JPEGs" % e, file=sys.stderr)
                enc = StubJpegEncoder()
        cfg = Config(token="" if args.no_token else new_token(), allow_peers=list(DEFAULT_ALLOW_PEERS) + nets,
                     cpu_io="auto", cpu_work="auto", nice_io=0, urb_bytes=_trace_urb_bytes(replay) if replay else 0)
        svc = Service(cfg, paths, sys=usb, clock=rig.clock, sys_root=rig.sys_root, dev_root=rig.dev_root,
                      proc_root=rig.proc_root, encoder=enc, pre=pre, worn=NO_WORN, threaded=True, http=True,
                      log=log, bind=(args.listen, args.port))
        if not replay:
            svc.trace_note = rig.kernel.note
        stop = threading.Event()
        restore = _install_stop(stop, svc, ("SIGTERM", "SIGINT", "SIGHUP"))
        svc.startup()
        if not svc.http_port:
            print("sim: cannot listen on %s:%d" % (args.listen, args.port), file=sys.stderr)
            svc.stop()
            return 1
        host = HEADSET_AP_IP if args.listen in ("0.0.0.0", "") else args.listen
        print("http://%s:%d%s" % (host, svc.http_port, "/stream/%s" % cfg.token if cfg.token else "/stream"),
              flush=True)
        timer = None
        if args.seconds:
            timer = threading.Timer(args.seconds, lambda: (stop.set(), svc.request_stop()))
            timer.daemon = True
            timer.start()
        svc.run(stop)
        if timer is not None:
            timer.cancel()
        last = svc.drivers[-1] if svc.drivers else None
        k = rig.kernel
        print(json.dumps({"sim_exit": {"teardown": last.teardown_path if last else "none", "inflight": k.inflight(),
                                       "violations": list(k.violations), "frames": svc.frames_done,
                                       "parts_sent": svc.server.stats["parts"]}}), flush=True)
        return 0
    finally:
        if rig is not None:
            rig.close()
        shutil.rmtree(root, ignore_errors=True)
        if restore is not None:                 # last: a second Ctrl-C while tidying up must not interrupt it
            restore()


def cmd_probe(argv):
    try:
        from . import probe
    except ImportError as e:
        print("probe: not available in this installation (%s)" % e, file=sys.stderr)
        return 2
    return probe.main(argv)


# -- parser

def build_parser():
    p = argparse.ArgumentParser(prog="frameface_main.py", description="FrameFace %s: VIVE Facial Tracker camera "
                                "service for Baballonia on the Steam Frame." % __version__)
    p.add_argument("--version", action="version", version="FrameFace %s" % __version__)
    sub = p.add_subparsers(dest="cmd", metavar="<command>")
    sub.required = True
    sub.add_parser("run", help="run the service (systemd unit); takes the lock")
    s = sub.add_parser("status", help="state of the running service")
    s.add_argument("--json", action="store_true", help="print the raw /status JSON")
    s = sub.add_parser("url", help="print the Baballonia camera URL(s)")
    s.add_argument("--new-token", action="store_true", help="rotate the token first (restarts a running service)")
    sub.add_parser("on", help="enable face tracking (remove the kill-switch flag)")
    sub.add_parser("off", help="disable face tracking (create the kill-switch flag)")
    sub.add_parser("restart", help="restart the service (systemctl --user, also over SSH)")
    sub.add_parser("start", help="start the service (systemctl --user, also over SSH)")
    s = sub.add_parser("init-config", help="write ~/.config/frameface/config.json")
    s.add_argument("--force", action="store_true", help="rewrite the defaults (the token is kept)")
    s.add_argument("--new-token", action="store_true", help="generate a new token")
    s.add_argument("--allow-peer", action="append", metavar="CIDR", help="also allow this IPv4 CIDR")
    s = sub.add_parser("probe", help="diagnose the tracker (read-only unless --active)")
    s.add_argument("--quick", action="store_true", help="P2, P6, P7, P8, P12 only")
    s.add_argument("--json", action="store_true", help="print report.json")
    s.add_argument("--bundle-name", metavar="NAME", help="write the bundle to the state dir (probe-YYYYmmdd-HHMMSS)")
    s.add_argument("--active", action="store_true", help="characterise the hardware (IR on; asks first)")
    s.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    s.add_argument("--stop-service", action="store_true", help="stop the service for the active run")
    s.add_argument("--seconds", type=float, default=10, help="streaming time of A4")
    s.add_argument("--strategy", choices=["S0", "S1", "S2", "S3", "S4", "S5"], help="activation strategy")
    s.add_argument("--matrix", action="store_true", help="A5: every strategy")
    s.add_argument("--ir-sweep", action="store_true", help="A6: IR current sweep")
    s.add_argument("--soak", type=int, metavar="N", help="A7: N s streaming then idle")
    s.add_argument("--dump", type=int, metavar="N", help="A8: N raw frames into <bundle>-images")
    s.add_argument("--pixels", action="store_true", help="full payload trace into <bundle>-images")
    s.add_argument("--teardown-magic3", action="store_true", help="trailing MAGIC after the OFF writes")
    s.add_argument("--xu-len", type=int, metavar="N", help="A11: XU commands of N bytes")
    s.add_argument("--risky-settle-test", action="store_true", help="A9 (needs a TTY and two confirmations)")
    s.add_argument("--risky-live-ir", action="store_true", help="A10 (needs a TTY and two confirmations)")
    s = sub.add_parser("selftest", help="install gate: ABI, pixels, JPEG and a simulated stream")
    s.add_argument("--quick", action="store_true", help="skip the simulation")
    s.add_argument("--lib", metavar="DIR", help="directory with libffproc.so and abi_dump")
    s.add_argument("--json", action="store_true", help="print the result JSON")
    s.add_argument("--result", metavar="FILE", help="result file (default: the state dir's selftest.json)")
    s = sub.add_parser("sim", help="the full service against a simulated tracker (temporary state)")
    s.add_argument("--scene", default="mouth", help="mouth, bars, dark, noise, gradient or replay:DIR")
    s.add_argument("--fps", type=float, default=60.0)
    s.add_argument("--listen", default="127.0.0.1", help="address to listen on")
    s.add_argument("--port", type=int, default=19023)
    s.add_argument("--allow", action="append", metavar="CIDR", help="also allow this IPv4 CIDR")
    s.add_argument("--no-token", action="store_true", help="serve /stream without a token")
    s.add_argument("--knob", action="append", metavar="KEY=VALUE", help="a VftModel field (JSON value)")
    s.add_argument("--build-helper", metavar="DIR", help="compile csrc/ffproc.c into DIR and use it")
    s.add_argument("--stub-jpeg", action="store_true", help="marker-valid stub JPEGs (no TurboJPEG)")
    s.add_argument("--seconds", type=float, help="stop after N seconds")
    return p


def main(argv: list) -> int:
    argv = list(argv)
    setup_env()
    if argv and argv[0] == "probe" and not ({"-h", "--help"} & set(argv[1:])):
        return cmd_probe(argv[1:])
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
    paths = default_paths()
    handlers = {"run": cmd_run, "status": cmd_status, "url": cmd_url, "on": cmd_onoff, "off": cmd_onoff,
                "restart": cmd_unit, "start": cmd_unit, "init-config": cmd_init_config, "selftest": cmd_selftest,
                "sim": cmd_sim}
    return handlers[args.cmd](args, paths)
