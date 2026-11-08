# SPDX-License-Identifier: MIT
"""install.sh, the unit, the udev rule and the FrameFace release files (spec §9.3-§9.5, §10.6,
§11.3 test_ff_install_sh, task T5).

ReleaseFiles (any platform): release-files.txt is the §9.4 list plus every .py of the package,
sorted, LF and complete; frameface.service == §9.4 and the udev rule == §9.5 byte for byte;
NOTICE.md has the MIT notices with the exact copyright lines of §10.6; LICENSE == the repo's.

InstallSh (Linux only, require_native("bash"): never on Windows, not even with Git Bash or WSL on
PATH): the real install.sh runs from an upload folder in a temporary HOME, with systemctl, sudo
and gcc replaced by recorders on PATH and the upload's probe.py replaced by a stub that records
its arguments. Every case forces the gate with FRAMEFACE_SELFTEST_FORCE, except test_real_gate
(real gcc, real selftest, real probe) and the selftest --quick of the first rollback.
"""
import ffutil  # first: installs the port guard

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

FF = ffutil.FRAMEFACE_DIR
SPEC_RELEASE = """70-vive-facial-tracker.rules LICENSE NOTICE.md README.md csrc/abi_dump.c csrc/ffproc.c
frameface.service frameface/__init__.py frameface/assembler.py frameface/cli.py frameface/clock.py
frameface/config.py frameface/descriptors.py frameface/jpeg.py frameface/logutil.py frameface/mailbox.py
frameface/policy.py frameface/preprocess.py frameface/probe.py frameface/quality.py frameface/selftest.py
frameface/server.py frameface/service.py frameface/sim/__init__.py frameface/sim/babaclient.py
frameface/sim/fakekernel.py frameface/sim/fakevft.py frameface/sim/replay.py frameface/sim/scenes.py
frameface/sim/simclock.py frameface/state.py frameface/sysfs.py frameface/usbfs.py frameface/uvc.py
frameface/vft.py frameface/worn.py frameface_main.py install-frameface.ps1 install.sh release-files.txt""".split()
UNIT = """# SPDX-License-Identifier: MIT
[Unit]
Description=FrameFace: VIVE Facial Tracker camera service for Baballonia (MJPEG on TCP 9023)
After=default.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/usr/bin/python3 -I %h/frameface/frameface_main.py run
Restart=always
RestartSec=3
TimeoutStopSec=8
KillMode=mixed
NoNewPrivileges=yes
UMask=0077
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1

[Install]
WantedBy=default.target
"""
RULES = ('# SPDX-License-Identifier: MIT\nSUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ATTR{idVendor}=="0bb4", '
         'ATTR{idProduct}=="0321", MODE="0660", GROUP="video", TAG+="uaccess"\n')
NOTICE_LINES = ("Copyright (c) 2026 OTT Labs", "Copyright (c) 2024 Plüss Roland", "Copyright (c) 2024 DragonDreams GmbH",
                "Copyright (c) 2026 Contributors to this repository", "Copyright DragonDreams GmbH 2024")
PERMISSION = ('Permission is hereby granted, free of charge, to any person obtaining a copy of this software and '
              'associated documentation files (the "Software"), to deal in the Software without restriction, including '
              'without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell '
              'copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the '
              'following conditions:')
TOKEN_RE = r"^[A-Za-z0-9_-]{16,64}$"
LF_SUFFIXES = (".sh", ".py", ".c", ".service", ".rules", ".txt", ".md")
GCC_CALLS = ["gcc -std=c99 -O2 -Wall -Wextra -fPIC -shared -o lib/libffproc.so csrc/ffproc.c",
             "gcc -std=c99 -O2 -Wall -Wextra -o lib/abi_dump csrc/abi_dump.c"]
UDEV_CMD = "sudo install -m 0644 ~/frameface/70-vive-facial-tracker.rules /etc/udev/rules.d/"

