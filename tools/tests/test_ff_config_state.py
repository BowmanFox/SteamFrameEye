# SPDX-License-Identifier: MIT
"""Configuration, paths, the state store and the purity of the core modules (spec §4.2, §4.19,
§3.3; §11.3 test_ff_config_state, task T1). Every file lives in a temporary directory."""
import ffutil  # first: installs the port guard

import ast
import dataclasses
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ffutil.setup_path()
from frameface import config as C  # noqa: E402
from frameface import state as S  # noqa: E402

TOKEN = "Abcdefghijklmnop_-12"
SPEC_DEFAULTS = {
    "enabled": True, "http_port": 9023, "allow_peers": ["10.35.78.0/24", "127.0.0.0/8"], "max_stream_clients": 2,
    "jpeg_quality": 92, "stream_max_kbps": 5000, "stream_fps_max": 60, "ip_tos": 0, "placeholder": True,
    "placeholder_ms": 1500, "keepalive_s": 2.0, "camera": "left", "y_phase": "auto", "preprocess": "auto",
    "strategy": "auto", "xu_len": "auto", "teardown_magic3": False, "n_urbs": 16, "urb_bytes": 0, "ir_level": "auto",
    "ir_current": None, "ir_pwm": 255, "settle_s": 8.0, "enum_settle_s": 2.0, "power_policy": "auto",
    "gate_on_worn": True, "worn_timeout_s": 3.0, "unworn_linger_s": 60.0, "no_client_linger_s": 30.0,
    "min_battery_pct": 5, "allow_port_reset": True, "cpu_io": "0-1", "cpu_work": "2-3", "nice_io": -5,
    "log_level": "info"}
# research brief §d.6 "ports to avoid" (plain values; no socket is involved anywhere in this file)
D6_PORTS = {27031, 27032, 27033, 27034, 27035, 27036, 27037, 10400, 10401, 9000, 9001, 9015, 9020, 9021, 8888, 8889,
            6767, 8555, 8097, 8085, 9099, 9337, 3240, 7575, 5555}
PURE = ("uvc", "descriptors", "assembler", "quality", "policy", "config", "state")
NOT_IN_PURE = ("ctypes", "select", "socket", "threading")


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ff config ")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, data, name="config.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        return path


