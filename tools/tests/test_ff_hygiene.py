# SPDX-License-Identifier: MIT
"""Repository hygiene of the 0.6.0 release (spec §10, §11.3 test_ff_hygiene, §16 task T7).

SPDX headers on every code file outside the pinned FrameEye Bridge set, the clean-room deny-list,
the exact NOTICE.md lines, ffutil imported first by every test_ff_* file, no *.onnx, one version
(0.6.0), the 27 pinned hashes of §10.5, a complete FrameFace release list, the 600-line limit
(.py and .cs), the strings §10.2/§10.3 require in the CI workflows, the README face section,
.gitattributes and .gitignore, and (Linux only) packaging/package.sh in a temporary copy of the
repository. The commands the READMEs, hints and installer messages give the user are checked in
test_ff_hygiene_commands.
"""
import ffutil  # first: installs the port guard

import ast
import hashlib
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
import xml.etree.ElementTree as ET
import zipfile

ffutil.setup_path()
import frameface  # noqa: E402

REPO = ffutil.REPO
FF = ffutil.FRAMEFACE_DIR
VERSION = "0.6.0"
CODE_SUFFIXES = (".py", ".c", ".cs", ".ps1", ".sh", ".service", ".rules")
TEXT_SUFFIXES = CODE_SUFFIXES + (".h", ".md", ".txt", ".json", ".yml", ".yaml", ".csproj", ".template")
SKIP_NAMES = {".git", ".vs", "bin", "obj", "__pycache__"}             # build output and tool state, anywhere
SKIP_PATHS = {"dist", "lib", "parity", "headset/frameface/lib"}     # releases, vendored DLLs, CI vectors, helpers

# §10.5: SHA-256 after replacing CRLF with LF (icon.png raw). These files keep their 0.5.3 bytes.
PINS = dict(line.split()[::-1] for line in """
c4f8a8823871944da8c611670ade94d466b3f943613238f585bd833938a7e30c headset/LICENSE
dfca005c31056489e4fa8135d40112311880bac1f9937654ef3a50ef405b54cd headset/README.md
6f539ab05e3ce6148614b9898929f8f19cdb240338e0bb9761c526eb18618ec8 headset/bitmap_font.py
3e62e42c2f7e3029010619694d9a30bd110858b287bf423436ee2ecf02c35931 headset/eye_smoothing.py
6bd89a8f5345e4f494efac765cb0a1e534ecd6a203f0bba4e37de2f1928c8d93 headset/frameeye-osc.service
3acaaa4b29ea5c800a5a17dd1761769cc12804aec8ce1e80a1917ed1fe2bb337 headset/frameeye_osc.py
d269104bc34835fc22b75e0ba3a5a3490116b8f007eaa5cb1655e01251b9a4dd headset/frameeye_overlay.py
ed892a09cc136875a0ec576fc3284b520ec2632a1d7b2a65ca91ada8d2d62901 headset/frameeye_overlay.sh
1ec0e44c9d0bd2594b5041a525091b003009a48520978beb452ced9a23802c4a headset/gl_texture.py
78bc63d6c1c97df1a1e1985e7245f323ed95072420408d123f859ac235edee69 headset/icon.png
555606c82ac3ce3be556f0a7599b10f188a713d0f348136eea6b1c85c6229b4f headset/install-overlay.sh
49eaefb49ee006e4e721c28392c8ffc99aeeca5423a4b64e05fd79cadc721829 headset/install-overlay.ps1
b432a358055f25e170eac60127a75990d9eb9d7d781a6654669a8cc1571f92da headset/openvr_min.py
96358d75efc70d2913b41d2e094fa1a15d4071322254f7d68b14a74950436634 headset/release-files.txt
82cd228ae1632cf8fed2cd488e3bf952512371a203daccb6e55d725363610be4 headset/tools/gen_icon.py
518a19cc23d2bc64015e166c5d6469810ccc2be6fe14071628b2b2f6a794fc3f headset/tools/record_eyes.py
da7251b0e59323b3dfde9beb7b45a0e6a4a95f86a556e03138651b618f5f02b0 headset/.gitignore
650485e26f44f7daa99885a63e4d83fc9bee59be185ec2af34714f46193281b8 EyeProcessing.cs
ec1d9c0d934875536f00027f33343b8dce238ecf15c1ab9ea7c58cdffc5be414 build.ps1
a119646dce8c6964e663ce73d114928a0edf586e00710cbf99c3eda79c8ee882 packaging/module-id.txt
7a78d233a996ad55685fa6d7d8e9e59cc8d3bfefca67a409914a025c15985ee6 tools/send_test_packets.py
c5aa5bd5d805d176935113ddc58ba3fed37cac6e7d17971d0b8de8e7633a1f3b tools/SmoothingChecks/Program.cs
6105afeb8fa9508935c1edde06901018600f830474bbf6d5ea3c01a6349718c2 tools/SmoothingChecks/SmoothingChecks.csproj
9f5f99598ce6c379afaa5f9deafcfa742222e4ccdbf1339f1df7d08aa5b3174e tools/ReplayProbe/Program.cs
a59801c8046a08062faba597c9457cdb2a0c4ce20aa53cc301aee2daf09dfd4e tools/ReplayProbe/ReplayProbe.csproj
23d8097f041886bdad2e191770c0a537994954d24f1413c20223ace561ca7b51 tools/tests/test_osc_smoothing.py
c4f8a8823871944da8c611670ade94d466b3f943613238f585bd833938a7e30c LICENSE
""".split("\n") if line)

