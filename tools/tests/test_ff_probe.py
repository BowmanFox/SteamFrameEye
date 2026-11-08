# SPDX-License-Identifier: MIT
"""The probe (spec §11.3 test_ff_probe, task T5): the read-only part P1-P13 on a fake tree (an opener spy proves that no node
is opened for writing), the active part A1-A12 on FakeKernel + SimClock with the inline driver and a mocked systemctl: bundle,
trace.jsonl and the 16-payload-byte rule, pixels only in <name>-images, Block C on errors and on SIGHUP/SIGTERM, the service
stop/start with its marker, the uid-derived lock and XDG_RUNTIME_DIR, the shared Store (dirty marker, clean teardown, settle),
REG_READ placement (A2 after STREAM_ON, A12 after the OFF writes), the confirmation rules and the profile-write rule of §5.2.
systemctl, journalctl and /status are always injected; nothing here touches a real device, unit or port."""
import ffutil  # first: installs the port guard

import builtins
from collections import Counter
import io
import json
import os
import shutil
import signal
import stat
import tempfile
import threading
import unittest
from unittest import mock

ffutil.setup_path()
from frameface import probe, probe_active  # noqa: E402
from frameface.config import default_paths  # noqa: E402
from frameface.descriptors import build_expected, sha1_hex  # noqa: E402
from frameface.jpeg import JpegError, StubJpegEncoder  # noqa: E402
from frameface.preprocess import StubPreprocessor  # noqa: E402
from frameface.sim import VftModel, check_trace, make_rig  # noqa: E402
from frameface.state import Runtime, Store  # noqa: E402
from frameface.sysfs import IFACE_SEP  # noqa: E402

UID, BOOT, POSIX = 4242, "sim-boot", os.name == "posix"
ACTIVE = ("--active", "--yes", "--seconds", "3", "--bundle-name", "probe-t")
BUS = "Failed to connect to bus: No medium found"


def hold_lock(fd):
    """Hold the lock file like a running service (fcntl.flock; msvcrt on Windows, as the probe does there)."""
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())


class Hangup(io.StringIO):
    """stdout of an `ssh -t` session that dropped: every write fails once `broken` is set."""
    broken = False

    def write(self, s):
        if self.broken:
            raise OSError(5, "Input/output error")
        return super().write(s)


class ProbeCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ffprobe-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home, self.run_root = os.path.join(self.root, "home"), os.path.join(self.root, "run")
        os.makedirs(os.path.join(self.run_root, str(UID)))
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        env.update(HOME=self.home, USERPROFILE=self.home)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.paths = default_paths(self.home, uid=UID, run_root=self.run_root)
        self.calls, self.sysctl, self.journal, self.out = [], {}, (1, "", "no journal here"), io.StringIO()

    def rig(self, **knobs):
        rig = make_rig(VftModel(**knobs))
        self.addCleanup(rig.close)
        return rig

    def runner(self, argv, env):
        """The mocked systemctl/journalctl/ip: records (argv, XDG_RUNTIME_DIR, marker present) of every call."""
        self.calls.append((list(argv), env.get("XDG_RUNTIME_DIR"), os.path.exists(os.path.join(self.paths.state_dir,
                                                                                               probe.MARKER))))
        if argv[0] == "systemctl":
            return self.sysctl.get(argv[2], (0, {"is-enabled": "enabled\n", "is-active": "active\n"}.get(argv[2], ""), ""))
        return self.journal if argv[0] == "journalctl" else (1, "", "%s: not here" % argv[0])

    def run_probe(self, rig, *argv, **env):
        e = dict(home=self.home, uid=UID, run_root=self.run_root, sys_root=rig.sys_root, dev_root=rig.dev_root,
                 proc_root=rig.proc_root, etc_root=os.path.join(self.root, "etc"), usr_root=os.path.join(self.root, "usr"),
                 shm=os.path.join(self.root, "shm"), lib_dir=os.path.join(self.root, "lib"), usb=rig.kernel, clock=rig.clock,
                 run=self.runner, status=lambda: {"display": "streaming", "message": "Streaming 60 fps to 1 client"},
                 jpeg=StubJpegEncoder, pre=StubPreprocessor(), stdin=io.StringIO(""), stdout=self.out, tty=False,
                 threaded=False, boot_id=BOOT, note=rig.kernel.note)
        e.update(env)
        return probe.main(list(argv), e)

    def bundle(self, name="probe-t"):
        return os.path.join(self.paths.state_dir, name)

    def report(self, name="probe-t"):
        with open(os.path.join(self.bundle(name), "report.json"), encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def checks(rep):
        return {c["id"]: c for c in rep["checks"]}

    def verbs(self):
        return [c[0][2] for c in self.calls if c[0][0] == "systemctl"]

    @staticmethod
    def ctrl(rig, name):
        return [i for i, op in enumerate(rig.kernel.ops) if op["kind"] == "ctrl" and op["name"] == name]

    @staticmethod
    def opened(rig):
        return [op for op in rig.kernel.ops if op["kind"] == "open"]

    def clean(self, rig):
        """No violation, the 13 trace invariants (probe mode: REG_READ allowed), nothing in flight: Block C ran."""
        self.assertEqual(rig.kernel.violations, [])
        self.assertEqual(check_trace(rig.kernel.ops, service=False), [])
        self.assertEqual(rig.kernel.inflight(), 0)


class ReadOnly(ProbeCase):
    def test_never_opens_a_node_and_writes_only_the_bundle(self):
        rig, seen = self.rig(), []
        real_os_open, real_open = os.open, builtins.open

        def spy_os_open(path, flags, *a, **kw):
            seen.append((os.fspath(path), bool(flags & (os.O_WRONLY | os.O_RDWR))))
            return real_os_open(path, flags, *a, **kw)

        def spy_open(file, mode="r", *a, **kw):
            if not isinstance(file, int):
                seen.append((os.fspath(file), any(c in mode for c in "wax+")))
            return real_open(file, mode, *a, **kw)

        with mock.patch("os.open", spy_os_open), mock.patch("builtins.open", spy_open):
            code = self.run_probe(rig, "--bundle-name", "probe-t")
        norm = lambda p: os.path.normcase(os.path.abspath(p))  # noqa: E731
        writes, dev = [norm(p) for p, w in seen if w], norm(rig.dev_root)
        self.assertEqual(code, 0, self.out.getvalue())
        self.assertTrue(writes and all(p.startswith(norm(self.bundle()) + os.sep) for p in writes), writes)
        self.assertEqual([p for p, _ in seen if norm(p).startswith(dev)], [])           # the usbfs node: not even read
        self.assertEqual(self.opened(rig), [])
        self.assertFalse(os.path.exists(self.paths.lock))
        self.assertEqual(self.verbs(), ["is-enabled", "is-active"])

    def test_every_check_reports_with_a_hint(self):
        rig = self.rig(sysname="1-1.2", other_usbfs_owner=(812, "vftstream"))
        write(os.path.join(rig.sys_root, "bus", "usb", "devices", "1-1.2", "descriptors"), build_expected("ss"))
        write(os.path.join(rig.sys_root, "class", "typec", "port0", "data_role"), "host [device]")
        write(os.path.join(self.root, "etc", "os-release"), 'VERSION_ID=0.4.5\nBUILD_ID="20261022.6150929"\n')
        write(os.path.join(self.root, "usr", "lib", "udev", "rules.d", "00-usb-permissions.rules"),
              '# Valve\nSUBSYSTEM=="usb", MODE="0660", GROUP="input"\n')
        write(os.path.join(self.root, "shm"), bytes([5, 0, 0, 0, 1, 0, 0, 0]) + bytes(0x38))
        Store(self.paths, rig.clock, boot_id=BOOT).set_fault({"kind": "port", "port": 9023, "errno": "EADDRINUSE"})
        self.sysctl = {"is-enabled": (1, "", BUS), "is-active": (1, "", BUS)}
        self.journal = (0, "kernel: usb 1-1.2: device descriptor read/64, error -71\n", "")

        def no_jpeg():
            raise JpegError("no libturbojpeg")

        ro = dict(exists=True, mode="0444", mode_str="cr--r--r--", uid=0, gid=0, group="input", writable=False)
        with mock.patch.object(probe, "node_access", return_value=ro):
            code = self.run_probe(rig, "--bundle-name", "probe-t", jpeg=no_jpeg)
        rep, text = self.report(), self.out.getvalue()
        c = self.checks(rep)
        self.assertEqual((code, rep["exit"]), (1, 1))
        self.assertEqual(sorted(c, key=lambda k: int(k[1:])), ["P%d" % i for i in range(1, 14)])
        want = {"P1": "PASS", "P2": "FAIL", "P3": "PASS", "P4": "WARN", "P5": "PASS", "P6": "WARN", "P7": "FAIL", "P8": "FAIL",
                "P9": "WARN", "P10": "PASS", "P11": "WARN", "P12": "WARN", "P13": "WARN"}
        self.assertEqual({k: v["status"] for k, v in c.items()}, want)
        for cid, chk in c.items():
            self.assertTrue(chk["hint"], cid)
            if chk["status"] != "PASS":
                self.assertIn("] %s %s: " % (cid, chk["name"]), text)
                self.assertIn("(hint: %s)" % chk["hint"], text)
        self.assertIn("vftstream pid 812", c["P8"]["hint"])
        self.assertEqual((c["P2"]["data"]["jpeg"], c["P2"]["hint"]), ("missing", "Update SteamOS, then run install.sh again"))
        self.assertIn(BUS, c["P12"]["detail"])
        self.assertEqual((c["P1"]["data"]["version_id"], c["P1"]["data"]["build_id"]), ("0.4.5", "20261022.6150929"))
        self.assertEqual((c["P10"]["data"]["abi"], c["P10"]["data"]["seq_moving"]), (5, False))
        self.assertEqual(c["P3"]["data"]["valve_rules"], ['SUBSYSTEM=="usb", MODE="0660", GROUP="input"'])
        self.assertGreaterEqual(text.count("] P9 descriptors: "), 2)                    # one WARN line per difference

    def test_p6_power_state_quick_subset_and_json(self):
        rig = self.rig()
        for name, value in (("control", "auto"), ("runtime_status", "suspended"), ("runtime_suspended_time", "1234")):
            write(os.path.join(rig.sys_root, "bus", "usb", "devices", "1-1", "power", name), value)
        self.assertEqual(self.run_probe(rig, "--quick", "--json"), 0)
        rep = json.loads(self.out.getvalue())
        d = self.checks(rep)["P6"]["data"]
        self.assertEqual([c["id"] for c in rep["checks"]], list(probe.QUICK))
        self.assertEqual((d["control"], d["autosuspend_delay_ms"], d["runtime_status"], d["runtime_suspended_time"]),
                         ("auto", 2000, "suspended", 1234))

    def test_p12_systemctl_errors_are_warnings(self):
        rig = self.rig()
        for rc, err in ((1, BUS), (1, "Failed to get unit file state for frameface.service: No such file or directory"),
                        (None, "[Errno 2] No such file or directory: 'systemctl'")):
            with self.subTest(err=err):
                self.sysctl, self.out = {"is-enabled": (rc, "", err), "is-active": (rc, "", err)}, io.StringIO()
                self.assertEqual(self.run_probe(rig, "--quick"), 0)                     # a WARN never changes the exit
                self.assertIn("[WARN] P12 service: systemctl --user failed: %s" % err, self.out.getvalue())
                self.assertIn("restart it on the headset with: python3 -I ~/frameface/frameface_main.py restart)", self.out.getvalue())
        self.assertEqual([h for h in probe.HINTS.values() if "systemctl" in h], [])     # over SSH it needs XDG_RUNTIME_DIR

    def test_p9_diffs_against_the_hs_persona(self):
        self.assertEqual(self.run_probe(self.rig(persona="ss"), "--bundle-name", "probe-t"), 0)   # a 5000 Mbit/s tracker
        p9 = self.checks(self.report())["P9"]
        self.assertEqual((p9["status"], p9["data"]["diff"]), ("WARN", [
            "bcd_usb 0x0300 != 0x0210", "max_packet0 9 != 64", "max_packet 1024 != 512"]))

    def test_bundle_names_must_match_the_uninstall_glob(self):
        rig = self.rig()                                     # install.sh --uninstall deletes probe-*-images (pixels)
        for name in ("mytest", probe.MARKER, "probe-", "probe-x\n", "probe-../x"):
            with self.subTest(name=name):
                self.assertEqual(self.run_probe(rig, "--bundle-name", name), 2)
                self.assertEqual(self.run_probe(rig, "--active", "--yes", "--dump", "1", "--bundle-name", name), 2)
        self.assertEqual((self.opened(rig), os.path.exists(self.paths.state_dir)), ([], False))

    def test_bundle_files(self):
        rig = self.rig()
        write(self.paths.events, '{"t":"x","ev":"start"}\n')
        self.journal = (0, "Nov 06 frameface: I usb: state from=IDLE to=SETTLING\n", "")
        self.assertEqual(self.run_probe(rig, "--bundle-name", "probe-t"), 0)
        b = self.bundle()
        self.assertEqual(sorted(os.listdir(b)), ["descriptors.bin", "events.jsonl", "journal.txt", "report.json", "summary.txt"])
        with open(os.path.join(b, "descriptors.bin"), "rb") as f:
            self.assertEqual(f.read(), build_expected("hs"))
        with open(os.path.join(b, "summary.txt"), encoding="utf-8") as f:
            self.assertIn("[PASS] P9 descriptors: 228 bytes as expected", f.read())
        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(b).st_mode), 0o700)
            self.assertEqual({stat.S_IMODE(os.stat(os.path.join(b, n)).st_mode) for n in os.listdir(b)}, {0o600})


