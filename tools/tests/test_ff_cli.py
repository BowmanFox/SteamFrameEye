# SPDX-License-Identifier: MIT
"""The command line and logutil (spec §11.3 test_ff_cli, task T4): the `url` golden strings (a contract
shared with install-frameface.ps1) and its LAN line, init-config --allow-peer, the XDG_RUNTIME_DIR
default and the uid-derived lock, restart/start and `url --new-token` against a mock systemctl first
on PATH (it also writes to its stdout; a subprocess run of `url --new-token` checks that the process's
stdout is the URL alone), `status` without a service but with a FAULT(port) in runtime.json, on/off, the stop signals of
run/sim (no lock in the handler; Linux: one SIGTERM per stop from another process), `sim --scene
replay:` at the trace's URB size, the journal line repeat rule, events.jsonl rotation and recent(),
and --help of every subcommand. HOME (and USERPROFILE, which Windows' expanduser reads) point at a
temp tree in every case."""
import ffutil  # first: installs the port guard

import contextlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ffutil.setup_path()
from frameface import cli  # noqa: E402
from frameface.clock import RealClock  # noqa: E402
from frameface.config import Config, default_paths, load, temp_paths, write_default  # noqa: E402
from frameface.jpeg import StubJpegEncoder  # noqa: E402
from frameface.logutil import ROTATE_BYTES, Log  # noqa: E402
from frameface.mailbox import Latest  # noqa: E402
from frameface.preprocess import StubPreprocessor  # noqa: E402
from frameface.server import StreamServer  # noqa: E402
from frameface.service import Service  # noqa: E402
from frameface.sim import SimClock, TraceSys, VftModel, make_rig  # noqa: E402
from frameface.sim.babaclient import BabaClient  # noqa: E402
from frameface.state import Runtime, Store  # noqa: E402
from frameface.worn import NO_WORN  # noqa: E402

MAIN = str(ffutil.FRAMEFACE_DIR / "frameface_main.py")
TOKEN = "AbCdEfGhIjKlMnOpQrStUv"
MOCK_SYSTEMCTL = """import json, os, sys
with open(os.environ["FF_SYSTEMCTL_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "xdg": os.environ.get("XDG_RUNTIME_DIR")}) + "\\n")
sys.stdout.write("mock systemctl stdout: %s\\n" % " ".join(sys.argv[1:]))     # never part of what the CLI prints
sys.stdout.flush()
rc = json.loads(os.environ.get("FF_SYSTEMCTL_RC") or "{}").get(sys.argv[2] if len(sys.argv) > 2 else "", 0)
sys.stderr.write("mock systemctl: job failed\\n" if rc else "")
sys.exit(rc)
"""
SENDER = """import os, random, signal, sys, time
pid, ack, tick = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])
while os.read(ack, 1):                          # one SIGTERM per byte, at a random time within two ticks
    time.sleep(random.uniform(0.0, 2.0 * tick))
    os.kill(pid, signal.SIGTERM)
"""
STRESS = """import faulthandler, os, subprocess, sys, tempfile, threading
sys.path.insert(0, sys.argv[1])
from frameface import cli, service
from frameface.config import Config, temp_paths
from frameface.jpeg import StubJpegEncoder
from frameface.logutil import Log
from frameface.preprocess import StubPreprocessor
from frameface.worn import NO_WORN
n, tick, root = int(sys.argv[2]), sys.argv[3], tempfile.mkdtemp(prefix="ffsig-")
service.TICK_S = float(tick)                    # the production run() loop, only faster
for d in ("sys", "dev", "proc"):
    os.makedirs(os.path.join(root, d))
ack_r, ack_w = os.pipe()
sender = subprocess.Popen([sys.executable, "-I", "-c", sys.argv[4], str(os.getpid()), str(ack_r), tick], pass_fds=(ack_r,))
os.close(ack_r)
for i in range(n):
    svc = service.Service(Config(token=""), temp_paths(os.path.join(root, "t%d" % (i % 4))), sys=None,
                          sys_root=os.path.join(root, "sys"), dev_root=os.path.join(root, "dev"),
                          proc_root=os.path.join(root, "proc"), encoder=StubJpegEncoder(), pre=StubPreprocessor(),
                          worn=NO_WORN, threaded=True, http=False, log=Log("warning", None, stream=None))
    stop = threading.Event()
    restore = cli._install_stop(stop, svc)
    faulthandler.dump_traceback_later(20, exit=True)    # a stop that never ends: every stack, exit 1
    os.write(ack_w, b"x")                               # arms exactly one SIGTERM for this stop
    assert svc.run(stop) == 0 and svc.state == "STOPPED", (i, svc.state)
    faulthandler.cancel_dump_traceback_later()
    callable(restore) and restore()
os.close(ack_w)
sender.wait()
print("%d stops, one SIGTERM each" % n)
"""


