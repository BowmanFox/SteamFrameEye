# SPDX-License-Identifier: MIT
#Requires -Version 5.1

<#
.SYNOPSIS
Packages the VRCFaceTracking module and both Steam Frame apps using native Windows tools.
.DESCRIPTION
Run dotnet build -c Release first, or use ..\build.ps1 to build and package together.
Relative DLL and output paths are resolved from the repository root, not the current directory.
Omit DownloadUrlBase for a local package. No Bash, WSL, Python, or external zip tool is required.
Outputs: SteamFrameEyeModule-<version>.zip (the module), FrameEye-Bridge-<version>.tar.gz (the
headset app for the eyes, headset\release-files.txt), FrameFace-<version>.tar.gz (the headset
service for a VIVE Facial Tracker, headset\frameface\release-files.txt) and module.json. They are
published to OutDir together, only after every one of them was built.
The archives are made with Windows' own tar.exe (System32), also when another tar (for example
Git Bash's GNU tar, which reads "C:" as a remote host) comes first on PATH. -TarPath selects
another tar program.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidatePattern('^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$')]
    [string]$Version,
    [Parameter(Position = 1)][string]$DownloadUrlBase = '',
    [Parameter(Position = 2)][string]$PageUrl = '',
    [Parameter(Position = 3)][string]$DllPath = 'bin/Release/net10.0/SteamFrameEyeModule.dll',
    [Parameter(Position = 4)][string]$OutDir = 'dist',
    [string]$TarPath = ''
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
function Resolve-RepoPath([string]$Path) {
    if (-not [IO.Path]::IsPathRooted($Path)) { $Path = Join-Path $repoRoot $Path }
    [IO.Path]::GetFullPath($Path)
}

# Windows' bsdtar first: whatever the calling shell puts on PATH, it understands "C:\..." paths.
function Find-Tar([string]$Requested) {
    if ($Requested) {
        $cmd = Get-Command -Name $Requested -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $cmd) { throw "tar program not found: $Requested" }
        return $cmd.Source
    }
    if ($env:SystemRoot) {
        $systemTar = Join-Path $env:SystemRoot 'System32\tar.exe'
        if (Test-Path -LiteralPath $systemTar -PathType Leaf) { return $systemTar }
    }
    $cmd = Get-Command tar -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    return $null
}

function Get-ReleaseFiles([string]$Folder, [string]$Label) {
    $list = @(Get-Content -LiteralPath (Join-Path $Folder 'release-files.txt') | Where-Object { $_.Trim() })
    foreach ($file in $list) {
        if (-not (Test-Path -LiteralPath (Join-Path $Folder $file) -PathType Leaf)) {
            throw "$Label release file not found: $file"
        }
    }
    , $list
}

$dll = Resolve-RepoPath $DllPath
$output = Resolve-RepoPath $OutDir
if (-not (Test-Path -LiteralPath $dll -PathType Leaf)) {
    throw "Module DLL not found: $dll. Run .\build.ps1 first."
}
$tar = Find-Tar $TarPath
if (-not $tar) { throw 'tar.exe is required to package the headset apps (included with Windows 10/11).' }
Add-Type -AssemblyName System.IO.Compression.FileSystem

$zipName = "SteamFrameEyeModule-$Version.zip"
$tgzName = "FrameEye-Bridge-$Version.tar.gz"
$ffName = "FrameFace-$Version.tar.gz"
$utf8 = New-Object System.Text.UTF8Encoding($false)
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'module.json.template') -Raw | ConvertFrom-Json
$manifest.ModuleId = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'module-id.txt') -Raw).Trim()
$manifest.Version = $Version
$manifest.LastUpdated = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss')
$manifest.IsLocal = [string]::IsNullOrWhiteSpace($DownloadUrlBase)
$manifest.DownloadUrl = if ($manifest.IsLocal) { '' } else { $DownloadUrlBase.TrimEnd('/') + '/' + $zipName }
$manifest.ModulePageUrl = $PageUrl

$headset = Join-Path $repoRoot 'headset'
$files = Get-ReleaseFiles $headset 'Headset'
$frameface = Join-Path $headset 'frameface'
$ffFiles = Get-ReleaseFiles $frameface 'FrameFace'

# Build in an isolated directory first so a failed tar/zip does not publish half a package.
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$stage = Join-Path $tempRoot ('frameeye-package-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($stage) | Out-Null
try {
    $moduleStage = Join-Path $stage 'module'
    [IO.Directory]::CreateDirectory($moduleStage) | Out-Null
    [IO.File]::WriteAllText((Join-Path $moduleStage 'module.json'), ($manifest | ConvertTo-Json -Depth 10), $utf8)
    Copy-Item -LiteralPath $dll -Destination (Join-Path $moduleStage 'SteamFrameEyeModule.dll')
    foreach ($name in @('README.md', 'LICENSE')) {
        Copy-Item -LiteralPath (Join-Path $repoRoot $name) -Destination $moduleStage
    }
    [IO.Compression.ZipFile]::CreateFromDirectory($moduleStage, (Join-Path $stage $zipName))
    & $tar -czf (Join-Path $stage $tgzName) -C $headset @files
    if ($LASTEXITCODE -ne 0) { throw "Headset packaging failed (tar exit code $LASTEXITCODE)." }
    & $tar -czf (Join-Path $stage $ffName) -C $frameface @ffFiles
    if ($LASTEXITCODE -ne 0) { throw "FrameFace packaging failed (tar exit code $LASTEXITCODE)." }
    foreach ($name in @($zipName, $tgzName, $ffName)) {
        if (-not (Test-Path -LiteralPath (Join-Path $stage $name) -PathType Leaf)) {
            throw "Packaging did not produce $name."
        }
    }

    [IO.Directory]::CreateDirectory($output) | Out-Null
    foreach ($name in @($zipName, $tgzName, $ffName)) {
        Copy-Item -LiteralPath (Join-Path $stage $name) -Destination $output -Force
        Write-Host (Join-Path $output $name)
    }
    Copy-Item -LiteralPath (Join-Path $moduleStage 'module.json') -Destination $output -Force
    Write-Host (Join-Path $output 'module.json')
}
finally {
    # Verify the generated absolute path before recursively removing our temporary directory.
    $resolvedStage = [IO.Path]::GetFullPath($stage)
    if ((Split-Path -Parent $resolvedStage).TrimEnd('\', '/') -eq $tempRoot.TrimEnd('\', '/') -and
        (Split-Path -Leaf $resolvedStage) -like 'frameeye-package-*') {
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force
    }
}