class Defaults(TempDir):
    def test_defaults_equal_the_spec(self):
        cfg = C.Config()
        values = dataclasses.asdict(cfg)
        token = values.pop("token")
        self.assertEqual(values, SPEC_DEFAULTS)
        self.assertRegex(token, r"^[A-Za-z0-9_-]{22}$")
        self.assertNotEqual(C.Config().token, token, "each default config gets its own random token")
        self.assertEqual((cfg.xu_len, cfg.teardown_magic3), ("auto", False))

    def test_valid_values_load_without_warnings(self):
        valid = {"enabled": False, "http_port": 19023, "allow_peers": ["192.168.4.31/32", "10.35.78.0/24"],
                 "token": TOKEN, "max_stream_clients": 4, "jpeg_quality": 100, "stream_max_kbps": 20000,
                 "stream_fps_max": 1, "ip_tos": 32, "placeholder": False, "placeholder_ms": 200, "keepalive_s": 0.5,
                 "camera": "right", "y_phase": "even", "preprocess": "swar", "strategy": "S5", "xu_len": 384,
                 "teardown_magic3": True, "n_urbs": 32, "urb_bytes": 4096, "ir_level": "L3", "ir_current": 0,
                 "ir_pwm": 0, "settle_s": 30, "enum_settle_s": 0.5, "power_policy": "always", "gate_on_worn": False,
                 "worn_timeout_s": 30, "unworn_linger_s": 600, "no_client_linger_s": 5, "min_battery_pct": 0,
                 "allow_port_reset": False, "cpu_io": "auto", "cpu_work": "0-1,4", "nice_io": -8,
                 "log_level": "debug"}
        self.assertEqual(set(valid), set(SPEC_DEFAULTS) | {"token"})
        cfg, warnings = C.load(self.write(valid))
        self.assertEqual(warnings, [])
        self.assertEqual(dataclasses.asdict(cfg), valid)

    def test_strategies_and_xu_lengths(self):
        for value in ("auto", "S0", "S1", "S2", "S3", "S4", "S5"):
            cfg, warnings = C.load(self.write({"token": TOKEN, "strategy": value}))
            self.assertEqual((cfg.strategy, warnings), (value, []))
        for value, expected in (("auto", "auto"), (64, 64), (384, 384), ("64", 64), ("384", 384)):
            cfg, warnings = C.load(self.write({"token": TOKEN, "xu_len": value}))
            self.assertEqual((cfg.xu_len, warnings), (expected, []))
        cfg, warnings = C.load(self.write({"token": "", "allow_peers": ["10.35.78.1/24", "10.35.78.0/24"]}))
        self.assertEqual((cfg.token, cfg.allow_peers, warnings), ("", ["10.35.78.0/24"], []))   # normalised, no dups

    INVALID = [
        ("xu_len", 128, "auto"), ("xu_len", "128", "auto"), ("xu_len", True, "auto"), ("enabled", "yes", True),
        ("enabled", 1, True), ("http_port", 80, 9023), ("http_port", 70000, 9023), ("http_port", "9023", 9023),
        ("allow_peers", ["::1/128"], ["10.35.78.0/24", "127.0.0.0/8"]),
        ("allow_peers", "10.0.0.0/8", ["10.35.78.0/24", "127.0.0.0/8"]),
        ("allow_peers", ["10.0.0.0/8", "not a net"], ["10.35.78.0/24", "127.0.0.0/8"]),
        ("max_stream_clients", 0, 2), ("max_stream_clients", 5, 2), ("jpeg_quality", 101, 92),
        ("jpeg_quality", 85, 90), ("jpeg_quality", 92.5, 92), ("stream_max_kbps", 100, 5000),
        ("stream_fps_max", 0, 60), ("ip_tos", 16, 0), ("ip_tos", True, 0), ("placeholder", "true", True),
        ("placeholder_ms", 100, 1500), ("keepalive_s", 5.0, 2.0), ("keepalive_s", float("nan"), 2.0),
        ("camera", "center", "left"), ("y_phase", "both", "auto"), ("preprocess", "gpu", "auto"),
        ("strategy", "S6", "auto"), ("strategy", "s0", "auto"), ("teardown_magic3", "true", False),
        ("n_urbs", 64, 16), ("n_urbs", 3, 16), ("urb_bytes", 5000, 0), ("urb_bytes", 2048, 0),
        ("urb_bytes", 70000, 0), ("ir_level", "L4", "auto"), ("ir_current", 300, None), ("ir_current", "1", None),
        ("ir_pwm", -1, 255), ("settle_s", 2, 8.0), ("enum_settle_s", 0.1, 2.0), ("power_policy", "never", "auto"),
        ("gate_on_worn", None, True), ("worn_timeout_s", 31, 3.0), ("unworn_linger_s", 5, 60.0),
        ("no_client_linger_s", 1000, 30.0), ("min_battery_pct", 60, 5), ("allow_port_reset", 0, True),
        ("cpu_io", "x", "0-1"), ("cpu_work", "1-0", "2-3"), ("cpu_io", 1, "0-1"), ("nice_io", -10, -5),
        ("nice_io", 1, -5), ("log_level", "trace", "info"),
        ("jpeg_quality", -5, 90), ("settle_s", 10 ** 400, 8.0), ("keepalive_s", -(10 ** 400), 2.0),
        ("keepalive_s", 10 ** 400, 2.0), ("http_port", 10 ** 400, 9023)]                # never raises (§4.19)

    def test_each_invalid_value_falls_back_with_one_warning(self):
        for key, value, expected in self.INVALID:
            with self.subTest(key=key, value=value):
                cfg, warnings = C.load(self.write({"token": TOKEN, key: value}))
                self.assertEqual(getattr(cfg, key), expected)
                self.assertEqual(len(warnings), 1, warnings)
                self.assertIn(key, warnings[0])
                others, expected_others = dataclasses.asdict(cfg), dict(SPEC_DEFAULTS, token=TOKEN)
                others.pop(key)
                expected_others.pop(key)
                self.assertEqual(others, expected_others, "the other keys keep their defaults")

    def test_invalid_token_becomes_a_random_one_and_is_not_logged(self):
        for bad in ("short", "x" * 65, "has space in it!!", 12345678901234567890, None):
            with self.subTest(bad=bad):
                cfg, warnings = C.load(self.write({"token": bad}))
                self.assertRegex(cfg.token, r"^[A-Za-z0-9_-]{22}$")
                self.assertNotEqual(cfg.token, bad)
                self.assertEqual(len(warnings), 1)
                self.assertIn("token", warnings[0])
                self.assertNotIn(str(bad), warnings[0])
        cfg, warnings = C.load(self.write({"enabled": True}))                # no token at all: random + warning
        self.assertRegex(cfg.token, r"^[A-Za-z0-9_-]{22}$")
        self.assertEqual(len(warnings), 1)

    def test_unknown_key(self):
        cfg, warnings = C.load(self.write({"token": TOKEN, "colour": "blue", "http_port": 9030}))
        self.assertEqual((cfg.http_port, len(warnings)), (9030, 1))
        self.assertIn("unknown key 'colour'", warnings[0])

    def test_every_forbidden_port_is_rejected_as_http_port(self):
        self.assertEqual(C.FORBIDDEN_PORTS, frozenset(D6_PORTS))
        self.assertTrue({27031, 27037, 6767, 8555, 8097, 8085, 9099, 9337, 3240, 7575, 5555} <= C.FORBIDDEN_PORTS)
        for port in sorted(D6_PORTS):
            with self.subTest(port=port):
                cfg, warnings = C.load(self.write({"token": TOKEN, "http_port": port}))
                self.assertEqual(cfg.http_port, 9023)
                self.assertEqual(len(warnings), 1)
                self.assertIn("http_port", warnings[0])
        for port in (1024, 9022, 9023, 19023, 27030, 27038, 65535):
            cfg, warnings = C.load(self.write({"token": TOKEN, "http_port": port}))
            self.assertEqual((cfg.http_port, warnings), (port, []))

    def test_unreadable_files_never_raise(self):
        deep = "[" * 100000 + "]" * 100000                                  # RecursionError inside json
        for text in ("{bad json", "[1, 2]", "", "null", '"text"', deep, '{"camera": ' + deep + "}"):
            with self.subTest(text=text):
                cfg, warnings = C.load(self.write(text))
                self.assertEqual(len(warnings), 1)
                self.assertEqual(cfg.http_port, 9023)
        cfg, warnings = C.load(os.path.join(self.tmp, "missing.json"))
        self.assertEqual((cfg.enabled, len(warnings)), (True, 1))
        cfg, warnings = C.load(self.tmp)                                     # a directory
        self.assertEqual(len(warnings), 1)
        path = os.path.join(self.tmp, "bom.json")
        with open(path, "wb") as f:
            f.write(b"\xef\xbb\xbf" + json.dumps({"token": TOKEN, "camera": "right"}).encode())
        cfg, warnings = C.load(path)
        self.assertEqual((cfg.camera, warnings), ("right", []))