class Home(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ffcli-")
        self.addCleanup(shutil.rmtree, self.home, True)
        env = {"HOME": self.home, "USERPROFILE": self.home}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.paths = default_paths(self.home)

    def run_cli(self, *argv):
        """(exit code, stdout bytes, stderr text) of cli.main in this process."""
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
            out.flush()
        return code, raw.getvalue(), err.getvalue()

    def config(self, **kv):
        write_default(self.paths.config, token=kv.pop("token", TOKEN))
        if kv:
            with open(self.paths.config, encoding="utf-8") as f:
                data = json.load(f)
            data.update(kv)
            with open(self.paths.config, "w", encoding="utf-8") as f:
                json.dump(data, f)

    def read_config(self):
        with open(self.paths.config, encoding="utf-8") as f:
            return json.load(f)

    def config_bytes(self):
        with open(self.paths.config, "rb") as f:
            return f.read()


class Url(Home):
    def test_golden_strings(self):
        self.config()
        with mock.patch.object(cli, "wlan0_ipv4", return_value=None):
            self.assertEqual(self.run_cli("url")[:2], (0, b"http://10.35.78.1:9023/stream/%s\n" % TOKEN.encode()))
        self.config(token="")
        with mock.patch.object(cli, "wlan0_ipv4", return_value="192.168.1.50"):
            self.assertEqual(self.run_cli("url")[:2], (0, b"http://10.35.78.1:9023/stream\n"))

    def test_second_line_only_for_wlan0_inside_an_allowed_lan_cidr(self):
        self.config(allow_peers=["10.35.78.0/24", "127.0.0.0/8", "192.168.4.0/24"], http_port=9123)
        with mock.patch.object(cli, "wlan0_ipv4", return_value="192.168.4.20"):
            self.assertEqual(self.run_cli("url")[1], b"http://10.35.78.1:9123/stream/%s\nhttp://192.168.4.20:9123/stream"
                                                     b"/%s\n" % (TOKEN.encode(), TOKEN.encode()))
        for ip in ("192.168.5.20", None):
            with mock.patch.object(cli, "wlan0_ipv4", return_value=ip):
                self.assertEqual(self.run_cli("url")[1], b"http://10.35.78.1:9123/stream/%s\n" % TOKEN.encode())
        cfg = Config(token="", allow_peers=["127.0.0.0/8"])
        self.assertEqual(cli.url_lines(cfg, "127.0.0.5"), ["http://10.35.78.1:9023/stream"])   # loopback never

    def test_lan_line_when_an_allowed_cidr_overlaps_wlan0s_network(self):      # datapath-4/ops-2: -AllowPeer <PC>/32
        cfg = Config(token=TOKEN, http_port=9123, allow_peers=["10.35.78.0/24", "127.0.0.0/8", "192.168.4.31/32"])
        ap, lan = "http://10.35.78.1:9123/stream/" + TOKEN, "http://%s:9123/stream/" + TOKEN
        self.assertEqual(cli.url_lines(cfg, "192.168.4.50/24"), [ap, lan % "192.168.4.50"])
        self.assertEqual(cli.url_lines(cfg, "192.168.4.203/22"), [ap, lan % "192.168.4.203"])
        for w in ("192.168.5.20/24", "192.168.4.50", None, "10.35.78.9/24", "bogus"):   # elsewhere, bare (/32), the AP net
            self.assertEqual(cli.url_lines(cfg, w), [ap], w)
        cfg.allow_peers = ["10.35.78.0/24", "127.0.0.0/8", "10.8.0.5/32"]
        self.assertEqual(cli.url_lines(cfg, "192.168.4.50/24"), [ap])
        ip = b"3: wlan0    inet 192.168.4.50/24 brd 192.168.4.255 scope global dynamic wlan0\\       valid_lft 85000sec\n"
        with mock.patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, ip, b"")):
            self.assertEqual(cli.wlan0_ipv4(), "192.168.4.50/24")             # the prefix is kept

    def test_no_config_or_no_token_prints_nothing(self):
        code, out, err = self.run_cli("url")
        self.assertEqual((code, out), (1, b""))
        self.assertIn("init-config", err)
        self.config()
        data = self.read_config()
        del data["token"]
        with open(self.paths.config, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.assertEqual(self.run_cli("url")[:2], (1, b""))

    def test_url_subprocess_writes_lf_only(self):
        self.config()
        env = dict(os.environ, HOME=self.home, USERPROFILE=self.home, PYTHONDONTWRITEBYTECODE="1")
        out = subprocess.run([sys.executable, "-I", "-B", MAIN, "url"], env=env, stdout=subprocess.PIPE, timeout=60).stdout
        self.assertTrue(out.startswith(b"http://10.35.78.1:9023/stream/%s\n" % TOKEN.encode()), out)
        self.assertNotIn(b"\r", out)


class InitConfig(Home):
    def test_fresh_config_has_defaults_and_a_token(self):
        code, _, _ = self.run_cli("init-config")
        cfg, warnings = load(self.paths.config)
        self.assertEqual((code, warnings), (0, []))
        self.assertRegex(cfg.token, r"^[A-Za-z0-9_-]{22}$")
        self.assertEqual(cfg.allow_peers, ["10.35.78.0/24", "127.0.0.0/8"])
        if os.name == "posix":
            self.assertEqual(os.stat(self.paths.config).st_mode & 0o777, 0o600)
        before = self.config_bytes()
        self.assertEqual(self.run_cli("init-config")[0], 0)             # nothing missing: unchanged
        self.assertEqual(self.config_bytes(), before)

    def test_allow_peer_appends_without_duplicates_and_rejects_invalid(self):
        self.config(extra_key=1)
        self.assertEqual(self.run_cli("init-config", "--allow-peer", "192.168.4.31/32")[0], 0)
        self.assertEqual(self.read_config()["allow_peers"], ["10.35.78.0/24", "127.0.0.0/8", "192.168.4.31/32"])
        self.assertEqual(self.run_cli("init-config", "--allow-peer", "192.168.4.31")[0], 0)      # normalised: same
        self.assertEqual(self.run_cli("init-config", "--allow-peer", "10.35.78.7/24", "--allow-peer",
                                      "192.168.9.0/24")[0], 0)
        data = self.read_config()
        self.assertEqual(data["allow_peers"], ["10.35.78.0/24", "127.0.0.0/8", "192.168.4.31/32", "192.168.9.0/24"])
        self.assertEqual((data["token"], data["extra_key"]), (TOKEN, 1))
        before = self.config_bytes()
        code, _, err = self.run_cli("init-config", "--allow-peer", "192.168.4.0/24", "--allow-peer", "300.1.1.1/8")
        self.assertEqual(code, 1)
        self.assertIn("invalid CIDR", err)
        self.assertEqual(self.config_bytes(), before)

    def test_new_token_and_force(self):
        self.config(http_port=9123)
        self.run_cli("init-config", "--new-token")
        data = self.read_config()
        self.assertNotEqual(data["token"], TOKEN)
        self.assertEqual(data["http_port"], 9123)
        self.run_cli("init-config", "--force")                           # defaults again, the token is kept
        forced = self.read_config()
        self.assertEqual((forced["token"], forced["http_port"]), (data["token"], 9023))


class Environment(Home):
    def test_xdg_runtime_dir_default_and_the_uid_lock(self):
        env = {}
        cli.setup_env(env, uid=1234)
        self.assertEqual(env, {"XDG_RUNTIME_DIR": "/run/user/1234"})
        env = {"XDG_RUNTIME_DIR": "/tmp/x"}
        cli.setup_env(env, uid=1234)
        self.assertEqual(env, {"XDG_RUNTIME_DIR": "/tmp/x"})
        run_root = tempfile.mkdtemp(prefix="ffrun-")
        self.addCleanup(shutil.rmtree, run_root, True)
        os.makedirs(os.path.join(run_root, "1234"))
        locks = set()
        for value in (None, "/tmp/x", os.path.join(run_root, "1234")):
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": value} if value else {}):
                if value is None:
                    os.environ.pop("XDG_RUNTIME_DIR", None)
                locks.add(default_paths(self.home, uid=1234, run_root=run_root).lock)
        self.assertEqual(locks, {os.path.join(run_root, "1234", "frameface.lock")})

    def test_main_sets_xdg_on_linux(self):
        os.environ.pop("XDG_RUNTIME_DIR", None)
        self.run_cli("on")
        if hasattr(os, "getuid"):
            self.assertEqual(os.environ["XDG_RUNTIME_DIR"], "/run/user/%d" % os.getuid())
        else:
            self.assertNotIn("XDG_RUNTIME_DIR", os.environ)

    def test_on_and_off(self):
        self.assertEqual(self.run_cli("off")[0], 0)
        self.assertTrue(os.path.exists(self.paths.disabled_flag))
        self.assertEqual(self.run_cli("off")[0], 0)
        self.assertEqual(self.run_cli("on")[0], 0)
        self.assertFalse(os.path.exists(self.paths.disabled_flag))
        self.assertEqual(self.run_cli("on")[0], 0)


class Unit(Home):
    """restart, start and url --new-token run `systemctl --user` (a mock, first on PATH) with XDG_RUNTIME_DIR=/run/user/<uid>
    (getuid mocked to 4242, also on Windows): datapath-3/ops-3."""

    def setUp(self):
        super().setUp()
        bindir, self.log = os.path.join(self.home, "bin"), os.path.join(self.home, "systemctl.jsonl")
        os.makedirs(bindir)
        script = os.path.join(bindir, "systemctl" if os.name == "posix" else "systemctl.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(("#!%s\n" % sys.executable if os.name == "posix" else "") + MOCK_SYSTEMCTL)
        if os.name == "posix":
            os.chmod(script, 0o755)
        else:                                                           # found through PATHEXT
            with open(os.path.join(bindir, "systemctl.bat"), "w", encoding="utf-8") as f:
                f.write('@"%s" -B "%s" %%*\n' % (sys.executable, script))
        for p in (mock.patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ.get("PATH", ""), "FF_SYSTEMCTL_LOG": self.log}),
                  mock.patch.object(os, "getuid", create=True, return_value=4242)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("XDG_RUNTIME_DIR", None)

    def calls(self):
        with contextlib.suppress(FileNotFoundError), open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]
        return []

    def test_restart_and_start(self):
        for verb, rc, said in (("restart", 0, "frameface.service restarted"), ("start", 0, "frameface.service started"),
                               ("restart", 5, "mock systemctl: job failed")):
            with mock.patch.dict(os.environ, {"FF_SYSTEMCTL_RC": json.dumps({verb: rc})}):
                code, out, err = self.run_cli(verb)
            self.assertEqual((code, out), (rc, b""), err)                      # systemctl's exit code
            self.assertEqual(self.calls()[-1], {"argv": ["--user", verb, "frameface.service"], "xdg": "/run/user/4242"})
            self.assertIn(said, err)
        with mock.patch.dict(os.environ, {"PATH": os.path.join(self.home, "nothing-here")}):
            code, out, err = self.run_cli("start")
        self.assertEqual((code, out), (1, b""))
        self.assertIn("systemctl not found", err)

    def test_url_new_token_restarts_a_running_service_and_keeps_the_rest(self):
        self.config(http_port=9123, extra_key="kept")
        for active, rc, said in ((0, 0, "frameface.service restarted"), (3, 0, "not running"), (0, 1, "mock systemctl: job failed")):
            with mock.patch.dict(os.environ, {"FF_SYSTEMCTL_RC": json.dumps({"is-active": active, "try-restart": rc})}), \
                    mock.patch.object(cli, "wlan0_ipv4", return_value=None):
                code, out, err = self.run_cli("url", "--new-token")
            data = self.read_config()
            self.assertEqual((code, out), (0, b"http://10.35.78.1:9123/stream/%s\n" % data["token"].encode()))   # the contract only
            self.assertIn(said, err)
            self.assertEqual((data["http_port"], data["extra_key"]), (9123, "kept"))
        self.assertNotEqual(data["token"], TOKEN)
        self.assertEqual([c["argv"] for c in self.calls()], [["--user", "is-active", "--quiet", "frameface.service"],
                                                             ["--user", "try-restart", "frameface.service"]] * 3)
        self.assertEqual({c["xdg"] for c in self.calls()}, {"/run/user/4242"})

    def test_url_new_token_prints_only_the_url_on_the_processs_stdout(self):     # B-2: run_cli sees Python's sys.stdout only,
        self.config()                                                           # the systemctl children write to fd 1
        env = dict(os.environ, HOME=self.home, USERPROFILE=self.home, PYTHONDONTWRITEBYTECODE="1",
                   FF_SYSTEMCTL_RC=json.dumps({"is-active": 0, "try-restart": 0}))
        p = subprocess.run([sys.executable, "-I", "-B", MAIN, "url", "--new-token"], env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=120)
        token = self.read_config()["token"]
        self.assertNotEqual(token, TOKEN)
        self.assertEqual((p.returncode, p.stdout), (0, b"http://10.35.78.1:9023/stream/%s\n" % token.encode()), p.stderr)
        self.assertIn(b"url: new token written; frameface.service restarted with it", p.stderr)
        self.assertNotIn(b"mock systemctl stdout", p.stderr)
        self.assertEqual([c["argv"] for c in self.calls()], [["--user", "is-active", "--quiet", "frameface.service"],
                                                             ["--user", "try-restart", "frameface.service"]])


