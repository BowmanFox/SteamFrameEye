#Requires -Version 5.1
<#
.SYNOPSIS
Packages the VRCFaceTracking module and Steam Frame app using native Windows tools.
.DESCRIPTION
Run dotnet build -c Release first, or use ..\build.ps1 to build and package together.
Relative DLL and output paths are resolved from the repository root, not the current directory.
Omit DownloadUrlBase for a local package. No Bash, WSL, Python, or external zip tool is required.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidatePattern('^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$')]
    [string]$Version,
    [Parameter(Position = 1)][string]$DownloadUrlBase = '',
    [Parameter(Position = 2)][string]$PageUrl = '',
    [Parameter(Position = 3)][string]$DllPath = 'bin/Release/net10.0/SteamFrameEyeModule.dll',
    [Parameter(Position = 4)][string]$OutDir = 'dist'
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
function Resolve-RepoPath([string]$Path) {
    if (-not [IO.Path]::IsPathRooted($Path)) { $Path = Join-Path $repoRoot $Path }
    [IO.Path]::GetFullPath($Path)
}

$dll = Resolve-RepoPath $DllPath
$output = Resolve-RepoPath $OutDir
if (-not (Test-Path -LiteralPath $dll -PathType Leaf)) {
    throw "Module DLL not found: $dll. Run .\build.ps1 first."
}
$tar = Get-Command tar -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $tar) { throw 'tar.exe is required to package the headset app (included with Windows 10/11).' }
Add-Type -AssemblyName System.IO.Compression.FileSystem

$zipName = "SteamFrameEyeModule-$Version.zip"
$tgzName = "FrameEye-Bridge-$Version.tar.gz"
$utf8 = New-Object System.Text.UTF8Encoding($false)
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'module.json.template') -Raw | ConvertFrom-Json
$manifest.ModuleId = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'module-id.txt') -Raw).Trim()
$manifest.Version = $Version
$manifest.LastUpdated = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss')
$manifest.IsLocal = [string]::IsNullOrWhiteSpace($DownloadUrlBase)
$manifest.DownloadUrl = if ($manifest.IsLocal) { '' } else { $DownloadUrlBase.TrimEnd('/') + '/' + $zipName }
$manifest.ModulePageUrl = $PageUrl

$headset = Join-Path $repoRoot 'headset'
$files = @(Get-Content -LiteralPath (Join-Path $headset 'release-files.txt') | Where-Object { $_.Trim() })
foreach ($file in $files) {
    if (-not (Test-Path -LiteralPath (Join-Path $headset $file) -PathType Leaf)) {
        throw "Headset release file not found: $file"
    }
}

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
    & $tar.Source -czf (Join-Path $stage $tgzName) -C $headset @files
    if ($LASTEXITCODE -ne 0) { throw "Headset packaging failed (tar exit code $LASTEXITCODE)." }

    [IO.Directory]::CreateDirectory($output) | Out-Null
    foreach ($name in @($zipName, $tgzName)) {
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