class WriteDefault(TempDir):
    def test_writes_every_key_and_the_token(self):
        path = os.path.join(self.tmp, "home", ".config", "frameface", "config.json")
        C.write_default(path, TOKEN)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data, dict(SPEC_DEFAULTS, token=TOKEN))
        cfg, warnings = C.load(path)
        self.assertEqual((cfg.token, warnings), (TOKEN, []))
        C.write_default(path)                                                 # a new random token
        token = C.load(path)[0].token
        self.assertRegex(token, r"^[A-Za-z0-9_-]{22}$")
        self.assertNotEqual(token, TOKEN)
        C.write_default(path, "")                                             # "" disables the path secret
        cfg, warnings = C.load(path)
        self.assertEqual((cfg.token, warnings), ("", []))
        with self.assertRaises(ValueError):
            C.write_default(path, "bad token")
        self.assertEqual(sorted(os.listdir(os.path.dirname(path))), ["config.json"])

    def test_modes(self):
        ffutil.require_native(reason="POSIX file modes")
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        folder = os.path.join(self.tmp, "home", ".config", "frameface")
        C.write_default(os.path.join(folder, "config.json"), TOKEN)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(folder, "config.json")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
        os.chmod(folder, 0o755)
        C.write_default(os.path.join(folder, "config.json"), TOKEN)
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)

    def test_token_format(self):
        tokens = {C.new_token() for _ in range(200)}
        self.assertEqual(len(tokens), 200)
        for t in tokens:
            self.assertTrue(C.TOKEN_RE.match(t) and len(t) == 22, t)
        for good in ("a" * 16, "A-_9" * 16):
            self.assertTrue(C.TOKEN_RE.match(good))
        for bad in ("a" * 15, "a" * 65, "a" * 15 + "/", "a" * 15 + "="):
            self.assertIsNone(C.TOKEN_RE.match(bad))


