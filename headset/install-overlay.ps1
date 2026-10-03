#Requires -Version 5.1
<#
.SYNOPSIS
Installs FrameEye Bridge on a Steam Frame from a Windows PC using OpenSSH.
.DESCRIPTION
SSH must be enabled and reachable on the headset. Uses its normal user account;
no root access, WSL, or local Bash installation is needed. Authentication and host
key verification use the normal OpenSSH prompts and configuration.
.EXAMPLE
.\headset\install-overlay.ps1 -HeadsetHost 192.168.1.50 -PcAddress 192.168.1.10
.EXAMPLE
.\headset\install-overlay.ps1 -HeadsetHost frame -PcAddress 192.168.1.10:9021 -NoLaunch
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9.-]*$')]
    [string]$HeadsetHost,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9.-]*(?::[0-9]{1,5})?$')]
    [string]$PcAddress,
    [ValidatePattern('^[A-Za-z_][A-Za-z0-9_.-]*$')]
    [string]$User = 'steamos',
    [ValidateRange(1, 65535)][int]$SshPort = 22,
    [string]$IdentityFile,
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'
if ($PcAddress.Contains(':')) {
    $udpPort = [int]($PcAddress.Split(':')[-1])
    if ($udpPort -lt 1 -or $udpPort -gt 65535) { throw 'PC UDP port must be between 1 and 65535.' }
}
else { $PcAddress += ':9021' }

$ssh = Get-Command ssh -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
$scp = Get-Command scp -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $ssh -or -not $scp) { throw 'Install the Windows OpenSSH Client optional feature (ssh.exe and scp.exe).' }
$sshArgs = @('-p', "$SshPort")
$scpArgs = @('-P', "$SshPort")
if ($IdentityFile) {
    $identity = (Resolve-Path -LiteralPath $IdentityFile).Path
    $sshArgs += @('-i', $identity)
    $scpArgs += @('-i', $identity)
}
$remote = "$User@$HeadsetHost"
$files = @(Get-Content -LiteralPath (Join-Path $PSScriptRoot 'release-files.txt') | Where-Object { $_.Trim() })
foreach ($file in $files) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $file) -PathType Leaf)) {
        throw "Headset release file not found: $file"
    }
}
if (-not $PSCmdlet.ShouldProcess($remote, "Install FrameEye Bridge sending to $PcAddress")) { return }

# Copy only release files, never local configuration, logs, or an entire checkout.
# The generated remote name contains no shell metacharacters and is relative to the SSH user's home.
$stageName = 'frameeye-upload-' + [Guid]::NewGuid().ToString('N')
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$stage = Join-Path $tempRoot $stageName
[IO.Directory]::CreateDirectory($stage) | Out-Null
try {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    foreach ($file in $files) {
        $source = Join-Path $PSScriptRoot $file
        $dest = Join-Path $stage $file
        [IO.Directory]::CreateDirectory((Split-Path -Parent $dest)) | Out-Null
        if ([IO.Path]::GetExtension($file) -eq '.sh') {
            # Also works with ZIP downloads or checkouts that acquired CRLF line endings.
            [IO.File]::WriteAllText($dest, ([IO.File]::ReadAllText($source).Replace("`r`n", "`n")), $utf8)
        }
        else { Copy-Item -LiteralPath $source -Destination $dest }
    }
    & $scp.Source @scpArgs -r $stage "${remote}:$stageName"
    if ($LASTEXITCODE -ne 0) { throw "Upload failed (scp exit code $LASTEXITCODE). The headset installer was not run." }

    $installerArgs = @($PcAddress)
    if ($NoLaunch) { $installerArgs += '--no-launch' }
    # All interpolated values are validated above or generated here; SSH executes this on Linux.
    $command = 'bash ~/' + $stageName + '/install-overlay.sh ' + ($installerArgs -join ' ')
    & $ssh.Source @sshArgs $remote $command
    if ($LASTEXITCODE -ne 0) {
        throw "Headset installation failed (ssh exit code $LASTEXITCODE). Uploaded files remain at ~/$stageName."
    }
    # Keep staging on failure for diagnosis; remove only our exact generated upload on success.
    & $ssh.Source @sshArgs $remote ("rm -rf -- ~/" + $stageName)
    if ($LASTEXITCODE -ne 0) { Write-Warning "Installed, but could not remove headset staging folder ~/$stageName." }
    Write-Host "Installed on $remote. Allow inbound UDP $($PcAddress.Split(':')[-1]) for VRCFaceTracking on this PC."
}
finally {
    $resolvedStage = [IO.Path]::GetFullPath($stage)
    if ((Split-Path -Parent $resolvedStage).TrimEnd('\', '/') -eq $tempRoot.TrimEnd('\', '/') -and
        (Split-Path -Leaf $resolvedStage) -eq $stageName) {
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force
    }
}