class Active(ProbeCase):
    def test_a1_to_a4_a8_a12_trace_report_and_images(self):
        rig = self.rig()
        self.assertEqual(self.run_probe(rig, *ACTIVE, "--dump", "2"), 0, self.out.getvalue())
        rep, b = self.report(), self.bundle()
        c = self.checks(rep)
        self.assertLessEqual({"v", "version", "argv", "mode", "exit", "checks", "summary", "teardown_ir", "profile", "service",
                              "aborted", "settle_waits", "bundle", "images"}, set(rep))
        self.assertLessEqual({"A1", "A2", "A3", "A4", "A8", "A12"}, set(c))
        self.assertEqual({c[k]["status"] for k in ("A1", "A2", "A3", "A4", "A8")}, {"PASS"})
        a1, a2, a3, a4 = (c[k]["data"] for k in ("A1", "A2", "A3", "A4"))
        self.assertEqual((a1["get_len"], a1["xu_len"], a1["sel1_get_len"], len(a1["first_get_cur"])), (64, 64, "4000", 128))
        self.assertEqual((a2["strategy"], len(a2["cmds"]), sorted(a2["regs"])), ("S0", 16, ["%02x" % r for r in (
            0, 2, 3, 4, 5, 6, 7, 8, 14, 15, 112)]))
        self.assertEqual((a2["regs"]["02"], a2["regs"]["05"]), (0xFF, 0xB2))                  # latched after MAGIC #3
        self.assertEqual((a3["max_frame"], a3["max_payload"], sorted(a3["before"])), (320000, 32768, [
            "GET_CUR", "GET_DEF", "GET_MAX", "GET_MIN"]))
        self.assertEqual([x.split()[0] for x in a3["after"]], ["PROBE_GET_CUR", "PROBE_SET", "PROBE_GET_CUR", "PROBE_GET_MAX",
                                                               "PROBE_GET_MIN", "COMMIT_SET"])     # Block B's own, not the A3 GETs
        sizes = {str(k): v for k, v in a4["payload_sizes"].items()}                    # 9 x 32768 + 25208 per frame
        self.assertEqual((len(a4["wire"]), set(sizes), a4["header_flags"].keys() >= {"0c", "0d", "0e", "0f"}),
                         (256, {"32768", "25208"}, True))
        self.assertLessEqual(abs(sizes["32768"] - 9 * sizes["25208"]), 9)
        self.assertTrue(all({"st", "len", "hdr", "dt_us"} <= set(x) for x in a4["wire"]))   # status, length, 16 bytes, dt
        dts = [x["dt_us"] for x in a4["wire"]]
        self.assertTrue(dts[0] == 0 and min(dts) >= 0 and sum(dts) > 0, dts[:10])
        self.assertEqual(a4["eof_payloads"], sizes["25208"])
        self.assertGreater(a4["fid_toggles"], 10)
        self.assertGreater(a4["health"]["ok"], 100)
        with open(os.path.join(b, "trace.jsonl"), encoding="utf-8") as f:
            ops = Counter(json.loads(line)["op"] for line in f)
        self.assertTrue(ops["open"] and ops["ctrl"] and ops["submit"] and ops["reap"] and ops["close"])
        img = b + "-images"
        names = sorted(os.listdir(img))
        self.assertLessEqual({"raw_0.bin", "raw_1.bin", "odd_0.pgm", "even_0.pgm", "left_0.pgm", "right_0.pgm", "pre_0.pgm",
                              "pre_0.jpg", "manifest.json"}, set(names))
        self.assertEqual(os.path.getsize(os.path.join(img, "raw_0.bin")), 320000)
        self.assertFalse([n for n in os.listdir(b) if n.endswith((".bin", ".pgm", ".jpg")) and n != "descriptors.bin"])
        if POSIX:
            self.assertEqual({stat.S_IMODE(os.stat(os.path.join(img, n)).st_mode) for n in names}, {0o600})
            self.assertEqual(stat.S_IMODE(os.stat(img).st_mode), 0o700)
        self.clean(rig)

    def test_a5_matrix_a6_ir_sweep_a7_soak(self):
        rig = self.rig()
        write(os.path.join(rig.sys_root, "bus", "iio", "devices", "iio%sdevice0" % IFACE_SEP, "in_current_pm8550b_iin_fb_input"),
              "300")
        write(os.path.join(rig.sys_root, "class", "thermal", "thermal_zone0", "temp"), "41500")
        self.assertEqual(self.run_probe(rig, *ACTIVE, "--matrix", "--ir-sweep", "--soak", "20"), 0, self.out.getvalue())
        c = self.checks(self.report())
        a5, a6, a7 = c["A5"]["data"]["rungs"], c["A6"]["data"]["levels"], c["A7"]["data"]
        self.assertEqual([(r["strategy"], r["xu_len"], r["error"]) for r in a5], [(s, 384 if s == "S5" else 64, None) for s in (
            "S0", "S1", "S2", "S3", "S4", "S5")])
        self.assertTrue(all(r["ok_run_s"] >= 2 for r in a5[:5]) and c["A5"]["status"] == "PASS")
        self.assertEqual([(r["level"], r["current"], r["pwm"]) for r in a6], [
            ("L0", 0xB2, 0xFF), ("L1", 0x96, 0xFF), ("L2", 0x78, 0xFF), ("L3", 0x5A, 0xFF), ("L0 pwm80", 0xB2, 0x80)])
        light = [r["brightness"] for r in a6]
        self.assertTrue(light[0] > light[1] > light[2] > light[3] and light[4] < light[0], light)  # the IR follows the sweep
        self.assertTrue(all(r["idle"]["iin_ma"] == 300 and r["during"]["temp_c"] == 41.5 and not r["brownout_suspect"]
                            for r in a6))
        self.assertEqual((len(a7["samples"]), [s["temp_c"] for s in a7["samples"]], len(a7["idle"])), (2, [41.5, 41.5], 2))
        self.assertTrue(all(55 < s["fps_in"] < 65 for s in a7["samples"]))
        self.assertEqual(a7["idle"][1]["runtime_status"], "active")
        self.clean(rig)

    def test_the_configured_ir_and_strategy_are_used(self):
        rig = self.rig()
        write(self.paths.config, json.dumps({"ir_pwm": 0x80, "ir_level": "L2", "strategy": "S2"}))
        self.run_probe(rig, *ACTIVE)
        ops, rep = rig.kernel.ops, self.report()
        on = [(ops[i]["reg"], ops[i]["val"]) for i in self.ctrl(rig, "REG_WRITE")][:11]                # Block A of A2
        self.assertEqual([v for r, v in on if r in (2, 3, 4, 5, 6, 7)], [0x80] * 3 + [0x78] * 3)
        self.assertEqual((self.checks(rep)["A2"]["data"]["strategy"], rep["checks"][-1]["id"]), ("S2", "A12"))
        self.clean(rig)

    def test_after_a_lost_the_next_activation_waits_for_the_new_enumeration(self):
        rig = self.rig(brownout_current_above=0x80)                                     # L0 browns out 1 s into the stream
        self.run_probe(rig, *ACTIVE, "--xu-len", "384")                                 # A4 is LOST, then A11
        ops, c = rig.kernel.ops, self.checks(self.report())
        replug = next(op for op in ops if op["kind"] == "replug")
        opens = [i for i, op in enumerate(ops) if op["kind"] == "open"]
        self.assertEqual([ops[i].get("err") for i in opens], [None, None])               # the new node, not the old one
        self.assertTrue(ops[opens[1]]["path"].endswith("%03d" % replug["devnum"]), ops[opens[1]]["path"])
        magic = next(op for op in ops[opens[1]:] if op["kind"] == "ctrl" and op["name"] == "MAGIC")
        self.assertGreaterEqual(magic["t"], replug["t"] + 2.0)                          # enum_settle_s after it came back
        self.assertTrue("lost" in c["A4"]["detail"] and c["A11"]["data"]["usb"])
        self.assertEqual(rig.kernel.violations, [])

    def test_block_c_runs_when_a_step_raises(self):
        rig = self.rig()
        with mock.patch.object(probe_active, "analyze_layout", side_effect=RuntimeError("boom")):   # A4's _stream
            code = self.run_probe(rig, *ACTIVE, "--stop-service")
        c = self.checks(self.report())
        self.assertEqual((code, c["probe"]["status"]), (1, "FAIL"))
        self.assertIn("internal error during A4", c["probe"]["detail"])
        self.clean(rig)                                                                 # incl. the teardown XU part
        self.assertFalse(Store(self.paths, rig.clock, boot_id=BOOT).load_runtime().dirty)
        self.assertEqual(self.verbs()[-1], "start")

    def test_service_stop_start_and_the_marker(self):
        rig, marker = self.rig(), os.path.join(self.paths.state_dir, probe.MARKER)
        self.assertEqual(self.run_probe(rig, *ACTIVE, "--stop-service"), 0)
        self.assertEqual(self.verbs(), ["is-enabled", "is-active", "stop", "start"])              # P12, then §5.2
        by_verb = {c[0][2]: c for c in self.calls if c[0][0] == "systemctl"}
        self.assertTrue(by_verb["stop"][2] and by_verb["start"][2])                    # written before the stop
        self.assertFalse(os.path.exists(marker))                                        # removed after a good start
        self.assertEqual(self.report()["service"], {"stopped": True, "restarted": True})
        self.calls, self.sysctl = [], {"start": (1, "", "Job for frameface.service failed")}
        self.assertEqual(self.run_probe(self.rig(), *ACTIVE[:-1], "probe-u", "--stop-service"), 0)
        self.assertTrue(os.path.exists(marker))                                         # kept: the PC starts the unit
        self.assertIn("[WARN] service service: systemctl --user start failed: Job for frameface.service failed (hint: start it by "
                      "hand: python3 -I ~/frameface/frameface_main.py start)", self.out.getvalue())      # the CLI sets XDG_RUNTIME_DIR

    def test_uid_lock_and_xdg_runtime_dir(self):
        rig, lock = self.rig(), os.path.join(self.run_root, str(UID), "frameface.lock")
        self.assertEqual(default_paths(self.home, uid=UID, run_root=self.run_root).lock, lock)    # the unit's own file
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            hold_lock(fd)
            code = self.run_probe(rig, *ACTIVE, "--stop-service")
        finally:
            os.close(fd)
        self.assertEqual(code, 2)                                                       # refused after 10 s (SimClock)
        self.assertEqual(self.opened(rig), [])
        self.assertEqual({x for _, x, _ in self.calls}, {"/run/user/%d" % UID})         # XDG_RUNTIME_DIR was unset
        self.assertEqual(self.verbs()[-2:], ["stop", "start"])                          # the service runs again
        os.environ["XDG_RUNTIME_DIR"] = elsewhere = os.path.join(self.root, "elsewhere")   # set, but not the lock's home
        os.makedirs(elsewhere)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            hold_lock(fd)                                                               # still the uid's file: refused
            self.assertEqual(self.run_probe(self.rig(), *ACTIVE[:-1], "probe-y"), 2)
        finally:
            os.close(fd)
        self.assertEqual(self.run_probe(self.rig(), *ACTIVE[:-1], "probe-x"), 0)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_bus_unreachable_with_a_run_process_refuses(self):
        rig = self.rig()
        write(os.path.join(rig.proc_root, "4321", "cmdline"), b"/usr/bin/python3\0-I\0/home/s/frameface/frameface_main.py\0run\0")
        self.sysctl = {"is-enabled": (1, "", BUS), "is-active": (1, "", BUS)}
        self.assertEqual(self.run_probe(rig, *ACTIVE), 2)
        self.assertEqual(self.opened(rig), [])

    def test_store_marks_and_settle(self):
        rig = self.rig()
        last = rig.clock.boottime() - 3.0
        Store(self.paths, rig.clock, boot_id=BOOT).save_runtime(Runtime(boot_id=BOOT, last_teardown_bt=last))
        real = {n: getattr(Store, n) for n in ("mark_dirty", "mark_clean_teardown")}

        def spy(name):
            return lambda store: rig.kernel.note(mark=name) or real[name](store)

        with mock.patch.object(Store, "mark_dirty", spy("mark_dirty")), \
                mock.patch.object(Store, "mark_clean_teardown", spy("mark_clean_teardown")):
            self.assertEqual(self.run_probe(rig, *ACTIVE, "--xu-len", "384"), 0)        # two sessions: A4 and A11
        ops = rig.kernel.ops
        opens = [i for i, op in enumerate(ops) if op["kind"] == "open"] + [len(ops)]
        self.assertEqual(len(opens), 3)
        first_magic = []
        for a, b in zip(opens, opens[1:]):
            seg = ops[a:b]
            m1 = next(i for i, op in enumerate(seg) if op["kind"] == "ctrl" and op["name"] == "MAGIC")
            close = next(i for i, op in enumerate(seg) if op["kind"] == "close")
            self.assertEqual([op["mark"] for op in seg[:m1] if op["kind"] == "note" and "mark" in op], ["mark_dirty"])
            self.assertEqual([op["mark"] for op in seg[close:] if op["kind"] == "note" and "mark" in op],
                             ["mark_clean_teardown"])
            first_magic.append(seg[m1]["t"])
        self.assertTrue(last + 8.0 <= first_magic[0] < last + 9.0)                      # the stopped service's teardown
        closes = [op["t"] for op in ops if op["kind"] == "close"]
        self.assertGreaterEqual(first_magic[1], closes[0] + 8.0)                        # and the probe's own
        rt = Store(self.paths, rig.clock, boot_id=BOOT).load_runtime()
        self.assertEqual((rt.dirty, rt.last_teardown_bt is not None and rt.last_teardown_bt > closes[0]), (False, True))
        self.clean(rig)

    def test_a2_reg_read_after_stream_on_a12_after_the_off_writes(self):
        for strategy in ("S0", "S3"):                                                   # S3: Block A inside Block B
            with self.subTest(strategy=strategy):
                rig = self.rig()
                self.assertEqual(self.run_probe(rig, *ACTIVE, "--strategy", strategy), 0)
                ops, reads = rig.kernel.ops, self.ctrl(rig, "REG_READ")
                writes, on = self.ctrl(rig, "REG_WRITE"), self.ctrl(rig, "STREAM_ON")
                discard = next(i for i, op in enumerate(ops) if op["kind"] == "discard")
                self.assertEqual(len(reads), 14)
                self.assertTrue(all(r > on[0] for r in reads[:11]))                     # A2: only after STREAM_ON
                self.assertTrue(all(writes[-1] < r < discard for r in reads[11:]))      # A12: after the 11 OFF writes
                self.assertEqual([ops[r]["out"][16:18] for r in reads[11:]], ["02", "03", "04"])
                self.assertEqual([ops[w]["val"] for w in writes[-11:] if ops[w]["reg"] in (2, 3, 4)], [0, 0, 0])
                rep = self.report()
                a12 = self.checks(rep)["A12"]
                self.assertEqual((a12["status"], rep["teardown_ir"]["pwm_readback"], rep["teardown_ir"]["magic3"]),
                                 ("WARN", [255, 255, 255], False))
                self.assertIn('"teardown_magic3": true', a12["hint"])
                self.assertLessEqual({"current_before", "current_after"}, set(rep["teardown_ir"]))
                with open(os.path.join(self.bundle(), "trace.jsonl"), encoding="utf-8") as f:   # A4: the stream's first 256
                    first = [(r["t"], r["st"], r["len"], r["hdr"]) for r in map(json.loads, f) if r["op"] == "reap"][:256]
                wire = self.checks(rep)["A4"]["data"]["wire"]                          # completions (S3: incl. those
                self.assertEqual([(w["t"], w["st"], w["len"], w["hdr"]) for w in wire], first)  # reaped during A2)
                self.clean(rig)

    def test_teardown_magic3_adds_the_magic_and_a12_passes_on_a_zero_readback(self):
        for iio in (False, True):                                                       # the currents are information only
            with self.subTest(iio=iio):
                rig = self.rig()
                if iio:                                                                 # static fake current: no drop
                    write(os.path.join(rig.sys_root, "bus", "iio", "devices", "iio%sdevice0" % IFACE_SEP,
                                       "in_current_pm8550b_iin_fb_input"), "350")
                self.assertEqual(self.run_probe(rig, *ACTIVE, "--teardown-magic3"), 0)
                ops, magics, writes = rig.kernel.ops, self.ctrl(rig, "MAGIC"), self.ctrl(rig, "REG_WRITE")
                self.assertTrue(writes[-1] < magics[-1] < self.ctrl(rig, "REG_READ")[-3])    # step 7b before the readback
                rep = self.report()
                self.assertEqual((rep["teardown_ir"]["pwm_readback"], rep["teardown_ir"]["magic3"]), ([0, 0, 0], True))
                self.assertEqual(self.checks(rep)["A12"]["status"], "PASS")
                self.assertEqual(rep["teardown_ir"]["current_after"]["iin_ma"], 350 if iio else None)
                self.clean(rig)

    def test_a12_warns_unless_every_pwm_register_reads_back_0(self):
        def power(p):                                   # the USB input drops by 40 mA when the stream stops, IR on or not
            return dict(pct=71, charging=False, iin_ma=420 if p.ses and p.ses.drv.dev.fd is not None else 380, vbus_mv=5000,
                        battery_ua=None, temps={}, temp_c=None)
        q = probe_active.ActiveSteps._q                 # check-S: zero = a REG_READ that is acknowledged but always says 0
        zero = lambda drv, setup, reg=None, ms=1000: q(drv, setup, reg, ms) if reg is None else q(drv, setup, reg, ms) and 0  # noqa
        for knobs, extra, why, rb, read in (({"reg_read_supported": False}, (), "cannot verify: REG_READ did not answer", [None] * 3, q),
                                            ({}, ("--ir-sweep",), "IR still on", [0x80] * 3, q),     # A6 ends with "L0 pwm80"
                                            ({}, (), "cannot verify: REG_READ never read 02-04 as non-zero, so 0 proves nothing", [0] * 3, zero)):
            with self.subTest(why=why):                 # zero (write-only registers): A2 read 0 under PWM 0xff, so 0 proves nothing
                rig = self.rig(**knobs)
                with mock.patch.object(probe.Probe, "_power", power), mock.patch.object(probe_active.ActiveSteps, "_q", staticmethod(read)):
                    self.assertEqual(self.run_probe(rig, *ACTIVE, *extra), 0)
                a12, rep = self.checks(self.report())["A12"], self.report()
                self.assertEqual((a12["status"], rep["teardown_ir"]["pwm_readback"]), ("WARN", rb), a12["detail"])
                self.assertTrue(a12["detail"].startswith(why + ": PWM 02-04 read back "), a12["detail"])
                self.assertIn('"teardown_magic3": true', a12["hint"])

    def test_pixels_only_in_the_images_dir_and_16_bytes_per_record(self):
        rig = self.rig()
        self.assertEqual(self.run_probe(rig, *ACTIVE, "--pixels"), 0)
        b, img = self.bundle(), self.bundle() + "-images"
        self.assertEqual(os.listdir(img), ["trace-pixels.jsonl"])
        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(img, "trace-pixels.jsonl")).st_mode), 0o600)
        with open(os.path.join(img, "trace-pixels.jsonl"), encoding="utf-8") as f:
            reaps = [r for r in map(json.loads, f) if r["op"] == "reap" and r.get("len")]
        self.assertTrue(reaps and all(len(r["data"]) == 2 * r["len"] for r in reaps))
        windows = set()                                                                 # noisy 17-byte payload windows
        for r in reaps[:600]:
            data = bytes.fromhex(r["data"])
            windows.update(w for w in (data[o:o + 17] for o in range(16, len(data) - 17, 1009)) if len(set(w)) >= 6)
        self.assertGreater(len(windows), 100)
        for name in os.listdir(b):
            with open(os.path.join(b, name), "rb") as f:
                raw = f.read()
            text = raw.decode("latin-1").lower()
            self.assertFalse([w for w in windows if w.hex() in text or w in raw], name)
        with open(os.path.join(b, "trace.jsonl"), encoding="utf-8") as f:
            recs = [json.loads(line) for line in f]
        self.assertTrue(all("data" not in r and len(r.get("hdr", "")) <= 32 for r in recs))
        self.assertTrue(all(len(w["hdr"]) <= 32 for w in self.checks(self.report())["A4"]["data"]["wire"]))

    def test_signals_during_a4_run_block_c_the_restart_and_the_report(self):
        for name in ("SIGHUP", "SIGTERM"):
            with self.subTest(signal=name):
                self.calls, self.out = [], Hangup()
                rig, started, real = self.rig(), threading.Event(), probe.Probe._stream

                def stream(p, ses, seconds, measure=False, until=None):
                    measure and started.set()
                    return real(p, ses, seconds, measure, until)

                def fire():
                    if started.wait(120):
                        self.out.broken = name == "SIGHUP"                              # the ssh -t session is gone
                        if POSIX:                                                       # a real signal to this process
                            os.kill(os.getpid(), getattr(signal, name))
                        else:                                                           # the handler the probe registered
                            signal.getsignal(signal.SIGTERM)(1 if name == "SIGHUP" else signal.SIGTERM, None)

                sigs, noop = [s for s in (getattr(signal, "SIGHUP", None), signal.SIGTERM) if s is not None], lambda *a: None
                old = {s: signal.signal(s, noop) for s in sigs}                         # a stray signal never kills the run
                timer = threading.Thread(target=fire, daemon=True)
                try:
                    timer.start()
                    with mock.patch.object(probe.Probe, "_stream", stream):
                        code = self.run_probe(rig, "--active", "--yes", "--stop-service", "--seconds", "100000",
                                              "--bundle-name", "probe-" + name.lower())
                    restored = [signal.getsignal(s) for s in sigs]
                finally:
                    started.set()
                    timer.join(10)
                    for s, h in old.items():
                        signal.signal(s, h)
                rep = self.report("probe-" + name.lower())
                self.assertEqual((code, rep["aborted"], rep["aborted_step"]), (1, name, "A4"))
                self.assertEqual(self.verbs()[-1], "start")
                self.assertEqual(restored, [noop] * len(sigs))                         # the probe's handlers are gone
                self.clean(rig)

    def test_xu_len_384_and_s5_need_one_confirmation(self):
        for extra in (("--xu-len", "384"), ("--strategy", "S5")):
            with self.subTest(args=extra):
                rig, self.out = self.rig(), io.StringIO()
                code = self.run_probe(rig, "--active", "--seconds", "1", "--bundle-name", "probe-c", *extra,
                                      stdin=io.StringIO("y\n"))
                text = self.out.getvalue()
                self.assertNotEqual(code, 2, text)
                self.assertEqual((text.count("[y/N]"), text.count("Type yes")), (1, 0))
                self.assertTrue(self.opened(rig))

    def test_risky_steps_refuse_without_a_tty_and_need_two_yes(self):
        rig = self.rig()
        for extra in (("--xu-len", "128"), ("--risky-settle-test",), ("--risky-live-ir",)):
            with self.subTest(args=extra):
                self.assertEqual(self.run_probe(rig, *ACTIVE, *extra), 2)               # --yes does not replace a TTY
        self.assertEqual((self.opened(rig), self.verbs(), os.path.exists(self.bundle())), ([], [], False))
        self.out = io.StringIO()
        self.assertEqual(self.run_probe(rig, *ACTIVE, "--risky-live-ir", tty=True, stdin=io.StringIO("yes\nno\n")), 2)
        self.assertEqual((self.out.getvalue().count("Type yes to continue:"), self.opened(rig)), (2, []))
        self.assertEqual(self.run_probe(rig, *ACTIVE, "--risky-live-ir", "--risky-settle-test", tty=True,
                                        stdin=io.StringIO("yes\nyes\n")), 0)
        c = self.checks(self.report())
        self.assertEqual((c["A10"]["data"]["ack"], [t["delay_s"] for t in c["A9"]["data"]["tries"]]),
                         ({"05": "done", "06": "done", "07": "done"}, [1, 2, 4, 8]))   # the three IR current registers
        ops = rig.kernel.ops                                                            # A10 runs on the A4 session
        first_reap = next(i for i, op in enumerate(ops) if op["kind"] == "reap")
        off = next(i for i in self.ctrl(rig, "STREAM_OFF") if i > first_reap)          # Block C's (Block A has one too)
        live = [i for i in self.ctrl(rig, "REG_WRITE") if first_reap < i < off]
        self.assertEqual([(ops[i]["reg"], ops[i]["val"]) for i in live], [(5, 0xB2), (6, 0xB2), (7, 0xB2)])
        self.assertTrue(any(op["kind"] == "reap" for op in ops[live[-1]:off]))          # while streaming, and it went on
        tries = c["A9"]["data"]["tries"]                                             # the fake needs 8 s after a close
        self.assertEqual([("XuTimeout" in (t["error"] or ""), t["ok_run_s"] >= 2) for t in tries], [(True, False)] * 3 + [
            (False, True)])
        rig = self.rig()
        self.assertEqual(self.run_probe(rig, "--active", "--bundle-name", "probe-n", stdin=io.StringIO("n\n")), 2)
        self.assertEqual(self.opened(rig), [])                                          # declined y/N: nothing ran

    def test_profile_write_rule(self):
        sha = sha1_hex(build_expected("hs"))
        load = lambda: Store(self.paths, None, boot_id=BOOT).load_profile(sha)  # noqa: E731
        # A2-A4 streamed at the normal length: profile.json stays alone although A11 at 64 would stream too
        self.assertEqual(self.run_probe(self.rig(), *ACTIVE, "--xu-len", "64", tty=True, stdin=io.StringIO("yes\nyes\n")), 0)
        self.assertEqual((load(), self.report()["profile"]["action"]), (None, "unchanged"))
        # only 384-byte commands program the sensor: A4 (S0 at 64) is dark, the first A5 rung that streams is written
        self.assertEqual(self.run_probe(self.rig(programs_at_len=384), *ACTIVE[:-1], "probe-m", "--matrix"), 1)
        p = load()
        self.assertEqual((p.strategy, p.xu_len, p.source), ("S5", 384, "probe"))
        self.assertEqual(self.report("probe-m")["profile"], {"action": "written", "strategy": "S5", "xu_len": 384})
        self.assertEqual(self.checks(self.report("probe-m"))["A5"]["detail"], "healthy frames with S5")
        os.remove(self.paths.profile)
        # the same with A11: --xu-len 384 on the configured rung is the first later step that streams
        self.assertEqual(self.run_probe(self.rig(programs_at_len=384), *ACTIVE[:-1], "probe-x", "--xu-len", "384"), 1)
        p = load()
        self.assertEqual((p.strategy, p.xu_len, p.source), ("S0", 384, "probe"))


if __name__ == "__main__":
    unittest.main()
