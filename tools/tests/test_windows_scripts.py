# SPDX-License-Identifier: MIT
"""Integration checks for native PowerShell packaging and SSH command routing.

Run on Windows: python -m unittest discover -s tools/tests -v
Set FRAMEEYE_POWERSHELL=pwsh to also exercise PowerShell 7. SSH is replaced with
local command recorders; these checks never connect to or change a headset.
Packaging builds three archives: the module zip, FrameEye Bridge (headset/release-files.txt)
and FrameFace (headset/frameface/release-files.txt).
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile


REPO = Path(__file__).resolve().parents[2]
SHELL = shutil.which(os.environ.get("FRAMEEYE_POWERSHELL", "powershell"))
SYSTEM_TAR = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
VERSION = "1.2.3-test.4"
# FrameFace files that run on the headset (Linux) must be LF in the archive. package.ps1 archives the bytes as
# they are; .gitattributes (eol=lf) keeps these files LF in every checkout, also with core.autocrlf=true.
LF_SUFFIXES = (".sh", ".py", ".c", ".service", ".rules", ".txt")
MODULE_TEXT = {  # spec §10.1
    "ModuleName": "Steam Frame Eye + Face (network)",
    "ModuleDescription": "Per-eye gaze and eyelid openness from the Valve Steam Frame's eye tracking (FrameEye "
                         "Bridge, UDP 9021), plus mouth and face expressions from Baballonia (OSC on 127.0.0.1:8888) "
                         "for a VIVE Facial Tracker on the Frame (FrameFace).",
    "UsageInstructions": "Eyes: install FrameEye Bridge on the headset. Face: install FrameFace "
                         "(headset/frameface/install-frameface.ps1), point Baballonia's Face Camera Address at the "
                         "printed URL as Wireless/IP Camera, and do not install VRCFT-Babble. See "
                         "github.com/BowmanFox/SteamFrameEye.",
}


def release_list(folder):
    return (folder / "release-files.txt").read_text(encoding="ascii").splitlines()


@unittest.skipUnless(os.name == "nt" and SHELL, "requires Windows PowerShell or PowerShell 7")
class WindowsScripts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="frameeye scripts ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo with spaces"
        self.repo.mkdir()
        for name in ("build.ps1", "README.md", "LICENSE", "SteamFrameEyeModule.csproj"):
            shutil.copy2(REPO / name, self.repo / name)
        shutil.copytree(REPO / "packaging", self.repo / "packaging")
        self.headset_files = release_list(REPO / "headset")
        self.frameface_files = release_list(REPO / "headset/frameface")
        for folder, names in (("headset", self.headset_files), ("headset/frameface", self.frameface_files)):
            for name in names:
                target = self.repo / folder / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPO / folder / name, target)
        self.dll = self.repo / "bin/Release/net10.0/SteamFrameEyeModule.dll"
        self.dll.parent.mkdir(parents=True)
        self.dll.write_bytes(b"package fixture; runtime is built separately")
        self.env = dict(os.environ)
        self.mock_tar = None

    def run_script(self, script, *args, ok=True):
        result = subprocess.run(
            [SHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(self.repo / script), *map(str, args)],
            cwd=self.base, env=self.env, text=True, capture_output=True, timeout=60,
        )
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def package(self, *args, **kwargs):
        return self.run_script("packaging/package.ps1", "-Version", VERSION, *args, **kwargs)

    def install(self, *args, **kwargs):
        return self.run_script("headset/install-overlay.ps1", "-HeadsetHost", "frame.example",
                               "-PcAddress", "192.0.2.10:9022", *args, **kwargs)

    def mock_commands(self):
        mocks = self.base / "mock commands"
        mocks.mkdir()
        self.trace = self.base / "calls.jsonl"
        self.env["FRAMEEYE_TEST_TRACE"] = str(self.trace)
        self.env["PATH"] = str(mocks) + os.pathsep + self.env["PATH"]
        recorder = mocks / "record.py"
        recorder.write_text('''import json, os, pathlib, sys
name, *args = sys.argv[1:]
record = {"name": name, "args": args}
if name == "scp":
    stage = pathlib.Path(args[-2])
    record["files"] = sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file())
    record["shell_lf"] = all(b"\\r" not in p.read_bytes() for p in stage.rglob("*.sh"))
with open(os.environ["FRAMEEYE_TEST_TRACE"], "a", encoding="utf-8") as f:
    f.write(json.dumps(record) + "\\n")
fail = os.environ.get("FRAMEEYE_TEST_FAIL") == name and os.environ.get("FRAMEEYE_TEST_FAIL_ARG", "") in " ".join(args)
sys.exit(23 if fail else 0)
''', encoding="utf-8")
        for name in ("ssh", "scp", "tar", "dotnet"):
            (mocks / (name + ".cmd")).write_text(
                '@echo off\n"' + sys.executable + '" "' + str(recorder) + '" ' + name + ' %*\n',
                encoding="utf-8",
            )
        self.mock_tar = mocks / "tar.cmd"

    def calls(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def assert_archive(self, path, folder, names):
        """The tarball holds exactly `names`, each with the bytes of the file in `folder`."""
        with tarfile.open(path) as t:
            members = t.getnames()
            self.assertEqual(sorted(members), sorted(names), path.name)
            for name in names:
                self.assertEqual(t.extractfile(name).read(), (folder / name).read_bytes(), name)
        return members

    def test_local_archive_layout_and_no_personal_files(self):
        for name in ("frameeye.conf.json", "frameeye.conf.json.tmp", "frameeye.vrmanifest",
                     "frameeye_overlay.log", "frameeye_overlay.log.old", "frameeye_overlay.lock"):
            (self.repo / "headset" / name).write_text("private fixture")
        # What a checkout used for development may hold next to the FrameFace sources: none of it ships.
        ff = self.repo / "headset/frameface"
        for name in ("lib/libffproc.so", "lib/abi_dump", "lib/build.log", "frameface/__pycache__/cli.cpython-312.pyc",
                     "config.json", "selftest.json", "frameface-probe-20261107-120000/report.json",
                     "probe-20261107-120000/trace.jsonl", "probe-20261107-120000-images/frame-0001.bin"):
            (ff / name).parent.mkdir(parents=True, exist_ok=True)
            (ff / name).write_text("private fixture")
        self.package()
        output = self.repo / "dist"
        self.assertEqual(sorted(p.name for p in output.iterdir()),
                         sorted(["SteamFrameEyeModule-%s.zip" % VERSION, "FrameEye-Bridge-%s.tar.gz" % VERSION,
                                 "FrameFace-%s.tar.gz" % VERSION, "module.json"]))
        with zipfile.ZipFile(output / ("SteamFrameEyeModule-%s.zip" % VERSION)) as z:
            self.assertEqual(set(z.namelist()), {"module.json", "SteamFrameEyeModule.dll", "README.md", "LICENSE"})
            self.assertEqual(z.read("SteamFrameEyeModule.dll"), self.dll.read_bytes())
            manifest = json.loads(z.read("module.json"))
            self.assertTrue(manifest["IsLocal"])
            self.assertEqual(manifest["DownloadUrl"], "")
            self.assertEqual(manifest["ModuleId"], "58be7fa6-4102-44ab-adba-25021d3af8b7")
            self.assertEqual(manifest["Version"], VERSION)
            self.assertEqual(manifest["DllFileName"], "SteamFrameEyeModule.dll")
            for key, text in MODULE_TEXT.items():
                self.assertEqual(manifest[key], text, key)
            self.assertEqual(json.loads((output / "module.json").read_bytes()), manifest)
        # The FrameEye Bridge archive is unchanged: exactly its release list, byte for byte.
        self.assert_archive(output / ("FrameEye-Bridge-%s.tar.gz" % VERSION), self.repo / "headset", self.headset_files)
        with tarfile.open(output / ("FrameEye-Bridge-%s.tar.gz" % VERSION)) as t:
            self.assertTrue(t.extractfile("install-overlay.ps1").read().startswith(b"#Requires"))
        members = self.assert_archive(output / ("FrameFace-%s.tar.gz" % VERSION), ff, self.frameface_files)
        self.assertEqual(len(members), len(self.frameface_files))
        for name in members:
            self.assertFalse(name.endswith(".so") or "__pycache__" in name or name.startswith("lib/"), name)
            self.assertNotIn(name.rsplit("/", 1)[-1], ("config.json", "selftest.json"))
            # the probe's modules ship; probe bundles and images never do
            self.assertTrue("probe" not in name or re.fullmatch(r"frameface/probe\w*\.py", name), name)
        with tarfile.open(output / ("FrameFace-%s.tar.gz" % VERSION)) as t:
            for name in members:
                if name.endswith(LF_SUFFIXES):
                    self.assertNotIn(b"\r", t.extractfile(name).read(), name)
            self.assertTrue(t.extractfile("install.sh").read().startswith(b"#!/bin/bash\n"))

    def test_release_urls_and_absolute_paths(self):
        output = self.base / "release output"
        custom_dll = self.base / "custom module.dll"
        custom_dll.write_bytes(b"different build")
        url = "https://example.com/releases/v1.2.3/"
        page = "https://example.com/project?one=1&two=2"
        self.package("-DownloadUrlBase", url, "-PageUrl", page,
                     "-DllPath", custom_dll, "-OutDir", output)
        with zipfile.ZipFile(output / ("SteamFrameEyeModule-%s.zip" % VERSION)) as z:
            manifest = json.loads(z.read("module.json"))
            self.assertFalse(manifest["IsLocal"])
            self.assertEqual(manifest["DownloadUrl"], url + "SteamFrameEyeModule-%s.zip" % VERSION)
            self.assertEqual(manifest["ModulePageUrl"], page)
            self.assertEqual(z.read("SteamFrameEyeModule.dll"), b"different build")
        self.assert_archive(output / ("FrameFace-%s.tar.gz" % VERSION), self.repo / "headset/frameface",
                            self.frameface_files)
        self.assertFalse((self.repo / "dist").exists())

    def test_missing_dll_and_tar_failure_do_not_publish(self):
        self.package("-DllPath", "absent.dll", ok=False)
        self.assertFalse((self.repo / "dist").exists())
        self.mock_commands()
        self.env["FRAMEEYE_TEST_FAIL"] = "tar"
        result = self.package("-TarPath", self.mock_tar, ok=False)
        self.assertIn("tar exit code 23", result.stderr)
        self.assertEqual([c["name"] for c in self.calls()], ["tar"])
        self.assertFalse((self.repo / "dist").exists())

    def test_frameface_failures_do_not_publish(self):
        self.mock_commands()
        # Only the FrameFace tar fails: the zip and the bridge archive were built, yet nothing is published.
        self.env["FRAMEEYE_TEST_FAIL"] = "tar"
        self.env["FRAMEEYE_TEST_FAIL_ARG"] = "FrameFace-%s.tar.gz" % VERSION
        result = self.package("-TarPath", self.mock_tar, ok=False)
        self.assertIn("FrameFace packaging failed (tar exit code 23)", result.stderr)
        calls = self.calls()
        self.assertEqual([c["name"] for c in calls], ["tar", "tar"])
        self.assertTrue(calls[0]["args"][1].endswith("FrameEye-Bridge-%s.tar.gz" % VERSION), calls[0])
        self.assertEqual(calls[1]["args"][2:4], ["-C", str(self.repo / "headset" / "frameface")])
        self.assertEqual(calls[1]["args"][4:], self.frameface_files)
        self.assertFalse((self.repo / "dist").exists())
        # A tar that exits 0 without writing its archive is caught before anything is published.
        del self.env["FRAMEEYE_TEST_FAIL"]
        result = self.package("-TarPath", self.mock_tar, ok=False)
        self.assertIn("Packaging did not produce FrameEye-Bridge-%s.tar.gz" % VERSION, result.stderr)
        self.assertFalse((self.repo / "dist").exists())
        # A listed FrameFace file that is missing stops packaging before any tar runs.
        os.remove(self.repo / "headset/frameface/frameface/worn.py")
        before = len(self.calls())
        result = self.package(ok=False)
        self.assertIn("FrameFace release file not found: frameface/worn.py", result.stderr)
        self.assertEqual(len(self.calls()), before)
        self.assertFalse((self.repo / "dist").exists())

    @unittest.skipUnless(SYSTEM_TAR.is_file(), "needs Windows' own tar.exe in System32")
    def test_windows_tar_is_used_even_when_another_tar_comes_first_on_path(self):
        # Git Bash puts GNU tar (which reads "C:\..." as a remote host) before System32 on PATH; the
        # failing mock stands in for it and must never be called.
        self.mock_commands()
        self.env["FRAMEEYE_TEST_FAIL"] = "tar"
        self.package()
        self.assertEqual(self.calls(), [])
        self.assert_archive(self.repo / "dist" / ("FrameFace-%s.tar.gz" % VERSION), self.repo / "headset/frameface",
                            self.frameface_files)

    @unittest.skipUnless(SYSTEM_TAR.is_file(), "needs Windows' own tar.exe in System32")
    def test_build_script_publishes_the_module_and_both_headset_archives(self):
        # build.ps1 (pinned) passes the csproj version to package.ps1; only dotnet is mocked.
        self.mock_commands()
        version = re.search(r"<Version>([^<]+)</Version>",
                            (self.repo / "SteamFrameEyeModule.csproj").read_text(encoding="utf-8")).group(1)
        self.run_script("build.ps1")
        calls = self.calls()
        self.assertEqual([c["name"] for c in calls], ["dotnet"])
        self.assertIn("-p:Version=" + version, calls[0]["args"])
        output = self.repo / "dist"
        self.assertEqual(sorted(p.name for p in output.iterdir()),
                         sorted(["SteamFrameEyeModule-%s.zip" % version, "FrameEye-Bridge-%s.tar.gz" % version,
                                 "FrameFace-%s.tar.gz" % version, "module.json"]))
        self.assert_archive(output / ("FrameEye-Bridge-%s.tar.gz" % version), self.repo / "headset", self.headset_files)
        self.assert_archive(output / ("FrameFace-%s.tar.gz" % version), self.repo / "headset/frameface",
                            self.frameface_files)
        self.assertEqual(json.loads((output / "module.json").read_bytes())["Version"], version)

    def test_build_failure_stops_before_packaging(self):
        self.mock_commands()
        self.env["FRAMEEYE_TEST_FAIL"] = "dotnet"
        self.run_script("build.ps1", ok=False)
        self.assertEqual([c["name"] for c in self.calls()], ["dotnet"])
        self.assertFalse((self.repo / "dist").exists())

    def test_install_upload_options_and_cleanup(self):
        self.mock_commands()
        # Even a CRLF checkout must launch correctly on Linux after upload.
        launcher = self.repo / "headset/frameeye_overlay.sh"
        launcher.write_bytes(launcher.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        (self.repo / "headset/frameeye.conf.json").write_text("do not upload")
        identity = self.base / "key with spaces"
        identity.write_text("fixture")
        self.install("-SshPort", "2222", "-IdentityFile", identity, "-NoLaunch")
        calls = self.calls()
        self.assertEqual([c["name"] for c in calls], ["scp", "ssh", "ssh"])
        self.assertEqual(calls[0]["files"], sorted(self.headset_files))
        self.assertTrue(calls[0]["shell_lf"])
        self.assertEqual(calls[0]["args"][:4], ["-P", "2222", "-i", str(identity)])
        self.assertEqual(calls[1]["args"][:4], ["-p", "2222", "-i", str(identity)])
        # cmd.exe test shims can split the SSH command; join to inspect its content.
        command = " ".join(calls[1]["args"][5:])
        self.assertIn("/install-overlay.sh 192.0.2.10:9022 --no-launch", command)
        self.assertIn("steamos@frame.example", calls[1]["args"])
        stage_name = calls[0]["args"][-1].split(":")[-1]
        self.assertIn("rm -rf -- ~/" + stage_name, " ".join(calls[2]["args"]))
        self.assertFalse(Path(calls[0]["args"][-2]).exists())

    def test_install_whatif_and_invalid_port_never_connect(self):
        self.mock_commands()
        self.install("-WhatIf")
        self.run_script("headset/install-overlay.ps1", "-HeadsetHost", "frame.example",
                        "-PcAddress", "192.0.2.10:65536", ok=False)
        self.assertEqual(self.calls(), [])

    def test_upload_failure_does_not_run_remote_installer(self):
        self.mock_commands()
        self.env["FRAMEEYE_TEST_FAIL"] = "scp"
        self.install(ok=False)
        self.assertEqual([c["name"] for c in self.calls()], ["scp"])
        self.assertFalse(Path(self.calls()[0]["args"][-2]).exists())

    def test_remote_failure_keeps_remote_upload_for_diagnosis(self):
        self.mock_commands()
        self.env["FRAMEEYE_TEST_FAIL"] = "ssh"
        self.install(ok=False)
        self.assertEqual([c["name"] for c in self.calls()], ["scp", "ssh"])


if __name__ == "__main__":
    unittest.main()