class StopSignals(unittest.TestCase):
    """runtime-1: CPython runs a signal handler in the main thread between two bytecodes, also while that thread holds
    stop's lock inside Event.wait() (Service.run's idle wait), so the handler of run/sim must take no lock at all."""

    def service(self):
        root = tempfile.mkdtemp(prefix="ffstop-")
        self.addCleanup(shutil.rmtree, root, True)
        for d in ("sys", "dev", "proc"):
            os.makedirs(os.path.join(root, d))
        self.addCleanup(signal.signal, signal.SIGINT, signal.getsignal(signal.SIGINT))
        return Service(Config(token=""), temp_paths(os.path.join(root, "t")), sys=None, sys_root=os.path.join(root, "sys"),
                       dev_root=os.path.join(root, "dev"), proc_root=os.path.join(root, "proc"), encoder=StubJpegEncoder(),
                       pre=StubPreprocessor(), worn=NO_WORN, threaded=True, http=False, log=Log("info", None, stream=None))

    def test_the_handler_takes_no_lock_and_a_real_signal_still_stops_the_service(self):
        svc, stop = self.service(), threading.Event()
        restore = cli._install_stop(stop, svc, ("SIGINT",))             # SIGINT: harmless if a bug left it unhandled
        if callable(restore):
            self.addCleanup(restore)
        handler, returned = signal.getsignal(signal.SIGINT), threading.Event()

        def interrupted():                          # Event.wait() holds stop's lock in its prologue and epilogue
            with stop._cond:
                handler(signal.SIGINT, None)        # where CPython may run the handler (review: sig_real_service.py)
                returned.set()
        threading.Thread(target=interrupted, daemon=True).start()
        self.assertTrue(returned.wait(1.0), "the stop handler blocked on stop's lock: Block C would never run")
        timer = threading.Timer(0.3, signal.raise_signal, (signal.SIGINT,))     # a real signal while run() waits
        self.addCleanup(timer.cancel)
        timer.start()
        t0 = time.monotonic()
        self.assertEqual((svc.run(stop), svc.state, stop.is_set()), (0, "STOPPED", True))
        self.assertLess(time.monotonic() - t0, 5.0)

    def test_a_signal_just_after_the_handler_is_installed_still_stops_the_service(self):     # check-S: none lost at start
        svc, stop, real, fired, lost = self.service(), threading.Event(), signal.signal, [], []

        def installing(sig, handler):               # the signal arrives as soon as its handler is in place
            old = real(sig, handler)
            if sig == signal.SIGINT and not fired:
                fired.append(sig)
                signal.raise_signal(sig)
            return old
        with mock.patch.object(signal, "signal", installing):
            restore = cli._install_stop(stop, svc, ("SIGINT",))
        self.addCleanup(restore)
        safety = threading.Timer(5.0, lambda: (lost.append(1), stop.set(), svc.request_stop()))
        self.addCleanup(safety.cancel)
        safety.start()
        self.assertEqual((svc.run(stop), svc.state, fired, lost), (0, "STOPPED", [signal.SIGINT], []))

    def test_sim_keeps_its_handler_until_it_has_cleaned_up(self):     # check-S: a second Ctrl-C while sim tidies up
        from frameface import sim
        make, late, leaked, out, err = sim.make_rig, [], [], io.StringIO(), io.StringIO()

        def rig(*a, **kw):
            r = make(*a, **kw)
            close = r.close
            r.close = lambda: (late.append(signal.raise_signal(signal.SIGINT)), close())
            return r
        self.addCleanup(signal.signal, signal.SIGINT, signal.getsignal(signal.SIGINT))
        signal.signal(signal.SIGINT, lambda s, f: leaked.append(s))     # what sim's restore() puts back
        with mock.patch.object(sim, "make_rig", rig), mock.patch.dict(os.environ), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cli.main(["sim", "--stub-jpeg", "--no-token", "--port", str(ffutil.free_port("tcp")), "--seconds", "0.5"])
        self.assertEqual((code, len(late), leaked), (0, 1, []), err.getvalue()[-3000:])
        self.assertIn('"sim_exit"', out.getvalue())

    def test_one_sigterm_per_stop_from_another_process_always_stops_the_service(self):
        ffutil.require_native(reason="SIGTERM from another process, the way systemd stops the unit")
        p = subprocess.run([sys.executable, "-I", "-B", "-c", STRESS, str(ffutil.FRAMEFACE_DIR), "3000", "0.005", SENDER],
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=900)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, b"3000 stops, one SIGTERM each"), p.stderr.decode()[-4000:])


