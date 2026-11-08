# SPDX-License-Identifier: MIT
"""install-frameface.ps1 (spec §9.2, §11.3 test_ff_install_ps1, task T5) in Windows PowerShell 5.1,
or in PowerShell 7 with FRAMEEYE_POWERSHELL=pwsh.

ssh and scp are .cmd recorders first on PATH (setUp refuses to run if they are not what PATH finds),
Set-Clipboard and Get-NetIPAddress (this PC's IPv4 addresses, FF_IPV4) are global function shims
defined by a small wrapper script, the headset is "frame.invalid" (never resolvable), and every run
passes a temporary -VrcftConfigDir and -VrcftInstallDir, so no test can reach the headset,
VRCFaceTracking's real settings or the Steam install folder. The ssh recorder answers the url call
with lines made by frameface.cli.url_lines (the golden format of test_ff_cli) and the status call
with a canned /status document.
"""
import ffutil  # first: installs the port guard

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ffutil.setup_path()
from frameface.cli import url_lines  # noqa: E402
from frameface.config import Config  # noqa: E402

FF = ffutil.FRAMEFACE_DIR
SHELL = shutil.which(os.environ.get("FRAMEEYE_POWERSHELL", "powershell"))
HOST = "frame.invalid"
REMOTE = "steamos@" + HOST
TOKEN = "AbCdEfGhIjKlMnOpQrStUv"
URLS = url_lines(Config(token=TOKEN, allow_peers=["10.35.78.0/24", "127.0.0.0/8", "192.168.4.0/24"]), "192.168.4.20")
URL_RE = re.compile(r"^http://(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})/stream(?:/([A-Za-z0-9_-]{16,64}))?$")
STAGE_RE = r"frameface-upload-[0-9a-f]{32}"
MODULE_ID = "58be7fa6-4102-44ab-adba-25021d3af8b7"
SETTINGS = "SteamFrameEyeModule.json"
STATUS = {"v": 1, "state": "IDLE", "display": "waiting_client", "severity": "info",
          "message": "Ready, waiting for Baballonia", "hint": "Face Camera Address, Wireless/IP Camera"}
RESTART = ("if [ -e ~/.local/state/frameface/probe-stopped-service ]; then XDG_RUNTIME_DIR=/run/user/$(id -u) "
           "systemctl --user start frameface.service && rm -f ~/.local/state/frameface/probe-stopped-service; fi")
CLI = "python3 -I ~/frameface/frameface_main.py"
RECORDER = r'''import hashlib, json, os, pathlib, re, sys
name, args = sys.argv[1], sys.argv[2:]
record = {"name": name, "args": args}
remote = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+:")
if name == "scp" and len(args) >= 2 and remote.match(args[-1]):
    stage = pathlib.Path(args[-2])
    files = {p.relative_to(stage).as_posix(): p.read_bytes() for p in stage.rglob("*") if p.is_file()}
    record["files"] = sorted(files)
    record["cr"] = sorted(n for n, data in files.items() if b"\r" in data)
    record["sha256"] = {n: hashlib.sha256(data).hexdigest() for n, data in files.items()}
elif name == "scp" and len(args) >= 2 and remote.match(args[-2]):
    pathlib.Path(args[-1]).mkdir(parents=True, exist_ok=True)
    pathlib.Path(args[-1], "report.json").write_text("{}")
with open(os.environ["FF_TRACE"], "a", encoding="utf-8") as f:
    f.write(json.dumps(record) + "\n")
with open(os.environ["FF_TRACE"], encoding="utf-8") as f:
    index = len(f.read().splitlines())
mock = json.loads(os.environ.get("FF_MOCK", "{}"))
for match, text in mock.get("stdout", []):
    if match in " ".join(args):
        sys.stdout.write(text)
        break
sys.exit(int(mock.get("exit", {}).get(str(index), 0)))
'''
WRAPPER = r'''$ErrorActionPreference = 'Stop'
function global:Set-Clipboard {
    param([Parameter(ValueFromPipeline = $true, Position = 0)][object[]]$Value, [switch]$Append, [switch]$PassThru)
    [IO.File]::AppendAllText($env:FF_CLIP, (($Value | ForEach-Object { [string]$_ }) -join ' ') + "`n")
    if ($env:FF_CLIP_FAIL) { throw 'clipboard unavailable (test)' }
}
function global:Get-NetIPAddress {
    [CmdletBinding()]
    param([string]$AddressFamily)
    [IO.File]::AppendAllText($env:FF_IPLOG, $AddressFamily + "`n")
    if ($env:FF_IPV4 -eq 'error') { throw 'NetTCPIP unavailable (test)' }
    foreach ($address in ([string]$env:FF_IPV4 -split ',')) { if ($address) { [pscustomobject]@{ IPAddress = $address } } }
}
$params = %s
try { & '%s' @params } catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }
exit $LASTEXITCODE
'''


