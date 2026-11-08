# SPDX-License-Identifier: MIT
"""Comment-based help of the PowerShell scripts outside the pinned set (K7): `Get-Help <script> -Full` shows
each script's .SYNOPSIS in Windows PowerShell 5.1 and in PowerShell 7.

A `#Requires` line directly before the `<#` help block hides that block from Get-Help (it then shows only the
syntax), so a blank line separates them. The pinned build.ps1 and headset/install-overlay.ps1 keep their 0.5.3
bytes (spec §10.5) and are not checked here. Get-Help only parses the scripts; none of them runs.
"""
import ffutil  # first: installs the port guard

import os
import re
import shutil
import subprocess
import unittest

REPO = ffutil.REPO
SCRIPTS = ("headset/frameface/install-frameface.ps1", "packaging/package.ps1", "tools/docker/arm64-checks.ps1")
SHELLS = [name for name in ("powershell", "pwsh") if shutil.which(name)]


def synopsis(rel):
    """The first line of the script's .SYNOPSIS section."""
    text = (REPO / rel).read_bytes().decode("ascii").replace("\r\n", "\n")
    return re.search(r"(?m)^\.SYNOPSIS\n(.+)$", text).group(1).strip()


class HelpBlock(unittest.TestCase):
    def test_a_blank_line_separates_requires_from_the_help_block(self):
        for rel in SCRIPTS:
            lines = (REPO / rel).read_bytes().decode("ascii").replace("\r\n", "\n").splitlines()
            self.assertEqual(lines[:4], ["# SPDX-License-Identifier: MIT", "#Requires -Version 5.1", "", "<#"], rel)
            self.assertGreater(len(synopsis(rel)), 20, rel)

    @unittest.skipUnless(os.name == "nt" and SHELLS, "needs Windows PowerShell or PowerShell 7 on Windows")
    def test_get_help_shows_the_synopsis(self):
        for shell in SHELLS:
            for rel in SCRIPTS:
                with self.subTest(shell=shell, script=rel):
                    path = str(REPO / rel).replace("'", "''")
                    command = "Get-Help -Full -Name '%s' | Out-String -Width 4096" % path
                    p = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                                        command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
                    out = " ".join(p.stdout.decode("utf-8", "replace").split())
                    self.assertEqual(p.returncode, 0, out)
                    self.assertIn("SYNOPSIS " + synopsis(rel), out)
                    self.assertIn("DESCRIPTION", out)


if __name__ == "__main__":
    unittest.main()
