# SPDX-License-Identifier: MIT
#Requires -Version 5.1

<#
.SYNOPSIS
Runs the FrameFace Linux/arm64 checks in the frameface-arm64-test container (spec section 11.1).
.DESCRIPTION
Mounts this repository read-only at /src and runs tools/docker/arm64-checks.sh in a linux/arm64
container (QEMU on an x86-64 PC) with FRAMEFACE_NO_FPS_ASSERT=1. The container has no network
(--network none; the tests need only loopback) and the image is never pulled (--pull never).
Each pattern is one unittest discovery pattern inside tools/tests; without -Pattern every
test_ff_*.py file runs. Exits with the container's exit code. Works in Windows PowerShell 5.1
and PowerShell 7.
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File tools\docker\arm64-checks.ps1 -Pattern test_ff_preprocess.py,test_ff_native.py
.EXAMPLE
pwsh -NoProfile -File tools\docker\arm64-checks.ps1
#>
[CmdletBinding()]
param(
    [string[]]$Pattern = @(),
    [ValidatePattern('^[a-z0-9][a-z0-9._/:-]*$')]
    [string]$Image = 'frameface-arm64-test'
)

$ErrorActionPreference = 'Stop'
# "-Pattern a.py,b.py" arrives as one string when the script runs with -File: split on commas.
$patterns = @($Pattern | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
foreach ($p in $patterns) {
    if ($p -notmatch '^[A-Za-z0-9_.*?\[\]-]+$') { throw "Invalid test file pattern: $p" }
}
$docker = Get-Command docker -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $docker) { throw 'docker was not found on PATH (Docker Desktop with linux/arm64 emulation is needed).' }
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$mount = ($repo -replace '\\', '/') + ':/src:ro'
$dockerArgs = @('run', '--rm', '--pull', 'never', '--network', 'none', '--platform', 'linux/arm64',
    '-e', 'FRAMEFACE_NO_FPS_ASSERT=1', '-v', $mount, $Image,
    'sh', '/src/tools/docker/arm64-checks.sh', '/src') + $patterns

# unittest reports on stderr; keep it a plain stream in every host (no NativeCommandError records).
$ErrorActionPreference = 'Continue'
& $docker.Source @dockerArgs
exit $LASTEXITCODE
