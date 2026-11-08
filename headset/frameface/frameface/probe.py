# SPDX-License-Identifier: MIT
"""The probe (spec §5): `frameface_main.py probe [--quick] [--json] [--bundle-name NAME] [--active ...]`.

P1-P13 (default) only read sysfs, /proc, /etc, systemctl and journals, never the usbfs node. --active asks first, then runs the service's
own driver, assembler and health code through TraceSys under the uid-derived flock and the shared Store, with Block C and the service restart
in `finally` (SIGINT, SIGTERM, SIGHUP: ProbeAbort; no lock in the handler: cli.watch_signals' thread sets the open session's abort).
Bundles hold <= 16 payload bytes per record; pixels only in <name>-images/ (0700/0600).
Exit 0 no FAIL, 1 any FAIL, 2 refused. The sessions and steps A1-A12 are the mixin probe_active.ActiveSteps (the 600-line limit); dense on
purpose, method groups without blank lines.
"""
import argparse, contextlib, datetime, glob, json, os, platform, re, struct, subprocess, sys, threading, time  # noqa: E401
from collections import Counter

from . import __version__
from .clock import RealClock
from .config import atomic_write, default_paths, ensure_dir, load
from .descriptors import DescriptorError, diff_expected, parse, sha1_hex
from .jpeg import JpegEncoder, JpegError
from .logutil import Log
from .policy import HUB_HINT, IR_LEVELS, LADDER, display
from .preprocess import make_preprocessor
from .probe_active import IR_SWEEP, OK_S, ActiveSteps, _bytes, _kv
from .server import port_owners
from .sim.replay import TraceSys
from .state import Store
from .sysfs import find_owners, interface_drivers, kind, node_access, read_battery, read_power, read_typec, scan
from .usbfs import LinuxSys
from .vft import Aborted

__all__ = ["main", "ENV", "Probe", "ProbeAbort", "QUICK", "OK_S", "MARKER", "UNIT", "IR_SWEEP"]
LOCK_WAIT_S, CLI = 10.0, "python3 -I ~/frameface/frameface_main.py"       # CLI: as cli.CLI names the command line in hints
QUICK, MARKER, UNIT = ("P2", "P6", "P7", "P8", "P12"), "probe-stopped-service", "frameface.service"
_ARCHIVE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# main(argv, env) overrides any of these (tests): run(argv, env) -> (rc|None, out, err), status() -> /status, jpeg() -> encoder, usb: Sys
ENV = dict(home=None, uid=None, run_root="/run/user", sys_root="/sys", dev_root="/dev", proc_root="/proc", etc_root="/etc", usr_root="/usr",
           shm="/dev/shm/eye-server.mmap", lib_dir=None, usb=None, clock=None, run=None, status=None, jpeg=None, pre=None, stdin=None,
           stdout=None, tty=None, threaded=True, boot_id=None, note=None)
NAMES = dict(zip(["P%d" % i for i in range(1, 14)] + ["A%d" % i for i in range(1, 13)], "system libraries udev type-c power usb-devices "
                 "node-access drivers/owners descriptors eye-server network service kernel-log identity activation probe/commit stream "
                 "matrix ir-sweep soak dump settle-test live-ir xu-length teardown-ir".split()))
HINTS = {"P1": "FrameFace needs SteamOS on aarch64 with Python 3.10 or newer", "P5": "charge the headset: the tracker draws up to 0.5 A",
         "P3": 'if P7 fails, install the fallback rule ("udev fallback" in headset/frameface/README.md)', "A9": "keep settle_s at 8 s or more",
         "P10": 'worn detection reads the eye tracker; with eye tracking off set "gate_on_worn": false', "A7": "check heat and battery drain",
         "P12": "install the unit with install-frameface.ps1; restart it on the headset with: %s restart" % CLI,
         "P13": "error -71 means the USB link dropped (often a brown-out): use a direct C-to-C connection",
         "A2": "the tracker did not acknowledge the start sequence: unplug it for 10 s and run the probe again",
         "A4": "no healthy frames: run again with --matrix, --strategy S5 or --xu-len 384 and report the bundle",
         "A6": "a brown-out at this IR current: use a direct C-to-C connection", "A10": "live IR writes disturb the stream: keep the full teardown",
         "A12": 'set "teardown_magic3": true in ~/.config/frameface/config.json (check it with --teardown-magic3)',
         "service": "start it by hand: %s start" % CLI}