class Paths(TempDir):
    def test_lock_comes_from_the_uid_never_from_xdg(self):
        home, run_root = os.path.join(self.tmp, "home"), os.path.join(self.tmp, "run", "user")
        real = os.path.join(run_root, "4242")
        fallback = os.path.join(home, ".local", "state", "frameface", "frameface.lock")
        other = os.path.join(self.tmp, "x")
        os.makedirs(other)
        for exists in (False, True):
            if exists:
                os.makedirs(real)
            for xdg in (None, "/tmp/x", other, real):
                with self.subTest(exists=exists, xdg=xdg), mock.patch.dict(os.environ):
                    os.environ.pop("XDG_RUNTIME_DIR", None)
                    if xdg is not None:
                        os.environ["XDG_RUNTIME_DIR"] = xdg
                    p = C.default_paths(home=home, uid=4242, run_root=run_root)
                    self.assertEqual(p.lock, os.path.join(real, "frameface.lock") if exists else fallback)
        self.assertEqual(C.default_paths(home=home, uid=4243, run_root=run_root).lock, fallback)
        self.assertEqual(C.default_paths(home=home, runtime_dir=other, uid=4242, run_root=run_root).lock,
                         os.path.join(other, "frameface.lock"))

    def test_the_process_uid(self):
        run_root = os.path.join(self.tmp, "run")
        home = os.path.join(self.tmp, "home")
        state_lock = os.path.join(home, ".local", "state", "frameface", "frameface.lock")
        if not hasattr(os, "getuid"):                                        # Windows: no uid, the state dir
            os.makedirs(run_root)
            self.assertEqual(C.default_paths(home=home, run_root=run_root).lock, state_lock)
            return
        os.makedirs(os.path.join(run_root, str(os.getuid())))
        self.assertEqual(C.default_paths(home=home, run_root=run_root).lock,
                         os.path.join(run_root, str(os.getuid()), "frameface.lock"))

    def test_file_names(self):
        home = os.path.join(self.tmp, "home")
        p = C.default_paths(home=home, uid=1, run_root=os.path.join(self.tmp, "none"))
        cfg, st = os.path.join(home, ".config", "frameface"), os.path.join(home, ".local", "state", "frameface")
        self.assertEqual(dataclasses.asdict(p), {
            "config_dir": cfg, "config": os.path.join(cfg, "config.json"), "disabled_flag": os.path.join(cfg, "disabled"),
            "state_dir": st, "runtime": os.path.join(st, "runtime.json"), "profile": os.path.join(st, "profile.json"),
            "selftest": os.path.join(st, "selftest.json"), "events": os.path.join(st, "events.jsonl"),
            "lock": os.path.join(st, "frameface.lock")})
        self.assertEqual(C.default_paths(uid=1, run_root=os.path.join(self.tmp, "none")).config_dir,
                         os.path.join(os.path.expanduser("~"), ".config", "frameface"))

    def test_temp_paths_put_everything_under_root(self):
        p = C.temp_paths(self.tmp)
        for name, value in dataclasses.asdict(p).items():
            self.assertEqual(os.path.commonpath([self.tmp, value]), self.tmp, name)
        self.assertEqual(len(set(dataclasses.asdict(p).values())), 9)
        self.assertTrue(p.lock.endswith("frameface.lock") and p.runtime.endswith("runtime.json"))