# Clean-room deny-list (§16, §11.3), stored split so that this file never contains the identifiers.
DENY = {
    "BITFOXDEN": ("xu|Send", "kCmd|Magic", "apply|EnableRegs", "clean|Shutdown", "vft|frame",
                  "enableVive|FacialTracker"),
    "Baballonia": ("LinuxUsb|Communicator", "VFT|Common"),
    "Babble Frame Runtime": ("BabbleUsb|Source", "Iso|Reader", "Urb|Free", "valid_camera|_setting", "format_from|_key"),
    "Vamprigamer005": ("set_no|_response", "register|_block", "keep_left|_half", "babble|_look"),
    "vft-stream": ("vft_set|_state", "xu|_magic", "xu|_sensor", "xu|_stream", "jpg|_sink"),
    "Babble-Bridge": ("Etvr|Parser",),
}
DENIED = [(origin, part.replace("|", "")) for origin, parts in DENY.items() for part in parts]
DENY_RE = re.compile(r"\b(%s)\b" % "|".join(re.escape(name) for _, name in DENIED), re.ASCII)

NOTICE_LINES = ("Copyright (c) 2026 OTT Labs", "Copyright (c) 2024 Plüss Roland",
                "Copyright (c) 2024 DragonDreams GmbH", "Copyright (c) 2026 Contributors to this repository",
                "Copyright DragonDreams GmbH 2024")
PERMISSION = ('Permission is hereby granted, free of charge, to any person obtaining a copy of this software and '
              'associated documentation files (the "Software"), to deal in the Software without restriction, '
              'including without limitation the rights to use, copy, modify, merge, publish, distribute, '
              'sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is '
              'furnished to do so, subject to the following conditions:')
MODULE_TEXT = {  # §10.1
    "ModuleName": "Steam Frame Eye + Face (network)",
    "ModuleDescription": "Per-eye gaze and eyelid openness from the Valve Steam Frame's eye tracking (FrameEye "
                         "Bridge, UDP 9021), plus mouth and face expressions from Baballonia (OSC on 127.0.0.1:8888) "
                         "for a VIVE Facial Tracker on the Frame (FrameFace).",
    "UsageInstructions": "Eyes: install FrameEye Bridge on the headset. Face: install FrameFace "
                         "(headset/frameface/install-frameface.ps1), point Baballonia's Face Camera Address at the "
                         "printed URL as Wireless/IP Camera, and do not install VRCFT-Babble. See "
                         "github.com/BowmanFox/SteamFrameEye.",
}
LF_SUFFIXES = (".sh", ".py", ".c", ".service", ".rules", ".txt")     # FrameFace files that run on Linux