for _k, _s, _c in (("P2", "FAULT", "library"), ("P4", "NO_DEVICE", None), ("P6", "NO_DEVICE", None), ("P7", "FAULT", "permission"),
                   ("P8", "FAULT", "busy"), ("P9", "FAULT", "unsupported"), ("P11", "FAULT", "port"), ("A1", "FAULT", "unsupported")):
    HINTS[_k] = display(_s, {"fault": _c, "data_role": "device" if _k == "P4" else None})[3]      # the display table's hints (§4.18)
_int = lambda t: int(t) if t is not None and re.fullmatch(r"\s*-?\d+\s*", t) else None  # noqa: E731
_syserr = lambda rc, err: (err or "systemctl not found") if rc is None else (err or "").strip() if rc else ""  # noqa: E731
_pids = lambda owners: ", ".join("%s pid %d" % (c, p) for p, c in owners) or None  # noqa: E731


class ProbeAbort(Exception): pass               # SIGINT, SIGTERM or SIGHUP: the steps stop; Block C and the restart still run  # noqa


class Refused(Exception): pass                  # the probe does not run (exit 2): no terminal for a risky step, no confirmation, lock held  # noqa


def _read(path, binary=False, n=-1):            # file content (text: stripped; at most n bytes), None if unreadable
    with contextlib.suppress(OSError, ValueError), open(path, "rb") as f:
        data = f.read(n)
        return data if binary else data.decode("utf-8", "replace").strip()


def _run(argv, env):
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30, check=False)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as e:
        return None, "", str(e)


def _flock(fd):                                 # the service's flock; Windows test runs: a one-byte msvcrt lock (closing releases it)
    if os.name == "nt":
        import msvcrt; return msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # noqa: E702
    import fcntl; fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # noqa: E401,E702