class SimReplay(Home):
    """usb-5: `sim --scene replay:DIR` replays with the URB size of the trace (its first SUBMITURB), never into smaller URBs."""

    def test_replay_of_a_trace_recorded_with_64_kib_urbs(self):
        rig, bundle = make_rig(VftModel()), os.path.join(self.home, "probe-urb")
        self.addCleanup(rig.close)
        os.makedirs(bundle)
        trace = TraceSys(rig.kernel, os.path.join(bundle, "trace.jsonl"), None, clock=rig.clock)
        svc = Service(Config(token="", urb_bytes=65536), temp_paths(os.path.join(self.home, "rec")), sys=trace, clock=rig.clock,
                      sys_root=rig.sys_root, dev_root=rig.dev_root, proc_root=rig.proc_root, encoder=StubJpegEncoder(),
                      pre=StubPreprocessor(), worn=lambda: True, threaded=False, http=False,
                      log=Log("info", None, stream=None, clock=rig.clock))
        svc.client_override, end = 1, rig.clock.monotonic() + 120.0
        while svc.frames_done < 400 and rig.clock.monotonic() < end:
            svc.step(0.05)
        svc.stop()
        trace.close_trace()
        self.assertEqual((svc.drivers[0].urb_bytes, svc.frames_done >= 400), (65536, True))
        errlog, port = os.path.join(self.home, "sim.err"), ffutil.free_port("tcp")
        with open(errlog, "wb") as err:                              # POSIX: SIGTERM once 32 live parts arrived
            proc = subprocess.Popen([sys.executable, "-I", "-B", MAIN, "sim", "--scene", "replay:" + bundle, "--stub-jpeg",
                                     "--no-token", "--port", str(port), "--seconds", "12" if os.name == "nt" else "150"],
                                    stdout=subprocess.PIPE, stderr=err, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
            self.addCleanup(lambda: proc.poll() is None and proc.kill())
            c = BabaClient(proc.stdout.readline().decode().strip()).start()
            self.addCleanup(c.stop)
            if os.name == "posix" and c.wait_frames(33, 120):             # + the placeholder
                proc.send_signal(signal.SIGTERM)
            out = proc.communicate(timeout=180)[0]
        with open(errlog, encoding="utf-8", errors="replace") as f:
            log = f.read()
        self.assertIn("I usb: streaming urbs=16 urb_bytes=65536", log, log[-3000:])
        self.assertGreater(json.loads(out.decode().splitlines()[-1])["sim_exit"]["frames"], 30, log[-3000:])
        self.assertEqual(cli._trace_urb_bytes(os.path.join(bundle, "trace.jsonl")), 65536)


class Status(Home):
    def test_unreachable_with_a_port_fault_prints_port_in_use_and_exits_3(self):
        port = ffutil.free_port("tcp")                  # nothing listens there
        self.config(http_port=port)
        store = Store(self.paths, RealClock())
        store.save_runtime(Runtime(boot_id=store.boot_id, fault={"kind": "port", "port": port, "errno": "EADDRINUSE",
                                                                  "owner": "holder pid 4242", "since": "2026-11-06T10:00:00Z"}))
        code, out, _ = self.run_cli("status")
        text = out.decode()
        self.assertEqual(code, 3)
        self.assertIn("display: port_in_use (error)", text)
        self.assertIn("Face stream port %d is in use" % port, text)
        self.assertIn("Stop holder pid 4242", text)
        code, out, _ = self.run_cli("status", "--json")
        doc = json.loads(out.decode())
        self.assertEqual((code, doc["display"], doc["fault"]["port"]), (3, "port_in_use", port))
        store.set_fault(None)
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 3)
        self.assertIn("not answering", out.decode())

    def test_reachable(self):
        doc = {"display": "streaming", "severity": "ok", "message": "Streaming 60 fps to 1 client", "hint": "",
               "stream": {"fps_in": 59.9, "fps_out": 41.0}, "policy": {"clients": 1}}
        cfg = Config(token=TOKEN, allow_peers=["127.0.0.0/8"])
        srv = StreamServer(cfg, Latest(), lambda: doc, RealClock(), None, Log("info", None, stream=None),
                           bind=("127.0.0.1", 0))
        port = srv.start()
        self.addCleanup(srv.stop)
        self.config(http_port=port)
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertEqual(out.decode().splitlines()[:2], ["display: streaming (ok)", "message: Streaming 60 fps to 1 client"])
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual((code, json.loads(out.decode())), (0, doc))