class Clock:
    def __init__(self):
        self.bt = 500.0

    def boottime(self):
        return self.bt

    monotonic = boottime

    def sleep(self, s):
        self.bt += s


class Store(TempDir):
    def store(self, boot="boot-A"):
        self.clock = getattr(self, "clock", None) or Clock()
        return S.Store(C.temp_paths(self.tmp), self.clock, boot_id=boot)

    def files(self):
        return sorted(os.listdir(os.path.join(self.tmp, "state")))

    def test_defaults_without_files(self):
        s = self.store()
        self.assertEqual(s.load_runtime(), S.Runtime())
        self.assertIsNone(s.load_profile("abc"))
        self.assertEqual((s.warnings, s.dirty_start()), ([], False))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "state")), "loading writes nothing")

    def test_mark_fields(self):
        s = self.store()
        s.mark_dirty()
        r = s.load_runtime()
        self.assertEqual((r.v, r.boot_id, r.dirty, r.dirty_since_bt), (1, "boot-A", True, 500.0))
        self.assertTrue(s.dirty_start())
        self.assertFalse(self.store("boot-B").dirty_start(), "another boot is a fresh start")
        self.clock.bt = 510.0
        s.mark_clean_teardown()
        r = s.load_runtime()
        self.assertEqual((r.dirty, r.dirty_since_bt, r.last_teardown_bt), (False, None, 510.0))
        self.clock.bt = 520.0
        s.mark_dirty()
        s.mark_reset()
        r = s.load_runtime()
        self.assertEqual((r.dirty, r.last_reset_bt, r.last_teardown_bt), (True, 520.0, 510.0))
        self.clock.bt = 530.0
        s.mark_lost()
        r = s.load_runtime()
        self.assertEqual((r.dirty, r.dirty_since_bt, r.last_loss_bt, r.last_teardown_bt, r.last_reset_bt),
                         (False, None, 530.0, 510.0, 520.0))
        self.assertEqual(self.files(), ["runtime.json"])                    # no temporary files left
        self.assertEqual(s.warnings, [])

    def test_a_new_boot_starts_from_fresh_times_but_keeps_the_fault(self):
        s = self.store()
        s.mark_clean_teardown()
        s.set_fault({"kind": "port", "port": 9023})
        b = self.store("boot-B")
        self.assertFalse(b.same_boot(b.load_runtime()))
        b.mark_dirty()
        r = b.load_runtime()
        self.assertEqual((r.boot_id, r.last_teardown_bt, r.dirty, r.fault), ("boot-B", None, True,
                                                                              {"kind": "port", "port": 9023}))

    def test_fault_round_trip(self):
        s = self.store()
        fault = {"kind": "port", "port": 9023, "errno": "EADDRINUSE", "owner": "python3 pid 77",
                 "since": "2026-11-06T10:00:01Z"}
        s.set_fault(fault)
        self.assertEqual(self.store().load_runtime().fault, fault)
        s.mark_dirty()
        s.mark_clean_teardown()
        self.assertEqual(s.load_runtime().fault, fault)
        s.set_fault(None)
        self.assertIsNone(s.load_runtime().fault)
        r = S.Runtime(boot_id="boot-A", dirty=True, dirty_since_bt=1.5, last_teardown_bt=2, fault={"kind": "port"})
        s.save_runtime(r)
        self.assertEqual(s.load_runtime(), r)

    def test_profile_round_trip(self):
        s = self.store()
        p = S.Profile(desc_sha1="abc", speed=480, get_len=64, strategy="S5", xu_len=384, source="probe",
                      y_phase="odd", plane={"verdict": "identical", "mad": 0.0}, payload_stride=32768, fps_pts=59.9,
                      ir_level="L1", ir_reason="brownout")
        s.save_profile(p)
        self.assertRegex(p.updated, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        loaded = self.store().load_profile("abc")
        self.assertEqual(loaded, p)
        self.assertEqual((loaded.strategy, loaded.xu_len, loaded.source), ("S5", 384, "probe"))
        self.assertIsNone(s.load_profile("another descriptor"))
        s.save_profile(S.Profile(desc_sha1="abc", strategy="S0", xu_len=None, source="service"))
        self.assertEqual((s.load_profile("abc").xu_len, s.load_profile("abc").source), (None, "service"))
        self.assertEqual(self.files(), ["profile.json"])

    def test_corrupt_files_load_as_defaults_with_one_warning(self):
        s = self.store()
        s.mark_dirty()
        s.save_profile(S.Profile(desc_sha1="abc"))
        for name, text in (("runtime.json", "{not json"), ("profile.json", "\xff\xfe garbage")):
            with open(os.path.join(self.tmp, "state", name), "w", encoding="latin-1") as f:
                f.write(text)
        self.assertEqual(s.load_runtime(), S.Runtime())
        self.assertEqual(len(s.warnings), 1)
        self.assertIsNone(s.load_profile("abc"))
        self.assertEqual(len(s.warnings), 2)
        for text in ('{"v": 2, "dirty": true}', "[1]", "null"):
            with open(os.path.join(self.tmp, "state", "runtime.json"), "w") as f:
                f.write(text)
            s.warnings.clear()
            self.assertEqual((s.load_runtime(), len(s.warnings)), (S.Runtime(), 1))
        s.mark_dirty()                                                        # a corrupt file is replaced
        self.assertTrue(s.load_runtime().dirty)

    def test_invalid_fields_take_their_defaults(self):
        s = self.store()
        os.makedirs(os.path.join(self.tmp, "state"))
        data = dict(dataclasses.asdict(S.Profile(desc_sha1="abc", get_len=64, payload_stride=16384)),
                    ir_level="L9", strategy="S7", xu_len=4096, future_field=1)
        with open(os.path.join(self.tmp, "state", "profile.json"), "w") as f:
            json.dump(data, f)
        p = s.load_profile("abc")
        self.assertEqual((p.ir_level, p.strategy, p.xu_len, p.get_len, p.payload_stride), ("L0", None, None, 64, 16384))
        self.assertEqual(len(s.warnings), 1)
        self.assertTrue(all(name in s.warnings[0] for name in ("ir_level", "strategy", "xu_len")))
        with open(os.path.join(self.tmp, "state", "runtime.json"), "w") as f:
            json.dump({"v": 1, "boot_id": "boot-A", "dirty": "yes", "last_reset_bt": "x", "fault": [1]}, f)
        s.warnings.clear()
        self.assertEqual(s.load_runtime(), S.Runtime(boot_id="boot-A"))
        self.assertEqual(len(s.warnings), 1)

    def test_type_corrupt_files_never_raise(self):
        s = self.store()
        folder = os.path.join(self.tmp, "state")
        os.makedirs(folder)
        for field, value in (("strategy", []), ("strategy", {"S0": 1}), ("ir_level", []), ("ir_level", {"L0": 1}),
                             ("y_phase", ["odd"]), ("fps_pts", float("inf"))):
            with self.subTest(field=field, value=value):
                with open(os.path.join(folder, "profile.json"), "w") as f:
                    json.dump({"v": 1, "desc_sha1": "abc", field: value}, f)        # unhashable, Infinity
                s.warnings.clear()
                p = s.load_profile("abc")
                self.assertEqual(getattr(p, field), getattr(S.Profile(), field))
                self.assertEqual(len(s.warnings), 1)
                self.assertIn(field, s.warnings[0])
        for value in (float("inf"), float("-inf"), float("nan")):              # would never settle
            with open(os.path.join(folder, "runtime.json"), "w") as f:
                json.dump({"v": 1, "boot_id": "boot-A", "last_teardown_bt": value, "last_reset_bt": 5.0}, f)
            s.warnings.clear()
            r = s.load_runtime()
            self.assertEqual((r.last_teardown_bt, r.last_reset_bt, len(s.warnings)), (None, 5.0, 1))
        for name in ("runtime.json", "profile.json"):
            with open(os.path.join(folder, name), "w") as f:
                f.write("[" * 100000 + "]" * 100000)                          # RecursionError inside json
        s.warnings.clear()
        self.assertEqual((s.load_runtime(), s.load_profile("abc"), len(s.warnings)), (S.Runtime(), None, 2))

    def test_atomic_writes(self):
        s = self.store()
        s.mark_dirty()
        path = os.path.join(self.tmp, "state", "runtime.json")
        with open(path, "rb") as f:
            before = f.read()
        with mock.patch.object(C.os, "replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                s.mark_clean_teardown()
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before, "the old file is intact")
        self.assertEqual(self.files(), ["runtime.json"], "the temporary file was removed")
        with mock.patch.object(C.os, "fsync", wraps=os.fsync) as fsync:
            s.mark_clean_teardown()
        self.assertGreaterEqual(fsync.call_count, 1)
        self.assertFalse(s.load_runtime().dirty)
        self.assertEqual(json.loads(before.decode())["dirty"], True)

    def test_state_modes(self):
        ffutil.require_native(reason="POSIX file modes")
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        s = self.store()
        s.mark_dirty()
        s.save_profile(S.Profile(desc_sha1="abc"))
        folder = os.path.join(self.tmp, "state")
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
        for name in ("runtime.json", "profile.json"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(folder, name)).st_mode), 0o600)