class Probe(ActiveSteps):
    def __init__(self, args, env, argv):
        if set(env) - set(ENV):
            raise TypeError("unknown probe env keys %s" % sorted(set(env) - set(ENV)))
        e = self.e = dict(ENV, **env)
        self.args, self.clock, self.lines, self.checks, self.settles = args, e["clock"] or RealClock(), [], [], []
        uid = e["uid"] if e["uid"] is not None else (os.getuid() if hasattr(os, "getuid") else None)
        self.paths = default_paths(e["home"] or os.path.expanduser("~"), uid=uid, run_root=e["run_root"])
        (self.cfg, warnings), self.run_cmd, self.stdin, self.stdout = load(self.paths.config), e["run"] or _run, e["stdin"] or sys.stdin, \
            e["stdout"] or sys.stdout
        self.status_fn = e["status"] or (lambda: json.loads(__import__("urllib.request").request.urlopen(
            "http://127.0.0.1:%d/status" % self.cfg.http_port, timeout=2).read().decode("utf-8")))
        self.store, self.log = Store(self.paths, self.clock, boot_id=e["boot_id"]), Log("debug", None, journal=True, stream=None, clock=self.clock)
        self.tty = e["tty"] if e["tty"] is not None else bool(getattr(self.stdin, "isatty", lambda: False)())
        self.lib, self.marker = e["lib_dir"] or os.path.join(_ARCHIVE, "lib"), os.path.join(self.paths.state_dir, MARKER)
        self.report = {"v": 1, "version": __version__, "argv": argv, "mode": "active" if args.active else "quick" if args.quick else "read-only",
                       "config_warnings": warnings, "teardown_ir": None, "profile": {"action": "unchanged"}, "service": {"stopped": False,
                       "restarted": None}, "aborted": None, "aborted_step": None}
        self.vft = self.desc = self.ses = self.lock_fd = self.trace = self.bundle = self.images = self.name = self.good = None
        self.pre, self.enc, self.aborted, self.step, self.readback, self.tdir = e["pre"], None, None, None, None, None
        self.bus_error = self.stopped = self.base_ok = self.wrote = self._raise_ok = self._cleanup = self.pwm_set = self.pwm_shown = False
        self.enum_bt = self.start_bt = self.clock.boottime()
        self.dirty_start, self._restore, self.magic3 = False, None, bool(args.teardown_magic3 or self.cfg.teardown_magic3)
        self.stop_req = threading.Event()               # set by the "stop-signals" thread only (_wake)

    # -- output, signals, prompts, small helpers
    def _say(self, text, line=True):            # a report line (summary.txt; not printed with --json) or raw text (prompts, JSON)
        self.lines += [text] if line else []
        with contextlib.suppress(OSError, ValueError, AttributeError):  # a terminal that hung up (SIGHUP): the run goes on
            (line and self.args.json) or (self.stdout.write(text + "\n" if line else text), self.stdout.flush())
    def _chk(self, cid, status, detail=None, hint=None, lines=None, **data):
        hint = HINTS.get(cid, "report the bundle (install-frameface.ps1 -ProbeActive copies it)") if hint is None else hint
        name, detail = NAMES.get(cid, cid), detail or _kv(data)
        self.checks.append({"id": cid, "name": name, "status": status, "detail": detail, "hint": hint, "data": data})
        for text in lines or [detail]:
            self._say("[%s] %s %s: %s%s" % (status, cid, name, text, " (hint: %s)" % hint if status != "PASS" else ""))
    def _signals(self, on):                     # cli.watch_signals, as run's: no lock in the handler; restored in run()'s `finally`
        from .cli import watch_signals
        if on:
            self._restore = watch_signals(self._wake, ("SIGINT", "SIGTERM", "SIGHUP"), self._on_signal)
        elif self._restore is not None:
            self._restore(); self._restore = None  # noqa: E702
    def _on_signal(self, signum, frame=None):
        # the Python handler (SIGINT/SIGTERM/SIGHUP alike) takes no lock: ProbeAbort at the next check, at once in a prompt;
        # _wake, in the "stop-signals" thread, has the open session's driver abandon Block A/B at a transfer boundary
        self.aborted = self.aborted or {1: "SIGHUP", 2: "SIGINT", 15: "SIGTERM"}.get(signum, "signal %d" % signum)
        if self._raise_ok and not self._cleanup:
            self._raise_ok = False; raise ProbeAbort(self.aborted)  # noqa: E702
    def _wake(self):                            # the "stop-signals" thread (locks are fine there); a session that opens later
        self.stop_req.set(); ses = self.ses; ses is not None and ses.drv.abort.set()  # noqa: E702   # sees stop_req (_session)
        self.e["note"] and self.e["note"](stop=True)    # tests: after the abort, as Service.request_stop (sim.invariants)
    def _check(self):
        if self.aborted and not self._cleanup: raise ProbeAbort(self.aborted)  # noqa: E701
    def _ask(self, prompt):
        self._say(prompt, False); self._raise_ok, line = True, ""  # noqa: E702
        try:
            with contextlib.suppress(OSError, ValueError, EOFError, AttributeError):     # AttributeError: no stdin at all
                line = self.stdin.readline() or ""
        finally:
            self._raise_ok = False
        return self._check() or line.strip().lower()
    def _sleep(self, s, quiet=False):           # short slices (inline mode: pumping the open session); ProbeAbort unless quiet
        end = self.clock.monotonic() + s
        while self.clock.monotonic() < end:
            pump = self.ses is not None and not self.e["threaded"] and self.ses.drv.urbs.slots and (quiet or not self._check())
            self.ses.drv.pump(min(end - self.clock.monotonic(), 0.01)) if pump else self.clock.sleep(min(end - self.clock.monotonic(), 0.1))
            quiet or self._check()
    def _sysctl(self, *args): return self.run_cmd(["systemctl", "--user"] + list(args), dict(os.environ))  # noqa: E704
    def _is_ff(self, pid): return b"frameface_main.py" in (_read(os.path.join(self.e["proc_root"], str(pid), "cmdline"), True) or b"")  # noqa
    def _need(self, cid): return self.vft is not None or self._chk(cid, "WARN", "not checked: no tracker", HINTS["P6"])  # noqa: E704
    def _pm(self): return read_power(self.e["sys_root"], self.vft.sysname)  # noqa: E704
    def _sys(self, *p): return os.path.join(self.e["sys_root"], *p)  # noqa: E704
    def _since(self, mark, op): recs = list(self.trace.records); return [r for r in recs[next((k for k, r in enumerate(recs) if r is mark), -1) + 1:] if r["op"] == op]  # noqa
    def _pre(self): self.pre = self.pre or make_preprocessor(self.lib, self.cfg.preprocess, None); return self.pre  # noqa: E702,E704
    def _power(self):                           # battery, USB input current (iio, mA), VBUS (mV), battery current_now (uA), temps (C)
        val, iio = lambda p: next((v for v in map(_int, map(_read, sorted(p))) if v is not None), None), self._sys("bus", "iio", "devices", "*")
        temps = {(_read(os.path.join(z, "type")) or z): _int(_read(os.path.join(z, "temp"))) for z in sorted(glob.glob(self._sys(
            "class", "thermal", "thermal_zone*")))}
        return dict(read_battery(self.e["sys_root"]), iin_ma=val(glob.glob(os.path.join(iio, "in_current_pm8550b_iin_fb_input"))),
                    vbus_mv=val(glob.glob(os.path.join(iio, "in_voltage_pm8550b_usb_sns_v_div_16_input"))), battery_ua=val(
                        os.path.join(os.path.dirname(t), "current_now") for t in glob.glob(self._sys("class", "power_supply", "*", "type"))
                        if (_read(t) or "").lower() == "battery"), temps=temps,
                    temp_c=max((v / 1000.0 for v in temps.values() if v is not None), default=None))

    # -- read-only part (§5.1)
    def _readonly(self):
        for cid in [c for c in ("P%d" % i for i in range(1, 14)) if not self.args.quick or c in QUICK]:
            self.step = cid
            try:
                getattr(self, "_" + cid.lower())()
            except ProbeAbort:
                raise
            except Exception as e:              # a check never takes the probe down
                self._chk(cid, "WARN", "check failed: %r" % e)
    def _p1(self):
        osr = dict(re.findall(r'^([A-Z_]+)="?([^"\n]*)"?$', _read(os.path.join(self.e["etc_root"], "os-release")) or "", re.M))
        self._chk("P1", "PASS", version_id=osr.get("VERSION_ID"), build_id=osr.get("BUILD_ID"), kernel=platform.release(),
                  python=platform.python_version(), arch=platform.machine(), cpu_capacity=[_int(_read(p)) for p in sorted(glob.glob(
                      self._sys("devices", "system", "cpu", "cpu*", "cpu_capacity")))])
    def _p2(self):
        try:
            enc, why = (self.e["jpeg"] or JpegEncoder.load)(), None
            api = enc.api; enc.close()  # noqa: E702
        except (JpegError, OSError) as ex:
            api, why = None, str(ex)
        st, helper = None, make_preprocessor(self.lib, "c")
        with contextlib.suppress(ValueError):
            st = json.loads(_read(self.paths.selftest) or "null")
        self._chk("P2", "FAIL" if api is None else "WARN" if helper.name != "c" else "PASS", None, None if api is None else "run install.sh again: "
                  "it builds lib/libffproc.so with gcc (SWAR runs at <= 30 fps)", jpeg=api or "missing", jpeg_error=why, helper=helper.name,
                  helper_reason=getattr(helper, "reason", None), selftest_exit=st.get("exit") if isinstance(st, dict) else None, selftest=st)
    def _p3(self):
        valve = _read(os.path.join(self.e["usr_root"], "lib", "udev", "rules.d", "00-usb-permissions.rules")) or ""
        self._chk("P3", "PASS", valve_rules=[x.strip() for x in valve.splitlines() if x.strip()[:1] not in ("", "#")],
                  fallback_rule=os.path.exists(os.path.join(self.e["etc_root"], "udev", "rules.d", "70-vive-facial-tracker.rules")))
    def _p4(self): tc = read_typec(self.e["sys_root"]); self._chk("P4", "WARN" if tc["data_role"] == "device" else "PASS", **tc)  # noqa
    def _p5(self): self._chk("P5", "PASS", **self._power())  # noqa: E704
    def _p6(self):
        nodes = scan(self.e["sys_root"], self.e["dev_root"])
        devs = ["%04x:%04x %s %d Mbit/s %s" % (n.vid, n.pid, n.sysname, n.speed_mbps, _read(self._sys("bus", "usb", "devices", n.sysname, "product")))
                for n in nodes]
        v = self.vft = next((n for n in nodes if kind(n) == "vft"), None)
        if v is None:
            return self._chk("P6", "FAIL", "no VIVE Facial Tracker (0bb4:0321)%s" % ("; the VIVE Focus facial tracker 0bb4:06a1 is not "
                             "supported" if any(kind(n) == "focus3" for n in nodes) else ""), devices=devs)
        self.desc, self.enum_bt = v.descriptors, self.clock.boottime()
        self._chk("P6", "WARN" if v.behind_hub else "PASS", None, HUB_HINT if v.behind_hub else None, sysname=v.sysname, speed=v.speed_mbps,
                  hub=v.behind_hub, node=v.devpath, **self._pm(), devices=devs)
    def _p7(self):
        if self._need("P7"):
            acc = node_access(self.vft.devpath)
            groups, owner = [], acc["uid"]
            with contextlib.suppress(ImportError, KeyError, OSError, TypeError):
                import grp, pwd  # noqa: E401
                groups, owner = [grp.getgrgid(g).gr_name for g in os.getgroups()], pwd.getpwuid(acc["uid"]).pw_name
            self._chk("P7", "PASS" if acc["writable"] else "FAIL", node=self.vft.devpath, mode=acc["mode"], owner=owner, group=acc["group"],
                      writable=acc["writable"], user_groups=groups)
    def _p8(self):
        if self._need("P8"):
            owners = [(p, c) for p, c in find_owners(self.vft.devpath, self.e["proc_root"]) if p != os.getpid()]
            busy = _pids((p, c) for p, c in owners if not self._is_ff(p))
            self._chk("P8", "FAIL" if busy else "PASS", None, display("FAULT", {"fault": "busy", "owner": busy})[3], busy=busy,
                      drivers={str(k): v for k, v in interface_drivers(self.e["sys_root"], self.vft.sysname).items()}, open_by=_pids(owners))
    def _p9(self):
        if not self._need("P9"): return  # noqa: E701
        try:
            lay = parse(self.desc)
        except DescriptorError as ex:
            return self._chk("P9", "FAIL", "descriptors unusable: %s" % ex, hex=self.desc.hex())
        diffs, tree = list(lay.warnings) + diff_expected(lay, "hs"), dict(vars(lay))      # §5.1: against the HS persona, whatever the speed
        tree.update(xu_units=[u.unit_id for u in lay.xu_units], format_guid=lay.format_guid.hex(), warnings=None)
        self._chk("P9", "WARN" if diffs else "PASS", "; ".join(diffs) or "%d bytes as expected" % len(self.desc), lines=diffs or None,
                  sha1=sha1_hex(self.desc), hex=self.desc.hex(), diff=diffs, tree=tree)
    def _p10(self):
        eye = lambda b: b and len(b) >= 0x40 and struct.unpack_from("<II", b) + struct.unpack_from("<I", b, 0x38)  # noqa: E731
        h1 = eye(_read(self.e["shm"], True, 0x40)); h2 = self._sleep(0.5) or eye(_read(self.e["shm"], True, 0x40))  # noqa: E702
        self._chk("P10", "PASS", present=bool(h1), abi=h1 and h1[0], initialized=h1 and h1[1], seq_moving=bool(h1 and h2 and h1[2] != h2[2]))
    def _p11(self):
        port, (rc, out, err) = self.cfg.http_port, self.run_cmd(["ip", "-4", "-o", "addr", "show", "dev", "wlanap"], dict(os.environ))
        owners, fault = port_owners(port, self.e["proc_root"]), self.store.load_runtime().fault
        foreign = _pids((p, c) for p, c in owners if not self._is_ff(p))
        self._chk("P11", "WARN" if foreign or (fault or {}).get("kind") == "port" else "PASS", None, display("FAULT", {"fault": "port", "port": port, "owner": foreign})[3],
                  wlanap=rc == 0 and "10.35.78.1/" in out, port=port, listening=_pids(owners), fault=fault)
    def _p12(self):
        res = {verb: self._sysctl(verb, UNIT) for verb in ("is-enabled", "is-active")}
        errors = [_syserr(rc, err) for rc, out, err in res.values() if _syserr(rc, err)]
        self.bus_error = any(rc is None or "bus" in _syserr(rc, err).lower() for rc, out, err in res.values())
        try:
            st = self.status_fn()
        except Exception as ex:                 # not running, refused, bad JSON: information only
            st = {"display": None, "message": "not answering (%s)" % ex}
        self._chk("P12", "WARN" if errors else "PASS", ("systemctl --user failed: %s; " % errors[0] if errors else "") + "enabled=%s active=%s "
                  "display=%s message=%s" % (res["is-enabled"][1].strip() or "?", res["is-active"][1].strip() or "?", st.get("display"), st.get(
                      "message")), systemctl={k: list(v) for k, v in res.items()}, service_status=st, events=(_read(self.paths.events) or "").splitlines()[-20:])
    def _p13(self):
        rc, out, err = self.run_cmd(["journalctl", "-k", "-b", "-n", "300", "--no-pager"], dict(os.environ))
        found = [x for x in (out or "").splitlines() if re.search(r"usb|dwc3|xhci|error -71|over-current", x, re.I)]
        self._chk("P13", "WARN" if any("error -71" in x for x in found) else "PASS", readable=rc == 0, error=None if rc == 0 else (
            err or "").strip() or rc, usb_lines=len(found), error_71=sum("error -71" in x for x in found), kernel_lines=found[-100:])

    # -- active part (§5.2)
    def _active(self):
        a = self.args
        risky = [s for s, on in (("A9 --risky-settle-test", a.risky_settle_test), ("A10 --risky-live-ir", a.risky_live_ir), (
            "A11 --xu-len %s" % a.xu_len, a.xu_len not in (None, 384))) if on]
        try:
            if risky and not self.tty: raise Refused("%s can wedge the tracker until a replug: needs a terminal (ssh -t)" % ", ".join(risky))  # noqa
            if not a.yes and self._ask("The active probe switches the tracker's IR LEDs on%s. Continue? [y/N] " % (
                    " and stops the face service for 1-3 minutes" if a.stop_service else "")) not in ("y", "yes"):
                raise Refused("not confirmed")
            if risky and (self._say("Risky: %s can wedge the tracker until you unplug it." % ", ".join(risky)) or any(
                    self._ask("Type yes to continue: ") != "yes" for _ in range(2))):
                raise Refused("not confirmed twice")
        except (Refused, ProbeAbort) as e:
            return self._say("probe: refused: %s" % e) or 2
        refused, self.bundle = None, os.path.join(self.paths.state_dir, self.name)
        self.images = self.bundle + "-images" if a.pixels or a.dump else None
        try:
            [ensure_dir(d) for d in (self.bundle, self.images) if d]
            self._readonly()
            if self.vft is not None:
                self.trace = TraceSys(self.e["usb"] or LinuxSys(), os.path.join(self.bundle, "trace.jsonl"), a.pixels and os.path.join(
                    self.images, "trace-pixels.jsonl"), clock=self.clock)
                a.stop_service and self._service("stop"); self._take_lock(); self._steps()  # noqa: E702
        except (ProbeAbort, Aborted) as e:
            self.report.update(aborted=self.aborted or str(e), aborted_step=self.step)
            self._chk("probe", "FAIL", "aborted by %s during %s" % (self.aborted or e, self.step))
        except Refused as e:
            refused = self._say("probe: refused: %s" % e) or str(e)
        except Exception as e:                  # a bug: Block C, the restart and the report still happen
            self._chk("probe", "FAIL", "internal error during %s: %r" % (self.step, e))
        finally:
            self._cleanup = True
            self.ses is not None and self._close(self.ses)
            self.lock_fd is not None and os.close(self.lock_fd)            # releases the flock
            self.stopped and self._service("start")
            self.trace is not None and self.trace.close_trace()
        return self._finish(refused)
    def _service(self, verb):                   # stop: the marker first; start (in `finally`): the marker goes only if the start worked
        if verb == "stop":
            ensure_dir(self.paths.state_dir); atomic_write(self.marker, _bytes(datetime.datetime.now().isoformat() + "\n"))  # noqa: E702
        rc, out, err = self._sysctl(verb, UNIT)
        ok, self.stopped = rc == 0, self.stopped or (verb == "stop" and rc == 0)
        (verb == "stop") != ok and os.path.exists(self.marker) and os.remove(self.marker)    # a failed stop: nothing to restart
        self.report["service"]["stopped" if verb == "stop" else "restarted"] = ok
        self._chk("service", "PASS" if ok else "WARN", "%s %s" % (UNIT, "stopped (started again when the probe ends)" if verb == "stop" else
                  "started again") if ok else "systemctl --user %s failed: %s" % (verb, _syserr(rc, err) or rc))
    def _take_lock(self):                       # the unit's own flock (uid-derived path), waiting <= 10 s for a stopping service
        path, end, procs = self.paths.lock, self.clock.monotonic() + LOCK_WAIT_S, self.e["proc_root"]
        os.path.isdir(os.path.dirname(path)) or ensure_dir(os.path.dirname(path))
        fd = self.lock_fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)    # `finally` closes it, locked or not
        while True:
            try:
                _flock(fd); break  # noqa: E702
            except OSError:
                if self.clock.monotonic() >= end:
                    raise Refused("%s is held: FrameFace is running (stop it, or use --stop-service)" % path) from None
                self._sleep(0.2)
        for pid in (p for p in os.listdir(procs) if p.isdigit() and int(p) != os.getpid()) if self.bus_error else ():
            argv = (_read(os.path.join(procs, pid, "cmdline"), True) or b"").split(b"\0")
            if any(x.endswith(b"frameface_main.py") and argv[i + 1:i + 2] == [b"run"] for i, x in enumerate(argv)):
                raise Refused("the user bus is unreachable and frameface_main.py run is running (pid %s)" % pid)
        rt = self.store.load_runtime()
        self.start_bt, self.dirty_start = self.clock.boottime(), self.store.same_boot(rt) and rt.dirty
    def _steps(self):
        a, cfg, prof = self.args, self.cfg, self.store.load_profile(sha1_hex(self.desc))
        self.known, self.prof_n = (prof.strategy, prof.xu_len) if prof else (None, None)
        self.rung = a.strategy or (cfg.strategy if cfg.strategy != "auto" else None) or self.known or LADDER[0]
        self.cur = cfg.ir_current if cfg.ir_current is not None else IR_LEVELS[cfg.ir_level if cfg.ir_level != "auto" else prof.ir_level if prof else "L0"]
        self.phase = 0 if (cfg.y_phase if cfg.y_phase != "auto" else prof and prof.y_phase) == "even" else 1
        self.col0 = 200 if cfg.camera == "right" else 0
        ses, st = self._main(self.rung, None, "A4")
        self.base_ok = st is not None and st.best >= OK_S; self.good = (self.rung, ses.n) if self.base_ok else None  # noqa: E702
        for on, sid, fn in ((a.dump, "A8", self._a8), (a.risky_live_ir, "A10", self._a10), (a.soak, "A7", self._a7)):
            on and (setattr(self, "step", sid) or fn(ses) if self.ses is ses else self._chk(sid, "WARN", "not run: the A4 session ended", HINTS["A4"]))
        self.ses is ses and self._close(ses)
        if a.matrix:
            self.step, rows = "A5", [self._cand(r, ses.n, st) or row for r in LADDER for ses, st, row in [self._run(r)]]
            ok = [r["strategy"] for r in rows if r["ok_run_s"] >= OK_S]
            self._chk("A5", "PASS" if ok else "WARN", "healthy frames with %s" % (", ".join(ok) or "no strategy"), rungs=rows)
        a.ir_sweep and (setattr(self, "step", "A6") or self._a6())
        if a.risky_settle_test:
            self.step, rows = "A9", [dict(self._run(*(self.good or (self.rung, None)), secs=3.0, settle=d)[2], delay_s=d) for d in (1, 2, 4, 8)]
            self._chk("A9", "PASS", "; ".join("%d s after a teardown: %s" % (r["delay_s"], "streams" if r["ok_run_s"] >= OK_S else r["error"]
                                                                             or "no healthy frames") for r in rows), tries=rows)
        a.xu_len and self._cand(self.rung, a.xu_len, self._main(self.rung, a.xu_len, "A11")[1])

    # -- A12, report, bundle, entry
    def _finish(self, refused=None):
        self._a12()                             # probe_active: PASS only on a [0, 0, 0] readback
        n = Counter(c["status"] for c in self.checks)
        code = 2 if refused else 1 if n["FAIL"] else 0
        self.report.update(when=datetime.datetime.now().astimezone().isoformat(), exit=code, refused=refused, summary=dict(n),
                           checks=self.checks, settle_waits=self.settles, bundle=self.bundle, images=self.images,
                           state_warnings=self.store.warnings, log=list(self.log.lines))
        self._say("probe: %d PASS, %d WARN, %d FAIL; exit %d%s" % (n["PASS"], n["WARN"], n["FAIL"], code, "; bundle %s" % self.bundle if self.bundle else ""))
        try:
            if self.bundle:
                ensure_dir(self.bundle)
                rc, out, err = self.run_cmd(["journalctl", "--user", "-u", UNIT, "-b", "-n", "500", "--no-pager"], dict(os.environ))
                for name, data in (("report.json", json.dumps(self.report, indent=1, default=str) + "\n"), ("summary.txt", "\n".join(self.lines) + "\n"),
                                   ("descriptors.bin", self.desc), ("events.jsonl", _read(self.paths.events, True)), ("journal.txt", out if rc == 0 else None)):
                    data is not None and atomic_write(os.path.join(self.bundle, name), _bytes(data))
        except OSError as e:
            self._say("probe: bundle not written: %s" % e)
        self.args.json and self._say(json.dumps(self.report, indent=1, default=str) + "\n", False)
        return code
    def run(self):
        a = self.args
        self.name = a.bundle_name or (time.strftime("probe-%Y%m%d-%H%M%S") if a.active else None)
        why = "--xu-len must be 17..1024" if a.xu_len is not None and not 17 <= a.xu_len <= 1024 else "--bundle-name must be probe-<name>" \
            if self.name and (self.name == MARKER or not re.fullmatch(r"probe-[A-Za-z0-9._-]{1,58}", self.name)) else "--seconds, " \
            "--soak and --dump must be positive" if a.seconds <= 0 or (a.soak or 0) < 0 or (a.dump or 0) < 0 else None
        if why: return self._say("probe: refused: %s" % why) or 2  # noqa: E701
        if not a.active:
            self.bundle = self.name and os.path.join(self.paths.state_dir, self.name)
            return self._readonly() or self._finish()
        self._signals(True)
        try:
            return self._active()
        finally:
            self._signals(False)


def main(argv: list, env: "dict | None" = None) -> int:
    """`frameface_main.py probe ...` (cli.py hands the arguments over unchanged); env overrides ENV (tests)."""
    from .cli import build_parser, setup_env
    parser = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)).choices["probe"]
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    setup_env(uid=(env or {}).get("uid"))
    return Probe(args, dict(env or {}), list(argv)).run()
