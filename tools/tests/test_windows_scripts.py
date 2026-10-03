"""Integration checks for native PowerShell packaging and SSH command routing.

Run on Windows: python -m unittest discover -s tools/tests -v
Set FRAMEEYE_POWERSHELL=pwsh to also exercise PowerShell 7. SSH is replaced with
local command recorders; these checks never connect to or change a headset.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile


REPO = Path(__file__).resolve().parents[2]
SHELL = shutil.which(os.environ.get("FRAMEEYE_POWERSHELL", "powershell"))


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
        self.headset_files = (REPO / "headset/release-files.txt").read_text().splitlines()
        for name in self.headset_files:
            target = self.repo / "headset" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / "headset" / name, target)
        self.dll = self.repo / "bin/Release/net10.0/SteamFrameEyeModule.dll"
        self.dll.parent.mkdir(parents=True)
        self.dll.write_bytes(b"package fixture; runtime is built separately")
        self.env = dict(os.environ)

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
        return self.run_script("packaging/package.ps1", "-Version", "1.2.3-test.4", *args, **kwargs)

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
sys.exit(23 if os.environ.get("FRAMEEYE_TEST_FAIL") == name else 0)
''', encoding="utf-8")
        for name in ("ssh", "scp", "tar", "dotnet"):
            (mocks / (name + ".cmd")).write_text(
                '@echo off\n"' + sys.executable + '" "' + str(recorder) + '" ' + name + ' %*\n',
                encoding="utf-8",
            )

    def calls(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def test_local_archive_layout_and_no_personal_files(self):
        for name in ("frameeye.conf.json", "frameeye.conf.json.tmp", "frameeye.vrmanifest",
                     "frameeye_overlay.log", "frameeye_overlay.log.old", "frameeye_overlay.lock"):
            (self.repo / "headset" / name).write_text("private fixture")
        self.package()
        output = self.repo / "dist"
        with zipfile.ZipFile(output / "SteamFrameEyeModule-1.2.3-test.4.zip") as z:
            self.assertEqual(set(z.namelist()), {"module.json", "SteamFrameEyeModule.dll", "README.md", "LICENSE"})
            self.assertEqual(z.read("SteamFrameEyeModule.dll"), self.dll.read_bytes())
            manifest = json.loads(z.read("module.json"))
            self.assertTrue(manifest["IsLocal"])
            self.assertEqual(manifest["DownloadUrl"], "")
            self.assertEqual(manifest["ModuleId"], "58be7fa6-4102-44ab-adba-25021d3af8b7")
            self.assertEqual(manifest["Version"], "1.2.3-test.4")
            self.assertEqual(json.loads((output / "module.json").read_bytes()), manifest)
        with tarfile.open(output / "FrameEye-Bridge-1.2.3-test.4.tar.gz") as t:
            self.assertEqual(set(t.getnames()), set(self.headset_files))
            self.assertTrue(t.extractfile("install-overlay.ps1").read().startswith(b"#Requires"))

    def test_release_urls_and_absolute_paths(self):
        output = self.base / "release output"
        custom_dll = self.base / "custom module.dll"
        custom_dll.write_bytes(b"different build")
        url = "https://example.com/releases/v1.2.3/"
        page = "https://example.com/project?one=1&two=2"
        self.package("-DownloadUrlBase", url, "-PageUrl", page,
                     "-DllPath", custom_dll, "-OutDir", output)
        with zipfile.ZipFile(output / "SteamFrameEyeModule-1.2.3-test.4.zip") as z:
            manifest = json.loads(z.read("module.json"))
            self.assertFalse(manifest["IsLocal"])
            self.assertEqual(manifest["DownloadUrl"], url + "SteamFrameEyeModule-1.2.3-test.4.zip")
            self.assertEqual(manifest["ModulePageUrl"], page)
            self.assertEqual(z.read("SteamFrameEyeModule.dll"), b"different build")

    def test_missing_dll_and_tar_failure_do_not_publish(self):
        self.package("-DllPath", "absent.dll", ok=False)
        self.assertFalse((self.repo / "dist").exists())
        self.mock_commands()
        self.env["FRAMEEYE_TEST_FAIL"] = "tar"
        result = self.package(ok=False)
        self.assertIn("tar exit code 23", result.stderr)
        self.assertFalse((self.repo / "dist").exists())

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