def repo_files(suffixes):
    """Relative POSIX paths of the repository's files with these suffixes (no build output, vendored DLLs)."""
    found = []
    for root, dirs, files in os.walk(REPO):
        rel = Path(root).relative_to(REPO).as_posix()
        prefix = "" if rel == "." else rel + "/"
        dirs[:] = sorted(d for d in dirs if d not in SKIP_NAMES and prefix + d not in SKIP_PATHS)
        found.extend(prefix + name for name in files if name.lower().endswith(suffixes))
    return sorted(found)


def read_text(rel):
    """A repository file as text with LF line ends (a Windows checkout may have CRLF)."""
    return (REPO / rel).read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")


def release_list(folder):
    return (folder / "release-files.txt").read_text(encoding="ascii").splitlines()


def denied_in(text):
    return sorted(set(DENY_RE.findall(text)))


def first_foreign_import(tree):
    """(name, node) of the first import, in source order, of a module outside the standard library."""
    stdlib = set(sys.stdlib_module_names)
    nodes = sorted((n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))),
                   key=lambda n: (n.lineno, n.col_offset))
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            names = [node.module or ""] if node.level == 0 else ["." * node.level + (node.module or "")]
        else:
            names = [alias.name for alias in node.names]
        for name in names:
            if name.split(".")[0] not in stdlib:
                return name, node
    return None, None


