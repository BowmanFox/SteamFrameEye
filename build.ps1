#Requires -Version 5.1
<#
.SYNOPSIS
Builds and packages FrameEye Bridge from Windows PowerShell or PowerShell 7.
.EXAMPLE
.\build.ps1
.EXAMPLE
.\build.ps1 -VrcftDir 'D:\SteamLibrary\steamapps\common\VRCFaceTracking'
#>
[CmdletBinding()]
param(
    [string]$Version,
    [string]$VrcftDir,
    [string]$OutDir = 'dist',
    [string]$DownloadUrlBase = '',
    [string]$PageUrl = ''
)

$ErrorActionPreference = 'Stop'
if (-not (Get-Command dotnet -CommandType Application -ErrorAction SilentlyContinue)) {
    throw 'Install the .NET 10 SDK, then run this script again.'
}
$project = Join-Path $PSScriptRoot 'SteamFrameEyeModule.csproj'
if (-not $Version) {
    [xml]$projectXml = Get-Content -LiteralPath $project -Raw
    $Version = [string]$projectXml.Project.PropertyGroup.Version
}
$buildArgs = @('build', $project, '-c', 'Release', "-p:Version=$Version")
if ($VrcftDir) {
    $buildArgs += '-p:VrcftDir=' + (Resolve-Path -LiteralPath $VrcftDir).Path
}
& dotnet @buildArgs
if ($LASTEXITCODE -ne 0) { throw "Build failed (dotnet exit code $LASTEXITCODE)." }
& (Join-Path $PSScriptRoot 'packaging/package.ps1') -Version $Version -OutDir $OutDir `
    -DownloadUrlBase $DownloadUrlBase -PageUrl $PageUrl