IMPORT_CHECK = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
names = sys.argv[2:]
before = sorted(m for m in names if m in sys.modules)
import frameface.uvc, frameface.descriptors, frameface.assembler, frameface.quality
import frameface.policy, frameface.config, frameface.state
print(json.dumps({"before": before, "after": sorted(m for m in names if m in sys.modules),
                  "loaded": sorted(m for m in sys.modules if m.startswith("frameface"))}))
"""


class Purity(unittest.TestCase):
    def test_pure_modules_import_none_of_ctypes_select_socket_threading(self):
        run = subprocess.run([sys.executable, "-I", "-B", "-c", IMPORT_CHECK, str(ffutil.FRAMEFACE_DIR)]
                             + list(NOT_IN_PURE), capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual((result["before"], result["after"]), ([], []))
        self.assertEqual(set(result["loaded"]), {"frameface"} | {"frameface." + m for m in PURE + ("clock",)})

    def test_no_such_import_anywhere_in_the_source(self):
        for name in PURE:
            path = ffutil.FRAMEFACE_DIR / "frameface" / (name + ".py")
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            found = []
            for node in ast.walk(tree):                                      # also imports inside functions
                if isinstance(node, ast.Import):
                    found += [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    found.append(node.module.split(".")[0])
            self.assertEqual([m for m in found if m in NOT_IN_PURE], [], name)
            self.assertNotRegex(path.read_text(encoding="utf-8"), re.compile(r"__import__|importlib"), name)


if __name__ == "__main__":
    unittest.main()