class SourceHygiene(unittest.TestCase):
    def test_spdx_header_on_every_code_file_outside_the_pinned_set(self):
        checked = [rel for rel in repo_files(CODE_SUFFIXES) if rel not in PINS]
        for rel in ("headset/frameface/install.sh", "headset/frameface/frameface.service", "FaceOsc.cs",
                    "headset/frameface/70-vive-facial-tracker.rules", "headset/frameface/csrc/ffproc.c",
                    "headset/frameface/install-frameface.ps1", "tools/tests/ffutil.py", "tools/docker/arm64-checks.sh",
                    "packaging/package.ps1", "packaging/package.sh", "tools/tests/test_ff_hygiene.py"):
            self.assertIn(rel, checked)
        self.assertGreater(len(checked), 80)
        missing = [rel for rel in checked
                   if not any("SPDX-License-Identifier: MIT" in line for line in read_text(rel).splitlines()[:5])]
        self.assertEqual(missing, [], "no SPDX MIT header in the first 5 lines")

    def test_deny_list_identifiers_appear_nowhere(self):
        self.assertEqual(len(DENIED), 23)
        # The matcher itself: whole words only, case-sensitive.
        name = DENIED[0][1]
        self.assertEqual(denied_in("x = %s(1)" % name), [name])
        self.assertEqual(denied_in("my_%s_2 %s2 %s" % (name, name, name.upper())), [])
        scanned = repo_files(TEXT_SUFFIXES)
        self.assertIn("tools/tests/test_ff_hygiene.py", scanned)
        self.assertGreater(len(scanned), 100)
        hits = {rel: denied_in(read_text(rel)) for rel in scanned}
        self.assertEqual({rel: names for rel, names in hits.items() if names}, {})

    def test_notice_has_the_exact_copyright_lines_and_mit_notices(self):
        notice = (FF / "NOTICE.md").read_text(encoding="utf-8")
        lines = [line.rstrip() for line in notice.splitlines()]
        for line in NOTICE_LINES:
            self.assertIn(line, lines)
        flat = " ".join(notice.split())
        self.assertGreaterEqual(flat.count(PERMISSION), 4)       # ours, vivefacialtracker, Liptracker, vivetracker.py
        self.assertGreaterEqual(flat.count('THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND'), 4)
        for text in ("vivetracker.py", "5bc0700", "faceModel.onnx"):
            self.assertIn(text, notice)

    def test_every_test_ff_file_imports_ffutil_first(self):
        files = sorted(p for p in (REPO / "tools" / "tests").glob("test_ff_*.py"))
        self.assertGreater(len(files), 20)
        for path in files:
            tree = ast.parse(path.read_bytes(), filename=str(path))
            name, node = first_foreign_import(tree)
            self.assertEqual(name, "ffutil", "%s: first non-stdlib import" % path.name)
            self.assertIn(node, tree.body, "%s: ffutil must be imported at module level" % path.name)
        tree = ast.parse("import os\nimport frameface\nimport ffutil\n")
        self.assertEqual(first_foreign_import(tree)[0], "frameface")      # the rule catches a late ffutil

    def test_no_model_files(self):
        found = []
        for root, dirs, files in os.walk(REPO):
            dirs[:] = [d for d in dirs if d != ".git"]
            found.extend(os.path.join(root, f) for f in files if f.lower().endswith(".onnx"))
        self.assertEqual(found, [])
        for folder in (REPO / "headset", FF):
            self.assertFalse(any(n.lower().endswith(".onnx") for n in release_list(folder)))

    def test_one_version_everywhere(self):
        self.assertEqual(frameface.__version__, VERSION)
        versions = [e.text for e in ET.parse(str(REPO / "SteamFrameEyeModule.csproj")).getroot().iter("Version")]
        self.assertEqual(versions, [VERSION])
        template = json.loads((REPO / "packaging" / "module.json.template").read_text(encoding="utf-8"))
        self.assertEqual(template["Version"], "__VERSION__")            # filled in from the build's version
        readme = read_text("README.md")
        quick = readme[readme.index("\n## Quick start\n"):readme.index("\n### Windows scripts\n")]
        for name in ("SteamFrameEyeModule-%s.zip", "FrameEye-Bridge-%s.tar.gz", "FrameFace-%s.tar.gz"):
            self.assertIn(name % VERSION, quick)
        archives = re.findall(r"(?:SteamFrameEyeModule|FrameEye-Bridge|FrameFace)-(\d+\.\d+\.\d+)", quick)
        self.assertEqual(set(archives), {VERSION})
        self.assertIn("SteamFrameEyeModule %s" % VERSION, read_text("headset/frameface/README.md"))

    def test_pinned_files_keep_their_bytes(self):
        self.assertEqual(len(PINS), 27)
        for rel, digest in sorted(PINS.items()):
            data = (REPO / rel).read_bytes()
            if rel != "headset/icon.png":
                data = data.replace(b"\r\n", b"\n")
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest, rel)

    def test_frameface_release_list_is_complete_and_exists(self):
        raw = (FF / "release-files.txt").read_bytes()
        self.assertNotIn(b"\r", raw)
        names = release_list(FF)
        self.assertEqual(names, sorted(set(names)))
        package = {p.relative_to(FF).as_posix() for p in (FF / "frameface").rglob("*.py") if "__pycache__" not in p.parts}
        self.assertGreater(len(package), 30)
        self.assertEqual(sorted(package - set(names)), [], "package modules missing from release-files.txt")
        for name in ("frameface_main.py", "install.sh", "install-frameface.ps1", "frameface.service", "LICENSE",
                     "NOTICE.md", "README.md", "70-vive-facial-tracker.rules", "csrc/ffproc.c", "csrc/abi_dump.c",
                     "release-files.txt"):
            self.assertIn(name, names)
        for name in names:
            self.assertTrue((FF / name).is_file(), "listed but missing: " + name)
            self.assertFalse(name.endswith(".so") or "__pycache__" in name or name.startswith("lib/"), name)
            if name.endswith(LF_SUFFIXES):
                self.assertNotIn(b"\r", (FF / name).read_bytes(), name + " must have LF line endings")

    def test_python_files_stay_within_600_lines(self):
        checked = [rel for rel in repo_files((".py",)) if rel not in PINS]
        headset = [rel for rel in checked if rel.startswith("headset/frameface/")]
        self.assertGreater(len(headset), 30)
        self.assertIn("tools/tests/test_ff_hygiene.py", checked)
        too_long = {rel: n for rel in checked for n in [len(read_text(rel).splitlines())] if n > 600}
        self.assertEqual(too_long, {})

    def test_csharp_files_stay_within_600_lines(self):
        checked = [rel for rel in repo_files((".cs",)) if rel not in PINS]
        self.assertIn("tools/FaceChecks/Program.cs", checked)
        self.assertNotIn("EyeProcessing.cs", checked)        # pinned: the 0.5.3 bytes, 947 lines
        too_long = {rel: n for rel in checked for n in [len(read_text(rel).splitlines())] if n > 600}
        self.assertEqual(too_long, {})