class LogUtil(unittest.TestCase):
    def test_repeats_are_suppressed_for_10_s(self):
        clock, stream = SimClock(), io.StringIO()
        log = Log("info", None, journal=True, stream=stream, clock=clock)
        for _ in range(4):
            log.w("http", "rejected", peer="10.0.0.9", reason="not_allowed")
            clock.advance(1.0)
        log.w("http", "bad token", peer="10.0.0.9")                       # another event: printed
        clock.advance(7.0)                                                 # 11 s after the first line
        log.w("http", "rejected", peer="10.0.0.8", reason="not_allowed")
        log.d("usb", "hidden at info")
        self.assertEqual(stream.getvalue().splitlines(), [
            "W http: rejected peer=10.0.0.9 reason=not_allowed",
            "W http: bad token peer=10.0.0.9",
            "W http: rejected peer=10.0.0.8 reason=not_allowed suppressed=3"])

    def test_timestamp_prefix_without_journal_and_quoting(self):
        stream = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JOURNAL_STREAM", None)
            Log("info", None, stream=stream).i("usb", "busy", owner="vftstream pid 812", ok=True, ms=1.5)
        self.assertRegex(stream.getvalue(), r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d I usb: busy owner="vftstream pid 812" '
                                            r'ok=yes ms=1\.5\n$')
        stream = io.StringIO()
        with mock.patch.dict(os.environ, {"JOURNAL_STREAM": "8:1234"}):
            Log("info", None, stream=stream).e("usb", "fault", kind="no_ir")
        self.assertEqual(stream.getvalue(), "E usb: fault kind=no_ir\n")

    def test_events_jsonl_rotation_and_recent(self):
        root = tempfile.mkdtemp(prefix="fflog-")
        self.addCleanup(shutil.rmtree, root, True)
        path = os.path.join(root, "state", "events.jsonl")
        clock = SimClock()
        log = Log("info", path, journal=True, stream=None, clock=clock)
        log.event("fault", kind="library", retry_s=float("inf"))
        with open(path, encoding="utf-8") as f:
            first = json.loads(f.readline())
        self.assertEqual((list(first)[:3], first["ev"], first["bt"], first["retry_s"]), (["t", "bt", "ev"], "fault",
                                                                                          1000.0, None))
        pad = "x" * 900
        for i in range(400):                                               # about 390 KiB in all
            log.event("tick", i=i, pad=pad)
        size = os.path.getsize(path)
        self.assertLessEqual(size, ROTATE_BYTES)
        self.assertTrue(os.path.exists(path + ".1"))
        self.assertFalse(os.path.exists(path + ".2"))
        self.assertLessEqual(os.path.getsize(path + ".1"), ROTATE_BYTES)
        with open(path, encoding="utf-8") as f:
            last = [json.loads(line) for line in f][-1]
        self.assertEqual(last["i"], 399)
        recent = log.recent(20)
        self.assertEqual(len(recent), 20)
        self.assertTrue(recent[-1].endswith("tick i=399 pad=" + pad))
        self.assertTrue(recent[0].endswith("tick i=380 pad=" + pad))
        self.assertRegex(recent[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d tick ")


class Help(unittest.TestCase):
    def test_every_subcommand_help_exits_0(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        for cmd in ([], ["run"], ["status"], ["url"], ["on"], ["off"], ["init-config"], ["probe"], ["selftest"],
                    ["sim"], ["restart"], ["start"]):
            p = subprocess.run([sys.executable, "-I", "-B", MAIN] + cmd + ["--help"], env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=60)
            self.assertEqual(p.returncode, 0, (cmd, p.stderr))
            self.assertTrue(re.match(rb"usage: frameface_main\.py", p.stdout), (cmd, p.stdout[:80]))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--version"]), 0)
        self.assertEqual(out.getvalue(), "FrameFace 0.6.0\n")


if __name__ == "__main__":
    unittest.main()