def ps_literal(value):
    if value is True or value is False:
        return "$true" if value else "$false"
    if isinstance(value, (list, tuple)):
        return "@(" + ", ".join(ps_literal(v) for v in value) + ")"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def release_list():
    return (FF / "release-files.txt").read_text(encoding="ascii").splitlines()


class ScriptText(unittest.TestCase):
    def test_ascii_crlf_spdx_and_single_quoted_remote_literals(self):
        raw = (FF / "install-frameface.ps1").read_bytes()
        raw.decode("ascii")                                         # Windows PowerShell 5.1 reads BOM-less files as ANSI
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
        self.assertTrue(raw.startswith(b"# SPDX-License-Identifier: MIT\r\n"))
        text = raw.decode("ascii")
        self.assertIn("$restart = '" + RESTART + "'", text)
        self.assertIn("'bash ~/{0}/install.sh {1}; rc=$?; rm -rf -- ~/{0}; exit $rc'", text)


@unittest.skipUnless(os.name == "nt" and SHELL, "needs Windows PowerShell or PowerShell 7 on Windows")
class InstallPs1(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="ff ps1 "))
        self.addCleanup(shutil.rmtree, str(self.base), True)
        self.ff = self.base / "repo with spaces" / "headset" / "frameface"
        for rel in release_list():
            (self.ff / rel).parent.mkdir(parents=True, exist_ok=True)
            data = (FF / rel).read_bytes()
            if rel.endswith((".sh", ".py", ".service")):          # a CRLF checkout must still upload LF
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            (self.ff / rel).write_bytes(data)
        self.config = self.base / "vrcft config"
        self.install_dir = self.base / "vrcft install"
        self.config.mkdir()
        self.install_dir.mkdir()
        mocks = self.base / "mock commands"
        mocks.mkdir()
        (mocks / "record.py").write_text(RECORDER, encoding="utf-8")
        for name in ("ssh", "scp"):
            (mocks / (name + ".cmd")).write_text('@echo off\n"%s" "%s" %s %%*\n' % (sys.executable, mocks / "record.py", name),
                                                 encoding="ascii")
        self.trace, self.clip, self.iplog = self.base / "calls.jsonl", self.base / "clipboard.txt", self.base / "ipv4.log"
        # by default this PC is on the Frame link and on a LAN (FF_IPV4: what the Get-NetIPAddress shim reports)
        self.env = dict(os.environ, PATH=str(mocks) + os.pathsep + os.environ.get("PATH", ""), FF_TRACE=str(self.trace),
                        FF_CLIP=str(self.clip), FF_IPLOG=str(self.iplog), FF_IPV4="10.35.78.28,192.168.4.31",
                        PYTHONDONTWRITEBYTECODE="1")
        for name in ("ssh", "scp"):                                # never run the real OpenSSH client
            found = shutil.which(name, path=self.env["PATH"])
            self.assertTrue(found and os.path.normcase(os.path.dirname(found)) == os.path.normcase(str(mocks)), found)
        self.mock = {"stdout": [["frameface_main.py url", "".join(u + "\n" for u in URLS)],
                                ["status --json", json.dumps(STATUS, indent=1) + "\n"]]}

    def run_ps(self, rc=0, direct=False, **params):
        """Run the script (through the clipboard-shim wrapper unless direct); returns (output, calls, clipboard)."""
        params = dict({"HeadsetHost": HOST, "VrcftConfigDir": str(self.config),
                       "VrcftInstallDir": str(self.install_dir)}, **params)
        script = self.ff / "install-frameface.ps1"
        env = dict(self.env, FF_MOCK=json.dumps(self.mock))
        env.update(params.pop("_env", {}))
        if direct:
            args = ["-File", str(script)]
            for key, value in params.items():
                args += ["-" + key] if value is True else ["-%s:$false" % key] if value is False else ["-" + key, str(value)]
        else:
            wrapper = self.base / "run.ps1"
            body = "@{ " + "; ".join("%s = %s" % (k, ps_literal(v)) for k, v in params.items()) + " }"
            wrapper.write_text(WRAPPER % (body, str(script).replace("'", "''")), encoding="ascii")
            args = ["-File", str(wrapper)]
        for f in (self.trace, self.clip, self.iplog):
            if f.exists():
                f.unlink()
        p = subprocess.run([SHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"] + args, cwd=str(self.base),
                           env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        out = p.stdout.decode("utf-8", "replace")
        self.flat = " ".join(out.split())                          # Windows PowerShell wraps long warnings
        if rc == 0:
            self.assertEqual(p.returncode, 0, out)
        elif rc is not None:
            self.assertNotEqual(p.returncode, 0, out)
        calls = [json.loads(line) for line in self.trace.read_text(encoding="utf-8").splitlines()] if self.trace.exists() else []
        clips = self.clip.read_text(encoding="utf-8").splitlines() if self.clip.exists() else []
        return out, calls, clips

    def settings(self, data=None, raw=None):
        path = self.config / SETTINGS
        if data is not None or raw is not None:
            path.write_bytes(raw if raw is not None else json.dumps(data, indent=2).encode())
        return path

    def config_files(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.config.rglob("*") if p.is_file()}

    def backups(self):
        return sorted(p.name for p in self.config.glob(SETTINGS + ".bak-*"))

    @staticmethod
    def remote_command(call):
        return call["args"][-1]

    def test_install_is_scp_ssh_ssh_with_exactly_the_release_files(self):
        identity = self.base / "key with spaces"
        identity.write_text("fixture")
        out, calls, clips = self.run_ps(SshPort=2222, IdentityFile=str(identity), NoStart=True, NewToken=True,
                                        AllowPeer=["192.168.4.31/32", "10.0.0.0/8"])
        self.assertEqual([c["name"] for c in calls], ["scp", "ssh", "ssh"])
        up = calls[0]
        self.assertEqual(up["files"], sorted(release_list()))
        self.assertEqual(up["cr"], ["install-frameface.ps1"])      # .sh/.py/.service/.c/.md/.txt/.rules staged LF
        self.assertEqual(up["sha256"]["install.sh"], hashlib.sha256((FF / "install.sh").read_bytes()).hexdigest())
        stage = Path(up["args"][-2])
        self.assertRegex(stage.name, "^%s$" % STAGE_RE)
        self.assertEqual(up["args"], ["-P", "2222", "-i", str(identity), "-r", str(stage), "%s:%s" % (REMOTE, stage.name)])
        self.assertFalse(stage.exists())
        ssh = ["-p", "2222", "-i", str(identity), REMOTE]
        self.assertEqual(calls[1]["args"], ssh + ["bash ~/%s/install.sh --no-start --new-token --allow-peer 192.168.4.31/32 "
                                                  "--allow-peer 10.0.0.0/8" % stage.name])
        self.assertEqual(calls[2]["args"], ssh + ["rm -rf -- ~/%s && python3 -I ~/frameface/frameface_main.py url" % stage.name])
        printed = [line.strip() for line in out.splitlines() if URL_RE.match(line.strip())]
        self.assertEqual(printed, URLS)
        first = URL_RE.match(printed[0])
        self.assertEqual(first.groups(), ("10.35.78.1", "9023", TOKEN))
        self.assertEqual(clips, [])
        self.assertNotIn("clipboard", self.flat.lower())
        self.assertIn("Expand-Archive -Force .\\dist\\SteamFrameEyeModule-0.6.0.zip", self.flat)

    def url_run(self, ipv4, urls, **params):
        """Install with -CopyUrl while url prints these lines and this PC has these IPv4 addresses; returns
        (clipboard, the address families the script asked Get-NetIPAddress for)."""
        self.mock["stdout"][0][1] = "".join(u + "\n" for u in urls)
        out, calls, clips = self.run_ps(CopyUrl=True, _env={"FF_IPV4": ipv4}, **params)
        self.assertEqual([c["name"] for c in calls], ["scp", "ssh", "ssh"])
        self.assertEqual([line.strip() for line in out.splitlines() if URL_RE.match(line.strip())], urls)
        return clips, (self.iplog.read_text().split() if self.iplog.exists() else [])

    def test_one_url_is_named_and_copied_as_before(self):
        clips, queried = self.url_run("192.168.4.31", URLS[:1])
        self.assertEqual((clips, queried), ([URLS[0]], []))           # this PC's addresses do not matter
        self.assertIn("[info] The URL is on the clipboard. Windows clipboard history and cloud clipboard sync may keep "
                      "a copy (it contains the stream token).", self.flat)
        self.assertIn('2. Baballonia: Face Camera Address = the URL above, then "This is a..." = Wireless/IP Camera',
                      self.flat)
        self.assertNotIn("Alternative", self.flat)
        out, calls, clips = self.run_ps(CopyUrl=True, _env={"FF_CLIP_FAIL": "1"})
        self.assertEqual(len(clips), 1)
        self.assertIn("Could not copy the URL to the clipboard", self.flat)
        self.assertEqual([c["name"] for c in calls], ["scp", "ssh", "ssh"])

    def test_two_urls_and_a_frame_link_address_choose_the_frame_link_url(self):
        clips, queried = self.url_run("192.168.4.31,10.35.78.28", URLS)
        self.assertEqual((clips, queried), ([URLS[0]], ["IPv4"]))
        self.assertIn("[info] The Frame-link URL (10.35.78.1) is on the clipboard.", self.flat)
        self.assertIn("2. Baballonia: Face Camera Address = %s (this PC has an address on the Frame link 10.35.78.0/24), "
                      'then "This is a..." = Wireless/IP Camera' % URLS[0], self.flat)
        self.assertIn("Alternative: %s (the headset's LAN address): use it on a PC that reaches the headset only over "
                      "the home LAN." % URLS[1], self.flat)

    def test_two_urls_without_a_frame_link_address_choose_the_lan_url(self):
        clips, queried = self.url_run("192.168.4.31,169.254.7.7", URLS)
        self.assertEqual((clips, queried), ([URLS[1]], ["IPv4"]))
        self.assertIn("[info] The LAN URL (192.168.4.20) is on the clipboard.", self.flat)
        self.assertIn("2. Baballonia: Face Camera Address = %s (the headset's LAN address: this PC has no address on the "
                      'Frame link 10.35.78.0/24), then "This is a..." = Wireless/IP Camera' % URLS[1], self.flat)
        self.assertIn("Alternative: %s (the Frame link): use it when this PC has an address in 10.35.78.0/24." % URLS[0],
                      self.flat)
        # Without Get-NetIPAddress the script reads the interfaces through .NET: one of the two, named as copied.
        clips, queried = self.url_run("error", URLS)
        self.assertEqual(queried, ["IPv4"])
        self.assertIn(clips, ([URLS[0]], [URLS[1]]))
        self.assertIn("[info] The %s is on the clipboard." % ("Frame-link URL (10.35.78.1)" if clips == URLS[:1]
                                                              else "LAN URL (192.168.4.20)"), self.flat)

    def test_no_start_names_the_start_command_for_this_pc(self):
        # install.sh tells a user on the headset how to start the service; the PC user gets the ssh form (K4).
        headset_form = "start it with: %s start" % CLI
        self.mock["stdout"].insert(0, ["install.sh --no-start", "[info] not started (--no-start); %s\n" % headset_form])
        out, calls, _ = self.run_ps(NoStart=True, User="deck")
        self.assertEqual([c["name"] for c in calls], ["scp", "ssh", "ssh"])
        self.assertLess(out.index(headset_form), out.index("Next steps:"))       # relayed first, then the next step
        self.assertIn('Next steps: 1. Start the face service (installed with -NoStart), from this PC: ssh deck@%s "%s '
                      'start" 2. Install the module with VRCFaceTracking closed' % (HOST, CLI), self.flat)
        out, calls, _ = self.run_ps()
        self.assertNotIn("frameface_main.py start", out)
        self.assertIn("Next steps: 1. Install the module with VRCFaceTracking closed", self.flat)

    def test_face_source_merge(self):
        original = {"eye_enabled": False, "smile_slant": True, "face_min_rate_hz": 12, "status_keeper": "off",
                    "nested": {"a": [1, 2.5, "x"], "b": None}}
        path = self.settings(original)
        before = path.read_bytes()
        out, _, _ = self.run_ps()                                   # absent -> written, with a backup
        self.assertEqual(json.loads(path.read_bytes()), dict(original, face_source="babble_osc"))
        self.assertFalse(path.read_bytes().startswith(b"\xef\xbb\xbf"))
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertRegex(backups[0], r"^SteamFrameEyeModule\.json\.bak-\d{8}-\d{6}$")
        self.assertEqual((self.config / backups[0]).read_bytes(), before)
        files = self.config_files()
        out, _, _ = self.run_ps()                                   # already babble_osc -> no write, no backup
        self.assertEqual(self.config_files(), files)
        self.assertIn('face_source is already "babble_osc"', self.flat)
        self.settings(dict(original, face_source="off"))
        files = self.config_files()
        out, _, _ = self.run_ps()                                   # "off" is kept
        self.assertEqual(self.config_files(), files)
        self.assertIn('[info] face_source is "off"; run again with -EnableFace to turn the face path on', self.flat)
        out, _, _ = self.run_ps(EnableFace=True)                    # -EnableFace -> written, with a backup
        self.assertEqual(json.loads(path.read_bytes()), dict(original, face_source="babble_osc"))
        self.assertEqual(len(self.backups()), 2)
        self.assertEqual(sorted(json.loads((self.config / b).read_bytes()).get("face_source") for b in self.backups()
                                if "face_source" in json.loads((self.config / b).read_bytes())), ["off"])

    def test_invalid_missing_and_skipped_settings(self):
        path = self.settings(raw=b'{"eye_enabled": true,')
        files = self.config_files()
        out, _, _ = self.run_ps()
        self.assertEqual(self.config_files(), files)
        self.assertIn("is not a valid JSON object; it was left untouched", self.flat)
        path.unlink()
        out, _, _ = self.run_ps()                                   # missing -> created, no backup
        self.assertEqual(json.loads(path.read_bytes()), {"face_source": "babble_osc"})
        self.assertEqual(self.backups(), [])
        self.settings({"eye_enabled": True})
        files = self.config_files()
        out, calls, _ = self.run_ps(SkipModuleSettings=True)
        self.assertEqual((self.config_files(), [c["name"] for c in calls]), (files, ["scp", "ssh", "ssh"]))

    def test_whatif_makes_no_calls_and_writes_nothing(self):
        self.settings({"eye_enabled": True})
        files = self.config_files()
        for mode in ({"CopyUrl": True}, {"Probe": True}, {"ProbeActive": True}, {"Check": True}, {"Uninstall": True},
                     {"Rollback": True}):
            out, calls, clips = self.run_ps(WhatIf=True, **mode)
            self.assertEqual((calls, clips, self.config_files()), ([], [], files), mode)
        self.settings({"eye_enabled": True, "face_source": "babble_osc"})
        files = self.config_files()
        out, calls, _ = self.run_ps(WhatIf=True, Uninstall=True)
        self.assertEqual((calls, self.config_files()), ([], files))
        self.assertIn("What if", self.flat)

    def test_remote_failure_keeps_the_upload_and_the_settings(self):
        self.settings({"eye_enabled": True})
        files = self.config_files()
        self.mock["exit"] = {"2": 1}
        out, calls, _ = self.run_ps(rc=1)
        self.assertEqual([c["name"] for c in calls], ["scp", "ssh"])
        self.assertRegex(self.flat, r"The upload stays at ~/%s" % STAGE_RE)
        self.assertEqual(self.config_files(), files)
        self.assertFalse(Path(calls[0]["args"][-2]).exists())

    def test_a_staging_failure_leaves_no_temp_folder(self):
        import msvcrt
        temp = self.base / "private temp"
        temp.mkdir()
        with open(str(self.ff / "install.sh"), "rb") as f:                # unreadable while it is staged
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1 << 20)
            try:
                out, calls, _ = self.run_ps(rc=1, _env={"TMP": str(temp), "TEMP": str(temp)})
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1 << 20)
        self.assertEqual(calls, [])
        self.assertEqual(sorted(p.name for p in temp.glob("frameface-upload-*")), [], out)

    def test_probe_is_ssh_then_scp(self):
        out, calls, _ = self.run_ps(Probe=True)
        self.assertEqual([c["name"] for c in calls], ["ssh", "scp"])
        cmd = self.remote_command(calls[0])
        bundle = re.fullmatch(r"python3 -I ~/frameface/frameface_main.py probe --bundle-name (probe-\d{8}-\d{6})", cmd).group(1)
        local = self.base / ("frameface-" + bundle)
        self.assertEqual(calls[1]["args"], ["-P", "22", "-r", "%s:.local/state/frameface/%s" % (REMOTE, bundle), str(local)])
        self.assertTrue((local / "report.json").is_file())
        self.assertIn(str(local), self.flat)

    def test_probe_active_is_ssh_t_restart_and_scp(self):
        out, calls, _ = self.run_ps(ProbeActive=True, Confirm=False, ProbeArgs="--matrix --soak 300")
        self.assertEqual([c["name"] for c in calls], ["ssh", "ssh", "scp"])
        self.assertEqual(calls[0]["args"][:4], ["-p", "22", "-t", REMOTE])
        bundle = re.fullmatch(r"python3 -I ~/frameface/frameface_main.py probe --active --yes --stop-service --bundle-name "
                              r"(probe-\d{8}-\d{6}) --matrix --soak 300", self.remote_command(calls[0])).group(1)
        self.assertEqual(calls[1]["args"], ["-p", "22", REMOTE, RESTART])
        self.assertEqual(calls[2]["args"][-2:], ["%s:.local/state/frameface/%s" % (REMOTE, bundle),
                                                 str(self.base / ("frameface-" + bundle))])
        self.mock["exit"] = {"1": 255}                              # the probe's SSH dropped: restart and copy anyway
        out, calls, _ = self.run_ps(ProbeActive=True, Confirm=False, IncludeImages=True)
        self.assertEqual([c["name"] for c in calls], ["ssh", "ssh", "scp", "scp"])
        self.assertEqual(self.remote_command(calls[1]), RESTART)
        bundle = calls[2]["args"][-2].rsplit("/", 1)[1]
        self.assertEqual(calls[3]["args"][-2:], ["%s:.local/state/frameface/%s-images" % (REMOTE, bundle),
                                                 str(self.base / ("frameface-%s-images" % bundle))])

    def test_probe_active_restart_failure_names_the_cli_start_command(self):
        # Over SSH a bare `systemctl --user start` lacks XDG_RUNTIME_DIR; the CLI sets it (ops-3).
        self.mock["exit"] = {"2": 1}                                # the start-if-stopped call fails
        out, calls, _ = self.run_ps(ProbeActive=True, Confirm=False)
        self.assertEqual([c["name"] for c in calls], ["ssh", "ssh", "scp"])
        self.assertIn('Could not restart the face service (ssh exit code 1); run: ssh %s '
                      '"python3 -I ~/frameface/frameface_main.py start"' % REMOTE, self.flat)
        self.assertNotIn("systemctl", self.flat)

    def test_probe_active_asks_first_and_probe_args_are_checked(self):
        out, calls, _ = self.run_ps(rc=1, ProbeActive=True)        # -NonInteractive: the prompt fails, nothing runs
        self.assertEqual(calls, [])
        for direct in (False, True):
            out, calls, _ = self.run_ps(rc=1, direct=direct, ProbeActive=True, ProbeArgs="x; rm -rf")
            self.assertEqual(calls, [])
            self.assertIn("ProbeArgs", self.flat)

    def test_uninstall_and_rollback_upload_install_sh_alone(self):
        path = self.settings({"eye_enabled": True, "face_source": "babble_osc"})
        sha = hashlib.sha256((FF / "install.sh").read_bytes()).hexdigest()
        expected = []
        for params, flags in (({"Uninstall": True}, "--uninstall"), ({"Uninstall": True}, "--uninstall"),
                              ({"Uninstall": True, "Purge": True}, "--uninstall --purge"), ({"Rollback": True}, "--rollback"),
                              ({"Rollback": True}, "--rollback")):
            out, calls, _ = self.run_ps(**params)
            self.assertEqual([c["name"] for c in calls], ["scp", "ssh"], params)
            self.assertEqual((calls[0]["files"], calls[0]["cr"], calls[0]["sha256"]), (["install.sh"], [], {"install.sh": sha}))
            name = Path(calls[0]["args"][-2]).name
            self.assertRegex(name, "^%s$" % STAGE_RE)
            self.assertEqual(calls[0]["args"][-1], "%s:%s" % (REMOTE, name))
            self.assertEqual(calls[1]["args"], ["-p", "22", REMOTE, "bash ~/%s/install.sh %s; rc=$?; rm -rf -- ~/%s; exit $rc"
                                                % (name, flags, name)])
            expected.append(name)
            if len(expected) == 1:                                  # the first uninstall removes the key, with a backup
                self.assertEqual(json.loads(path.read_bytes()), {"eye_enabled": True})
                self.assertEqual(len(self.backups()), 1)
                files = self.config_files()
            else:
                self.assertEqual(self.config_files(), files)
        self.assertEqual(len(set(expected)), 5)                       # a fresh upload folder every time

    def test_check_lists_every_module_and_writes_nothing(self):
        libs = self.config / "CustomLibs"
        # the three known IDs carry neutral names (flagged by id); the official Frame module is flagged by name
        modules = {MODULE_ID: ("Steam Frame Eye + Face (network)", "0.6.0", "SteamFrameEyeModule.dll"),
                   "360b014b-b57b-450f-8f12-9904618ff370": ("Neutral A", "2.1.2", "a.dll"),
                   "2a8c8080-2a76-46af-bf76-1da7c0127ef8": ("Neutral B", "1.0.0", "b.dll"),
                   "74f18332-1bcf-4b55-832d-9c91069afa31": ("Neutral C", "5.0.0", "c.dll"),
                   "0f0f0f0f-0000-4000-8000-000000000001": ("SteamFrameTrackingModule", "1.0.0", "d.dll"),
                   "0f0f0f0f-0000-4000-8000-000000000002": ("Some Other Module", "3.2.1", "OtherModule.dll")}
        reasons = ["VRCFT-Babble claims expressions and binds UDP 8888", "Steam Link / Meta face tracking claims eyes and "
                   "expressions", "SRanipal claims eyes and lips", "the official Steam Frame eye module claims the eyes"]
        for folder, (name, version, dll) in modules.items():
            (libs / folder).mkdir(parents=True)
            (libs / folder / "module.json").write_text(json.dumps({"ModuleId": folder, "ModuleName": name, "Version": version,
                                                                   "DllFileName": dll}))
            (libs / folder / dll).write_bytes(b"dll")
        (libs / "SteamFrameEye-backup").mkdir()                     # a backup copy inside CustomLibs
        (libs / "SteamFrameEye-backup" / "module.json").write_text(json.dumps({"ModuleId": MODULE_ID, "ModuleName": "old",
                                                                               "Version": "0.5.3"}))
        (libs / "LegacyThing.dll").write_bytes(b"dll")
        self.settings({"face_source": "babble_osc"})
        files = self.config_files()
        out, calls, _ = self.run_ps(Check=True, VrcftInstallDir=str(ffutil.REPO / "lib" / "vrcft-5.4.5"))
        self.assertEqual([(c["name"], self.remote_command(c)) for c in calls],
                         [("ssh", "python3 -I ~/frameface/frameface_main.py status --json")])
        self.assertEqual(self.config_files(), files)
        self.assertRegex(self.flat, r"\[ok\] VRCFaceTracking 5\.4\.5")
        self.assertIn("[ok] SteamFrameEye module: Steam Frame Eye + Face (network) 0.6.0 (%s)" % MODULE_ID, self.flat)
        for folder, reason in zip(list(modules)[1:5], reasons):
            name, version = modules[folder][:2]
            self.assertIn("[fail] %s %s (%s) in CustomLibs\\%s: %s" % (name, version, folder, folder, reason), self.flat)
        self.assertIn("[fail] second copy of SteamFrameEye: old 0.5.3 (%s) in CustomLibs\\SteamFrameEye-backup" % MODULE_ID, self.flat)
        self.assertIn("[warn] other module: Some Other Module 3.2.1 (0f0f0f0f-0000-4000-8000-000000000002)", self.flat)
        self.assertIn("[warn] other module: legacy module CustomLibs\\LegacyThing.dll", self.flat)
        self.assertIn('[ok] face_source = "babble_osc"', self.flat)
        # read-only queries of this PC (no socket is opened): their verdict depends on what runs here
        self.assertRegex(self.flat, r"\[(ok|warn|fail)\] (UDP 8888 is held by|nothing listens on UDP 8888|cannot list UDP)")
        self.assertRegex(self.flat, r"\[(ok|info)\] Baballonia is (not )?running")
        self.assertIn("[ok] FrameFace on %s: waiting_client: Ready, waiting for Baballonia" % HOST, self.flat)
        self.assertIn("hint: Face Camera Address, Wireless/IP Camera", self.flat)

    def test_check_reports_an_interrupted_install_local_module(self):
        (self.config / "CustomLibs" / MODULE_ID).mkdir(parents=True)
        (self.config / "CustomLibs" / MODULE_ID / "SteamFrameEyeModule.dll").write_bytes(b"dll")
        out, calls, _ = self.run_ps(Check=True)
        self.assertIn("[fail] CustomLibs\\%s has no module.json: an interrupted 'Install local module'" % MODULE_ID, self.flat)
        self.assertIn("[warn] VRCFaceTracking.Core.dll not found", self.flat)
        self.assertEqual([c["name"] for c in calls], ["ssh"])

    def test_bad_combinations_make_no_calls(self):
        for params in ({"Probe": True, "Check": True}, {"Purge": True}, {"IncludeImages": True},
                       {"Check": True, "CopyUrl": True}, {"Probe": True, "ProbeArgs": "--quick"},
                       {"AllowPeer": ["300.1.1.1/32"]}, {"Rollback": True, "SkipModuleSettings": True}):
            out, calls, _ = self.run_ps(rc=1, **params)
            self.assertEqual(calls, [], params)

    def test_direct_file_invocation(self):
        out, calls, _ = self.run_ps(direct=True, WhatIf=True)
        self.assertEqual(calls, [])
        out, calls, clips = self.run_ps(direct=True, NoStart=True)
        self.assertEqual([c["name"] for c in calls], ["scp", "ssh", "ssh"])
        self.assertEqual([line.strip() for line in out.splitlines() if URL_RE.match(line.strip())], URLS)
        self.assertEqual(json.loads((self.config / SETTINGS).read_bytes()), {"face_source": "babble_osc"})

    def test_allow_peer_list_under_the_documented_file_call(self):
        # With -File, "-AllowPeer a,b" arrives as one string (ops-4): the script splits it on commas.
        for value in ("192.168.4.31/32,10.0.0.0/8", " 192.168.4.31/32 , 10.0.0.0/8 "):
            out, calls, _ = self.run_ps(direct=True, NoStart=True, AllowPeer=value)
            self.assertEqual([c["name"] for c in calls], ["scp", "ssh", "ssh"], value)
            self.assertEqual(self.remote_command(calls[1]), "bash ~/%s/install.sh --no-start --allow-peer 192.168.4.31/32 "
                             "--allow-peer 10.0.0.0/8" % Path(calls[0]["args"][-2]).name, value)
        for value in ("192.168.4.31/32,300.1.1.1/8", "192.168.4.31/32,10.0.0.0/33", "192.168.4.31/32;10.0.0.0/8"):
            out, calls, _ = self.run_ps(rc=1, direct=True, NoStart=True, AllowPeer=value)
            self.assertEqual(calls, [], value)
            self.assertIn("Invalid -AllowPeer", self.flat, value)


if __name__ == "__main__":
    unittest.main()