class Workflows(unittest.TestCase):
    @staticmethod
    def header(text):
        """The leading comment block, without '#' and line breaks."""
        lines = []
        for line in text.splitlines():
            if not line.startswith("#"):
                break
            lines.append(line.lstrip("#").strip())
        return " ".join(" ".join(lines).split())

    def common(self, text, version_expr):
        for line in text.splitlines():
            if "apt-get install" in line:
                self.assertIn("apt-get update", line.split("apt-get install")[0], line)
        self.assertIn("FrameFace-X.Y.Z.tar.gz", self.header(text))
        self.assertIn("dist/FrameFace-${{ %s }}.tar.gz" % version_expr, text)
        flat = " ".join(text.split())
        for phrase in ("frameface-upload-manual", "(never into `~/frameface`)", "bash ~/frameface-upload-manual/install.sh",
                       "headset/frameface/install-frameface.ps1", "exit VRCFaceTracking first", "Baballonia",
                       "Do not install VRCFT-Babble with this module", "VRCFaceTracking 5.4.x and 6.0",
                       "CustomLibs\\58be7fa6-4102-44ab-adba-25021d3af8b7\\"):
            self.assertIn(phrase, flat)

    def test_github_build_workflow(self):
        text = read_text(".github/workflows/build.yml")
        self.common(text, "steps.ver.outputs.version")
        self.assertIn("the module and both headset apps (FrameEye Bridge for the eyes, FrameFace for a VIVE Facial "
                      "Tracker)", self.header(text))
        self.assertRegex(text, r"(?m)^\s+name: SteamFrameEye \$\{\{ steps\.ver\.outputs\.version \}\} "
                               r"\(module, FrameEye Bridge, FrameFace\)$")
        for needle in ("python: ['3.x']", "- powershell: pwsh\n            python: '3.10'",
                       "python-version: ${{ matrix.python }}", "-python${{ matrix.python }}",
                       "dotnet run --project tools/FaceChecks -c Release", "headset-arm64:", "runs-on: ubuntu-24.04-arm",
                       "sudo apt-get update -qq && sudo apt-get install -y --no-install-recommends gcc libturbojpeg0-dev",
                       "for f in headset/install-overlay.sh headset/frameeye_overlay.sh headset/frameface/install.sh "
                       'tools/docker/arm64-checks.sh; do\n            bash -n "$f"\n          done',
                       'sh tools/docker/arm64-checks.sh "$PWD"', "alpine:3.20",
                       "apk add --no-cache python3 gcc musl-dev linux-headers libjpeg-turbo-dev bash",
                       "opencv-parity:", "python tools/parity/gen_vectors.py --out parity",
                       "dotnet run --project tools/OpenCvParity -c Release -- parity", "FRAMEFACE_NO_FPS_ASSERT"):
            self.assertIn(needle, text)
        # bash -n reads only its first file (the others become $1, $2, ...), so never several files per call.
        self.assertNotRegex(text, r"bash -n +\S+\.sh +\S+\.sh")
        self.assertEqual(text.count("dotnet run --project tools/FaceChecks -c Release"), 2)    # windows and ubuntu
        self.assertNotRegex(text, r"needs:.*(headset-arm64|opencv-parity)")
        # The optional ModuleHarness step: continue-on-error, an empty temp config folder, two allowed ports.
        step = text[text.rindex("- name:", 0, text.index("tools/ModuleHarness -c Release")):]
        step = step[:step.index("- name:", 1)]
        self.assertIn("continue-on-error: true", step)
        self.assertIn("Always ends in SKIP (77) without the VRCFT core's dependencies", " ".join(step.replace("#", "").split()))
        self.assertIn("SFEYE_CONFIG_DIR: ${{ runner.temp }}/", step)
        ports = [int(re.search(r'%s: "(\d+)"' % key, step).group(1)) for key in ("SFEYE_PORT", "SFFACE_PORT")]
        self.assertEqual(len(set(ports)), 2)
        for port in ports:
            self.assertTrue(1024 <= port <= 65535 and port not in ffutil.FORBIDDEN_TEST_PORTS and port != 9023, port)
        self.assertIn("continue-on-error: true", text[text.index("  opencv-parity:"):])
        # tools/ModuleChecks runs SteamFrameEyeModule.cs itself and gates every windows leg and the ubuntu build,
        # after FaceChecks; on CI the eye rows that bind 0.0.0.0 run too.
        checks = ("dotnet run --project tools/ModuleChecks/ModuleChecks.csproj -c Release "
                  "-p:VrcftDir=${{ github.workspace }}/lib/vrcft-5.4.5")
        self.assertEqual(text.count(checks), 2)
        for job in (text[text.index("\n  windows:"):text.index("\n  build:")],
                    text[text.index("\n  build:"):text.index("\n  headset-arm64:")]):
            self.assertLess(job.index("dotnet run --project tools/FaceChecks -c Release"), job.index(checks))
            step = job[job.rindex("- name:", 0, job.index(checks)):]
            step = step[:step.index("- name:", 1)]
            self.assertNotIn("continue-on-error", step)
            self.assertIn('MODULECHECKS_ALLOW_WILDCARD: "1"', step)
        for project, args in re.findall(r"dotnet run --project (\S+)(.*)", text):   # it exists and uses a -p:VrcftDir it gets
            self.assertTrue((REPO / project).exists() and ("-p:VrcftDir=" not in args or "$(VrcftDir)" in read_text(project)), project)
        # Every hosted test run reports frame rates and timings instead of asserting them.
        for marker in ("python -m unittest discover -s tools/tests", "python3 -m unittest discover -s tools/tests"):
            end = text.index(marker)
            self.assertIn('FRAMEFACE_NO_FPS_ASSERT: "1"', text[text.rindex("- name:", 0, end):end], marker)
        arm64 = text[text.index("\n  headset-arm64:"):text.index("\n  opencv-parity:")]
        self.assertIn('FRAMEFACE_NO_FPS_ASSERT: "1"', arm64)
        self.assertIn("docker run --rm -e FRAMEFACE_NO_FPS_ASSERT=1", arm64)

    def test_forgejo_release_workflow(self):
        text = read_text(".forgejo/workflows/release.yml")
        self.common(text, "steps.tag.outputs.version")
        self.assertRegex(text, r"(?m)^\s+name: SteamFrameEye \$\{\{ steps\.tag\.outputs\.version \}\}$")