# systemctl recorder: one log line per call with XDG_RUNTIME_DIR and what exists at that moment
SYSTEMCTL = r'''#!/bin/bash
e() { [ -e "$1" ] && echo 1 || echo 0; }
printf 'systemctl %s | xdg=%s new=%s cur=%s state_selftest=%s\n' "$*" "${XDG_RUNTIME_DIR-unset}" \
  "$(e "$HOME/frameface.new")" "$(e "$HOME/frameface")" "$(e "$HOME/.local/state/frameface/selftest.json")" >> "$MOCK_LOG"
# Tests never use the production port 9023: the config that init-config wrote gets the test's free
# port (in place, so its mode stays) before anything can ask the "service" for its status.
conf="$HOME/.config/frameface/config.json"
if [ -f "$conf" ] && ! grep -Eq "\"http_port\": $MOCK_HTTP_PORT([^0-9]|\$)" "$conf"; then
  python3 -I -c 'import json, sys
with open(sys.argv[1], "r+", encoding="utf-8") as f:
    d = json.load(f); d["http_port"] = int(sys.argv[2]); f.seek(0); f.truncate(); json.dump(d, f, indent=2)' "$conf" "$MOCK_HTTP_PORT"
fi
case "$*" in
  "--user show-environment") [ -z "${MOCK_NO_BUS:-}" ] || exit 1; echo "HOME=$HOME" ;;
  "--user is-active --quiet frameface.service") [ -f "$MOCK_STATE/active" ] ;;
  "--user start frameface.service"|"--user restart frameface.service") touch "$MOCK_STATE/active" ;;
  "--user stop frameface.service") rm -f "$MOCK_STATE/active" ;;
  "--user disable --now frameface.service") rm -f "$MOCK_STATE/active"; [ -f "$HOME/.config/systemd/user/frameface.service" ] ;;
esac
'''
GCC = r'''#!/bin/bash
printf 'gcc %s\n' "$*" >> "$MOCK_LOG"
out=
while [ $# -gt 0 ]; do
  if [ "$1" = -o ] && [ $# -gt 1 ]; then out=$2; shift; fi
  shift
done
[ -z "$out" ] || printf 'mock gcc output\n' > "$out"
'''
SUDO = '#!/bin/bash\nprintf \'sudo %s\\n\' "$*" >> "$MOCK_LOG"\nexit 1\n'
STUB_PROBE = '''import os


def main(argv):
    with open(os.environ["MOCK_LOG"], "a") as f:
        f.write("probe %s\\n" % " ".join(argv))
    verdict = os.environ.get("MOCK_PROBE_P7", "PASS")
    print("[%s] P7 node access: stub probe of test_ff_install_sh" % verdict)
    return 1 if verdict == "FAIL" else 0
'''


def release_list():
    return (FF / "release-files.txt").read_text(encoding="ascii").splitlines()


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class ReleaseFiles(unittest.TestCase):
    def test_release_list_is_the_spec_list_plus_every_package_module(self):
        raw = (FF / "release-files.txt").read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertTrue(raw.endswith(b"\n") and not raw.endswith(b"\n\n"))
        names = raw.decode("ascii").split("\n")[:-1]
        self.assertEqual(names, sorted(set(names)))
        tree = {p.relative_to(FF).as_posix() for p in (FF / "frameface").rglob("*.py") if "__pycache__" not in p.parts}
        self.assertEqual(set(names), set(SPEC_RELEASE) | tree)
        for name in names:
            self.assertTrue((FF / name).is_file(), "listed but missing: " + name)

    def test_unit_and_udev_rule_equal_the_spec(self):
        self.assertEqual((FF / "frameface.service").read_bytes(), UNIT.encode("ascii"))
        self.assertNotRegex(UNIT, r"Nice=|WatchdogSec")
        self.assertEqual((FF / "70-vive-facial-tracker.rules").read_bytes(), RULES.encode("ascii"))

    def test_notice_license_and_install_sh_text(self):
        notice = (FF / "NOTICE.md").read_text(encoding="utf-8")
        lines = notice.splitlines()
        for line in NOTICE_LINES:
            self.assertIn(line, lines)
        self.assertEqual(notice.count(" ".join(PERMISSION.split()[:8])), 4)      # ours + three third-party notices
        self.assertGreaterEqual(" ".join(notice.split()).count(PERMISSION), 4)
        self.assertEqual(" ".join(notice.split()).count('THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND'), 4)
        for text in ("vivetracker.py", "THIRD_PARTY_NOTICES.md", "5bc0700", "faceModel.onnx"):
            self.assertIn(text, notice)
        repo = (ffutil.REPO / "LICENSE").read_bytes().replace(b"\r\n", b"\n")
        self.assertEqual((FF / "LICENSE").read_bytes().replace(b"\r\n", b"\n"), repo)
        script = (FF / "install.sh").read_bytes()
        self.assertNotIn(b"\r", script)
        self.assertTrue(script.startswith(b"#!/bin/bash\n# SPDX-License-Identifier: MIT\n"))