class Docs(unittest.TestCase):
    def test_readme_face_section(self):
        readme = read_text("README.md")
        heading = "\n## Face tracking with a VIVE Facial Tracker (FrameFace)\n"
        self.assertEqual(readme.count(heading), 1)
        start = readme.index(heading)
        section = readme[start:readme.index("\n## ", start + len(heading))]
        for sub in ("\n### Baballonia setup\n", "\n### Troubleshooting the face path\n"):
            self.assertEqual(section.count(sub), 1, sub)
        flat = " ".join(section.split())
        for phrase in (
                "VRCFaceTracking closed", "Install local module", "once more", "Expand-Archive", "frameface_main.py sim",
                "SteamFrameEye: claims eye=True expression=True (eye UDP 9021, face 127.0.0.1:8888, "
                "face_source=babble_osc)", '"Steam Frame Eye + Face (network)"', "Wireless/IP Camera",
                "Force Re-Initialize", "Settings -> Risky", "until VRCFaceTracking is restarted", "EyeTrackingActive",
                "LipTrackingActive", "at most once per VRCFaceTracking run", "`Default = true` or `Saved`",
                "status keeper", "does not move its eye or expression claim", "url --new-token", "-CopyUrl",
                "clipboard history", "allow_peers", "unencrypted", "Port 8888 in use", "VRCFT-Babble installed",
                "Charging blocks the tracker", "MIT", "BSDL 1.0", "never bundled", "faceModel.onnx",
                "headset/frameface/NOTICE.md", "-EnableFace", "-Check", "-Rollback", "-Uninstall"):
            self.assertIn(phrase, flat)
        for key in ("eye_enabled", "face_source", "face_port", "face_min_rate_hz", "face_stale_ms", "face_decay_ms",
                    "face_fade_in_ms", "smile_slant", "status_keeper", "status_keeper_port"):
            self.assertIn("| `%s` |" % key, section)
        trouble = section[section.index("\n### Troubleshooting the face path\n"):]
        for display in ("disabled", "no_device", "port_device_mode", "no_permission", "busy", "standby",
                        "waiting_client", "settling", "starting", "streaming", "recovering", "test_pattern",
                        "dark_start", "dark", "brownout", "wedged", "no_ir", "port_in_use", "unsupported", "library"):
            self.assertIn("| `%s` |" % display, trouble)
        layout = readme[readme.index("\n## Repository layout\n"):]
        for path in ("headset/frameface/", "tools/FaceChecks/", "tools/ModuleChecks/", "tools/tests/", "tools/docker/", "build.yml"):
            self.assertIn(path, layout)
        how = readme[readme.index("\n## How it works\n"):readme.index("\n## Quick start\n")]
        self.assertIn("FrameFace", how)
        self.assertIn("version 0.5.3, built against `lib/vrcft-5.4.5`, was verified running on VRCFaceTracking 6.0",
                      " ".join(readme.split()))

    def test_module_json_template(self):
        template = json.loads((REPO / "packaging" / "module.json.template").read_text(encoding="utf-8"))
        for key, text in MODULE_TEXT.items():
            self.assertEqual(template[key], text, key)
        self.assertEqual(template["ModuleId"], "__MODULE_ID__")
        self.assertEqual(template["DllFileName"], "SteamFrameEyeModule.dll")

    def test_gitattributes_and_gitignore_text(self):
        attributes = read_text(".gitattributes").splitlines()
        for line in ("*.c text eol=lf", "*.rules text eol=lf", "headset/frameface/release-files.txt text eol=lf",
                     "*.py text eol=lf", "*.sh text eol=lf", "*.service text eol=lf"):
            self.assertIn(line, attributes)
        ignores = [line.strip() for line in read_text(".gitignore").splitlines()]
        for line in ("headset/frameface/lib/", "*.so", "frameface-probe-*/", "/parity/"):
            self.assertIn(line, ignores)
        # lib/vrcft-5.4.5 is tracked and tools/parity holds sources: never a bare lib/ or parity/ pattern.
        for line in ("lib/", "lib", "/lib/", "parity/", "parity", "*.dll"):
            self.assertNotIn(line, ignores)

    @unittest.skipUnless(shutil.which("git") and (REPO / ".git").exists(), "needs git and a checkout")
    def test_git_ignores_exactly_the_generated_paths(self):
        def ignored(rel):
            run = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", "--no-index", rel],
                                 capture_output=True, timeout=60)
            self.assertIn(run.returncode, (0, 1), run.stderr)
            return run.returncode == 0
        for rel in ("headset/frameface/lib/libffproc.so", "headset/frameface/lib/build.log", "tools/x/libffproc.so",
                    "frameface-probe-20261107-120000/report.json", "parity/manifest.json"):
            self.assertTrue(ignored(rel), rel)
        for rel in ("lib/vrcft-5.4.5/VRCFaceTracking.SDK.dll", "lib/vrcft-5.4.5/NOTICE.md", "tools/parity/gen_vectors.py",
                    "headset/frameface/frameface/probe.py", "tools/tests/test_ff_probe.py",
                    "headset/frameface/release-files.txt", "headset/frameface/csrc/ffproc.c"):
            self.assertFalse(ignored(rel), rel)