class InstallSh(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ffutil.require_native("bash", "python3", reason="install.sh runs on Linux only")

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ffinstall-"))
        self.addCleanup(shutil.rmtree, str(self.root), True)
        self.home, self.bin, self.mstate = self.root / "home", self.root / "bin", self.root / "mock-state"
        for d in (self.home, self.bin, self.mstate, self.root / "tmp"):
            d.mkdir()
        self.log = self.root / "calls.log"
        self.log.write_text("")
        for name, text in (("systemctl", SYSTEMCTL), ("gcc", GCC), ("sudo", SUDO)):
            self.mock(name, text)
        self.port = ffutil.free_port("tcp")
        self.env = {"HOME": str(self.home), "PATH": "%s:%s" % (self.bin, os.environ.get("PATH", "/usr/bin:/bin")),
                    "LANG": "C.UTF-8", "TMPDIR": str(self.root / "tmp"), "MOCK_LOG": str(self.log),
                    "MOCK_STATE": str(self.mstate), "MOCK_HTTP_PORT": str(self.port), "FRAMEFACE_SELFTEST_FORCE": "0"}
        self.upload = self.make_upload("frameface-upload-test")
        self.cur, self.prev = self.home / "frameface", self.home / "frameface.prev"
        self.new, self.state = self.home / "frameface.new", self.home / ".local/state/frameface"
        self.conf = self.home / ".config/frameface/config.json"
        self.unit = self.home / ".config/systemd/user/frameface.service"

    def mock(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def make_upload(self, name, version=None, unit_comment=None, real_probe=False):
        """The release files as install-frameface.ps1 uploads them (LF), the probe stubbed."""
        up = self.home / name
        for rel in release_list():
            data = (FF / rel).read_bytes()
            if rel.endswith(LF_SUFFIXES):
                data = data.replace(b"\r\n", b"\n")
            (up / rel).parent.mkdir(parents=True, exist_ok=True)
            (up / rel).write_bytes(data)
        if not real_probe:
            (up / "frameface/probe.py").write_text(STUB_PROBE)
        if version:
            init = up / "frameface/__init__.py"
            init.write_text(re.sub(r'__version__ = "[^"]*"', '__version__ = "%s"' % version, init.read_text()))
        if unit_comment:
            with open(up / "frameface.service", "a") as f:
                f.write("# %s\n" % unit_comment)
        return up

    def run_sh(self, *args, script=None, rc=0, **env):
        """Run install.sh; returns (output, calls of this run). sudo must never be called."""
        e = dict(self.env)
        e.update(env)
        e = {k: v for k, v in e.items() if v is not None}
        before = len(self.calls())
        p = subprocess.run(["bash", str(script or self.upload / "install.sh")] + list(args), cwd=str(self.home), env=e,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)
        out = p.stdout.decode("utf-8", "replace")
        calls = self.calls()[before:]
        self.assertEqual([c for c in calls if c.startswith("sudo")], [], out)
        if rc is not None:
            self.assertEqual(p.returncode, rc, out)
        return out, calls

    def calls(self):
        return self.log.read_text().splitlines()

    @staticmethod
    def systemctl(calls):
        return [c.split(" | ")[0][len("systemctl "):] for c in calls if c.startswith("systemctl ")]

    def files(self, folder):
        return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts and p.parts[len(folder.parts)] != "lib"
                      and p.name != "selftest.json")

    def snapshot(self, *folders):
        snap = {}
        for folder in folders:
            for p in [folder] + sorted(folder.rglob("*")) if folder.exists() else []:
                st = os.lstat(str(p))
                snap[str(p)] = (stat.S_IMODE(st.st_mode), p.read_bytes() if p.is_file() else None,
                                st.st_mtime_ns if p.is_file() else None)
        return snap

    def version(self, folder):
        return re.search(r'__version__ = "([^"]*)"', (folder / "frameface/__init__.py").read_text()).group(1)

    def token(self):
        return json.loads(self.conf.read_text())["token"]

    def test_bash_n_is_clean(self):
        p = subprocess.run(["bash", "-n", str(FF / "install.sh")], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))

    def test_fresh_install(self):
        out, calls = self.run_sh()
        self.assertEqual(self.files(self.cur), sorted(release_list()))
        for rel in release_list():
            self.assertEqual((self.cur / rel).read_bytes(), (self.upload / rel).read_bytes(), rel)
        self.assertEqual([c for c in calls if c.startswith("gcc ")], GCC_CALLS)
        self.assertEqual((self.cur / "lib/libffproc.so").read_text(), "mock gcc output\n")
        self.assertTrue((self.cur / "lib/abi_dump").is_file() and (self.cur / "lib/build.log").is_file())
        self.assertFalse(self.new.exists() or self.prev.exists())
        self.assertEqual((mode(self.conf), mode(self.conf.parent)), (0o600, 0o700))
        data = json.loads(self.conf.read_text())
        self.assertRegex(data["token"], TOKEN_RE)
        self.assertEqual((data["allow_peers"], data["jpeg_quality"]), (["10.35.78.0/24", "127.0.0.0/8"], 92))
        self.assertEqual((self.unit.read_bytes(), mode(self.unit)), (UNIT.encode(), 0o644))
        sc = self.systemctl(calls)
        self.assertEqual(sc[0], "--user show-environment")
        self.assertEqual([c for c in sc if "is-active" not in c][1:], ["--user daemon-reload",
                         "--user enable frameface.service", "--user restart frameface.service"])
        self.assertEqual({re.search(r"xdg=(\S+)", c).group(1) for c in calls if c.startswith("systemctl")},
                         {"/run/user/%d" % os.getuid()})
        # the gate result reaches the state dir only after the swap
        swap = {c.split(" | ")[0]: c.split(" | ")[1] for c in calls if c.startswith("systemctl")}
        self.assertEqual(swap["systemctl --user is-active --quiet frameface.service"], "xdg=/run/user/%d new=1 cur=0 "
                         "state_selftest=0" % os.getuid())
        self.assertIn("new=0 cur=1 state_selftest=1", swap["systemctl --user daemon-reload"])
        st = self.state / "selftest.json"
        self.assertEqual((st.read_bytes(), mode(st), mode(self.state)), ((self.cur / "selftest.json").read_bytes(), 0o600,
                                                                         0o700))
        self.assertEqual({k: json.loads(st.read_text())[k] for k in ("exit", "version", "forced")},
                         {"exit": 0, "version": "0.6.0", "forced": True})
        self.assertIn("http://10.35.78.1:%d/stream/%s\n" % (self.port, data["token"]), out)
        self.assertIn("did not answer within 5 s", out)
        self.assertIn("probe --quick", calls)
        self.assertNotIn(UDEV_CMD, out)

    def test_rerun_is_idempotent(self):
        def tree(snap):          # mode and bytes; the gate result differs only in its "when"
            return {k: v[:2] for k, v in snap.items() if "__pycache__" not in k and not k.endswith("selftest.json")}

        def result(folder):
            return {k: v for k, v in json.loads((folder / "selftest.json").read_text()).items() if k != "when"}

        self.run_sh()
        first, conf, unit, gate = self.snapshot(self.cur), self.conf.read_bytes(), self.unit.read_bytes(), result(self.cur)
        out, calls = self.run_sh()
        self.assertEqual(self.files(self.cur), sorted(release_list()))
        self.assertEqual(tree(self.snapshot(self.cur)), tree(first))
        self.assertEqual((result(self.cur), result(self.prev)), (gate, gate))
        self.assertEqual(self.files(self.prev), sorted(release_list()))
        self.assertEqual((self.conf.read_bytes(), self.unit.read_bytes()), (conf, unit))
        self.assertIn("config kept", out)
        self.assertEqual(self.systemctl(calls)[1:], ["--user is-active --quiet frameface.service",
                         "--user stop frameface.service", "--user daemon-reload", "--user enable frameface.service",
                         "--user restart frameface.service"])

    def test_upgrade_keeps_the_token(self):
        self.run_sh("--no-start")
        token, unit = self.token(), self.unit.read_bytes()
        up2 = self.make_upload("frameface-upload-v2", version="0.6.1", unit_comment="v2")
        out, calls = self.run_sh("--no-start", script=up2 / "install.sh")
        self.assertEqual(self.token(), token)
        self.assertEqual((self.version(self.cur), self.version(self.prev)), ("0.6.1", "0.6.0"))
        self.assertEqual(self.unit.read_bytes(), unit + b"# v2\n")
        self.assertEqual(json.loads((self.state / "selftest.json").read_text())["version"], "0.6.1")
        self.assertNotIn("--user restart frameface.service", self.systemctl(calls))
        # the hint works over SSH too: the CLI sets XDG_RUNTIME_DIR, a bare systemctl --user may not have it
        self.assertIn("\nnot started (--no-start); start it with: python3 -I ~/frameface/frameface_main.py start\n", out)
        self.assertNotIn("systemctl --user start", out)

    def test_gate_failure_keeps_the_old_install_and_its_selftest_result(self):
        self.run_sh("--no-start")
        (self.mstate / "active").touch()                                   # the old version runs
        before = self.snapshot(self.cur, self.state, self.conf.parent, self.unit.parent)
        up2 = self.make_upload("frameface-upload-v2", version="0.6.1")
        out, calls = self.run_sh(script=up2 / "install.sh", rc=1, FRAMEFACE_SELFTEST_FORCE="2")
        self.assertIn("selftest forced to exit 2", out)
        self.assertIn("nothing was installed", out)
        self.assertEqual(self.snapshot(self.cur, self.state, self.conf.parent, self.unit.parent), before)
        self.assertFalse(self.new.exists() or self.prev.exists())
        self.assertEqual(self.systemctl(calls), ["--user show-environment"])
        self.assertTrue((self.mstate / "active").exists())

    def test_gate_parity_failure_removes_the_helper_and_continues(self):
        out, _ = self.run_sh("--no-start", FRAMEFACE_SELFTEST_FORCE="3")
        self.assertIn("[WARN] the C pixel helper failed the parity check and was removed", out)
        self.assertFalse((self.cur / "lib/libffproc.so").exists())
        self.assertTrue((self.cur / "lib/abi_dump").is_file() and self.unit.is_file())
        self.assertEqual(json.loads((self.state / "selftest.json").read_text())["exit"], 3)

    def test_gate_without_turbojpeg_continues_with_a_fail_line(self):
        out, calls = self.run_sh("--no-start", FRAMEFACE_SELFTEST_FORCE="4")
        self.assertIn("[FAIL] libturbojpeg missing: the service will report it\n", out)
        self.assertEqual(self.files(self.cur), sorted(release_list()))
        self.assertTrue((self.cur / "lib/libffproc.so").is_file() and self.unit.is_file())
        self.assertEqual(json.loads((self.state / "selftest.json").read_text())["exit"], 4)
        self.assertIn("probe --quick", calls)

    def test_install_mode_refuses_to_run_from_an_installed_copy(self):
        self.run_sh("--no-start")
        self.run_sh("--no-start")
        shutil.copytree(str(self.upload), str(self.new))
        before = self.snapshot(self.cur, self.prev, self.new, self.state, self.conf.parent, self.unit.parent)
        for folder in (self.cur, self.prev, self.new):
            out, calls = self.run_sh(script=folder / "install.sh", rc=1)
            self.assertIn("run install.sh from the unpacked or uploaded folder", out)
            self.assertEqual(calls, [])
        self.assertEqual(self.snapshot(self.cur, self.prev, self.new, self.state, self.conf.parent, self.unit.parent),
                         before)

    def test_relative_invocation_ignores_an_exported_cdpath(self):
        decoy = self.root / "decoy" / self.upload.name                # `cd frameface-upload-test` could land here
        decoy.mkdir(parents=True)
        out, _ = self.run_sh("--no-start", script=self.upload.name + "/install.sh", CDPATH=str(decoy.parent))
        self.assertIn("staged %d files" % len(release_list()), out)
        self.assertEqual(self.files(self.cur), sorted(release_list()))

    def test_rollback_swaps_reinstalls_the_unit_and_runs_selftest_quick(self):
        self.run_sh("--no-start")
        unit1 = self.unit.read_bytes()
        self.run_sh("--no-start", script=self.make_upload("frameface-upload-v2", "0.6.1", "v2") / "install.sh")
        (self.mstate / "active").touch()
        out, calls = self.run_sh("--rollback", script=self.cur / "install.sh", FRAMEFACE_SELFTEST_FORCE=None)
        self.assertEqual((self.version(self.cur), self.version(self.prev)), ("0.6.0", "0.6.1"))
        self.assertEqual(self.unit.read_bytes(), unit1)
        self.assertEqual(self.systemctl(calls), ["--user is-active --quiet frameface.service",
                         "--user stop frameface.service", "--user daemon-reload", "--user start frameface.service"])
        result = json.loads((self.cur / "selftest.json").read_text())
        self.assertEqual((result["sim"], result["abi"], result.get("forced")), ("skip", "fail", None))   # --quick, mock lib
        self.assertIn(str(self.cur / "lib" / "abi_dump"), out)                                         # --lib ~/frameface/lib
        self.assertIn("[WARN] selftest --quick exit 2", out)
        self.assertEqual((self.state / "selftest.json").read_bytes(), (self.cur / "selftest.json").read_bytes())
        self.assertFalse(self.new.exists())
        self.run_sh("--rollback", script=self.upload / "install.sh")
        self.assertEqual((self.version(self.cur), self.version(self.prev)), ("0.6.1", "0.6.0"))
        self.assertEqual(self.unit.read_bytes(), unit1 + b"# v2\n")

    def test_rollback_without_a_previous_version_changes_nothing(self):
        out, calls = self.run_sh("--rollback", rc=1)
        self.assertIn("no previous version to roll back to", out)
        self.assertEqual(calls, [])
        self.run_sh("--no-start")
        before = self.snapshot(self.home)
        out, calls = self.run_sh("--rollback", rc=1)
        self.assertIn("no previous version to roll back to", out)
        self.assertEqual((calls, self.snapshot(self.home)), ([], before))

    def test_uninstall_twice_then_purge(self):
        self.run_sh("--no-start")
        self.run_sh("--no-start")
        self.new.mkdir()
        for rel in ("probe-20261106-101010/report.json", "probe-20261106-101010-images/raw_0.bin",
                    "probe-20261106-121212-images/trace-pixels.jsonl", "probe-stopped-service", "profile.json"):
            (self.state / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.state / rel).write_text("x")
        for other in ("frameface-upload-0123abcd", "frameface-upload-manual"):
            (self.home / other / "sub").mkdir(parents=True)
        out, calls = self.run_sh("--uninstall")
        self.assertEqual(self.systemctl(calls), ["--user disable --now frameface.service", "--user daemon-reload"])
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [".config", ".local", "frameface-upload-test"])
        self.assertFalse(self.unit.exists())
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["probe-20261106-101010", "profile.json",
                                                                        "selftest.json"])
        self.assertTrue(self.conf.is_file())
        self.assertEqual(self.files(self.upload), sorted(release_list()))
        snap = self.snapshot(self.home)
        self.run_sh("--uninstall")                          # the mock's `disable` now fails: ignored
        self.assertEqual(self.snapshot(self.home), snap)
        out, _ = self.run_sh("--uninstall", "--purge")
        self.assertFalse(self.conf.parent.exists() or self.state.exists())
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [".config", ".local", "frameface-upload-test"])

    def test_udev_text_only_when_the_quick_probe_reports_p7_fail(self):
        out, _ = self.run_sh("--no-start", MOCK_PROBE_P7="FAIL")
        self.assertIn("[FAIL] P7 node access", out)
        self.assertIn(UDEV_CMD, out)
        self.assertIn("/etc/atomic-update.conf.d/vive-facial-tracker.conf", out)

    def test_options_preflight_and_config_flags(self):
        for args in (["--purge"], ["--bogus"], ["--allow-peer", "300.1.1.1/32"], ["--rollback", "--uninstall"],
                     ["--uninstall", "--no-start"], ["--allow-peer"]):
            out, calls = self.run_sh(*args, rc=2)
            self.assertEqual(calls, [], args)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["frameface-upload-test"])
        out, calls = self.run_sh(rc=1, MOCK_NO_BUS="1")
        self.assertIn("systemctl --user does not work", out)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["frameface-upload-test"])
        self.run_sh("--no-start")
        token = self.token()
        (self.state / "profile.json").write_text("{}")
        self.run_sh("--no-start", "--new-token", "--reset-profile", "--allow-peer", "192.168.4.31/32",
                    "--allow-peer=192.168.4.32/32")
        data = json.loads(self.conf.read_text())
        self.assertNotEqual(data["token"], token)
        self.assertEqual(data["allow_peers"], ["10.35.78.0/24", "127.0.0.0/8", "192.168.4.31/32", "192.168.4.32/32"])
        self.assertFalse((self.state / "profile.json").exists())

    def test_real_gate(self):
        """The one unforced gate: real gcc, the real selftest (FRAMEFACE_NO_FPS_ASSERT=1) and the real probe."""
        ffutil.require_native("gcc", "turbojpeg", reason="the real install gate")
        (self.bin / "gcc").unlink()
        upload = self.make_upload("frameface-upload-real", real_probe=True)
        out, calls = self.run_sh("--no-start", script=upload / "install.sh", FRAMEFACE_SELFTEST_FORCE=None,
                                 FRAMEFACE_NO_FPS_ASSERT="1")
        self.assertIn("[ok] selftest passed", out)
        result = json.loads((self.state / "selftest.json").read_text())
        self.assertEqual({k: result[k] for k in ("exit", "abi", "pixels", "sim")},
                         {"exit": 0, "abi": "ok", "pixels": "c", "sim": "ok"})
        self.assertIn(result["jpeg"], ("tj3", "tj2"))
        self.assertNotIn("forced", result)
        self.assertNotIn(b"mock", (self.cur / "lib/libffproc.so").read_bytes())
        self.assertEqual([c for c in calls if c.startswith(("gcc", "probe"))], [])
        self.assertIn("quick probe:", out)
        self.assertIn("http://10.35.78.1:%d/stream/%s\n" % (self.port, self.token()), out)


if __name__ == "__main__":
    unittest.main()