ZIP_SHIM = """#!/usr/bin/env python3
# stands in for zip(1) in test_ff_hygiene: zip -q -9 OUT FILE...
import os, sys, zipfile
args = [a for a in sys.argv[1:] if a not in ("-q", "-9")]
with zipfile.ZipFile(args[0], "w", zipfile.ZIP_DEFLATED) as z:
    for name in args[1:]:
        z.write(name, name)
open(os.environ["ZIP_SHIM_MARK"], "w").close()
"""


class PackageSh(unittest.TestCase):
    """packaging/package.sh in a temporary copy of the repository with a dummy DLL (Linux only)."""

    def test_package_sh_builds_the_three_archives(self):
        ffutil.require_native("bash", "tar", "sed", "python3", reason="packaging/package.sh is a Linux script")
        tmp = Path(tempfile.mkdtemp(prefix="ff-hygiene-package-"))
        self.addCleanup(shutil.rmtree, str(tmp), True)
        root = tmp / "repo"
        bridge, ff = release_list(REPO / "headset"), release_list(FF)
        shutil.copytree(str(REPO / "packaging"), str(root / "packaging"))
        for rel in ["README.md", "LICENSE"] + ["headset/" + n for n in bridge] + ["headset/frameface/" + n for n in ff]:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(REPO / rel), str(root / rel))
        for junk in ("lib/libffproc.so", "frameface/__pycache__/cli.cpython-312.pyc", "config.json",
                     "probe-20261107-120000/report.json"):
            (root / "headset/frameface" / junk).parent.mkdir(parents=True, exist_ok=True)
            (root / "headset/frameface" / junk).write_text("must not ship")
        dll = root / "bin/Release/net10.0/SteamFrameEyeModule.dll"
        dll.parent.mkdir(parents=True)
        dll.write_bytes(b"package.sh fixture, not a module")
        shim = tmp / "shim"
        shim.mkdir()
        (shim / "zip").write_text(ZIP_SHIM)
        os.chmod(str(shim / "zip"), 0o755)
        base, page = "https://example.invalid/releases/download/v" + VERSION, "https://example.invalid/project"
        for variant in ("as found", "zip shim"):
            out = tmp / ("out " + variant)                      # absolute, with a space
            env = dict(os.environ, ZIP_SHIM_MARK=str(tmp / "zip-shim-used"))
            if variant == "zip shim":
                env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
            run = subprocess.run(["bash", str(root / "packaging/package.sh"), VERSION, base, page,
                                  "bin/Release/net10.0/SteamFrameEyeModule.dll", str(out)],
                                 cwd=str(root), env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual((tmp / "zip-shim-used").exists(), variant == "zip shim")
            zipname, tgz, fftgz = ("SteamFrameEyeModule-%s.zip" % VERSION, "FrameEye-Bridge-%s.tar.gz" % VERSION,
                                   "FrameFace-%s.tar.gz" % VERSION)
            self.assertEqual(sorted(os.listdir(str(out))), sorted([zipname, tgz, fftgz, "module.json"]))
            with zipfile.ZipFile(str(out / zipname)) as z:
                self.assertEqual(sorted(z.namelist()), ["LICENSE", "README.md", "SteamFrameEyeModule.dll", "module.json"])
                self.assertEqual(z.read("SteamFrameEyeModule.dll"), dll.read_bytes())
                self.assertEqual(z.read("README.md"), (REPO / "README.md").read_bytes())
                manifest = json.loads(z.read("module.json").decode("utf-8"))
            self.assertEqual(json.loads((out / "module.json").read_text(encoding="utf-8")), manifest)
            self.assertEqual(manifest["ModuleId"], (REPO / "packaging/module-id.txt").read_text().strip())
            self.assertEqual((manifest["Version"], manifest["IsLocal"]), (VERSION, False))
            self.assertEqual((manifest["DownloadUrl"], manifest["ModulePageUrl"]), (base + "/" + zipname, page))
            for key, text in MODULE_TEXT.items():
                self.assertEqual(manifest[key], text, key)
            for name, folder, names in ((tgz, REPO / "headset", bridge), (fftgz, FF, ff)):
                with tarfile.open(str(out / name)) as t:
                    self.assertEqual(sorted(m.name for m in t.getmembers()), sorted(names), name)
                    for member in names:
                        self.assertEqual(t.extractfile(member).read(), (folder / member).read_bytes(), member)
            if variant == "zip shim":
                os.remove(str(tmp / "zip-shim-used"))


if __name__ == "__main__":
    unittest.main()
