# SPDX-License-Identifier: MIT
#Requires -Version 5.1

<#
.SYNOPSIS
Installs FrameFace (the VIVE Facial Tracker camera service for Baballonia) on a Steam Frame over
SSH, and probes, checks, rolls back or removes it.
.DESCRIPTION
Install (no mode switch): uploads exactly the files of release-files.txt to
~/frameface-upload-<id> on the headset, runs install.sh there (it builds, selftests and starts the
service as the normal user, without sudo), removes the upload and prints the Baballonia URL. When
the headset also prints its LAN URL, the next steps and -CopyUrl use the one this PC reaches: the
Frame-link URL if this PC has an IPv4 address in 10.35.78.0/24, otherwise the LAN URL. Then
it sets "face_source": "babble_osc" in <VrcftConfigDir>\SteamFrameEyeModule.json if that key is
absent (a backup SteamFrameEyeModule.json.bak-<yyyyMMdd-HHmmss> is made only when it writes);
a deliberate "off" is kept unless -EnableFace is given. After -NoStart the next steps give the
command that starts the service from this PC.
-Probe runs the read-only probe and copies its report to .\frameface-probe-<ts>; -ProbeActive runs
the active probe (IR on, the face service stops for 1-3 minutes; asks first).
-Check is read-only: VRCFaceTracking version, every installed module, face_source, the owner of
UDP 8888, Baballonia, and the state of the headset service.
-Uninstall [-Purge] and -Rollback upload a fresh install.sh and run it with --uninstall [--purge]
or --rollback; -Uninstall also removes face_source from the module settings (with a backup).
-WhatIf never runs ssh or scp and never writes the settings file.
Needs ssh.exe and scp.exe (Windows OpenSSH client). Authentication and host keys use the normal
OpenSSH prompts and configuration. Works in Windows PowerShell 5.1 and PowerShell 7.
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File .\headset\frameface\install-frameface.ps1 -HeadsetHost frame
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File .\headset\frameface\install-frameface.ps1 -HeadsetHost frame -Probe
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File .\headset\frameface\install-frameface.ps1 -HeadsetHost frame -ProbeActive -ProbeArgs '--matrix --soak 300'
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File .\headset\frameface\install-frameface.ps1 -HeadsetHost frame -Check
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File .\headset\frameface\install-frameface.ps1 -HeadsetHost frame -Uninstall -Purge
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9.-]*$')]
    [string]$HeadsetHost,
    [ValidatePattern('^[A-Za-z_][A-Za-z0-9_.-]*$')]
    [string]$User = 'steamos',
    [ValidateRange(1, 65535)]
    [int]$SshPort = 22,
    [string]$IdentityFile,
    [switch]$NoStart,
    [switch]$NewToken,
    # One or more IPv4 CIDRs, comma-separated (e.g. 192.168.4.31/32); each is checked before any SSH.
    [string[]]$AllowPeer = @(),
    [switch]$SkipModuleSettings,
    [switch]$EnableFace,
    [switch]$CopyUrl,
    [string]$VrcftConfigDir = $(if ($env:APPDATA) { Join-Path $env:APPDATA 'VRCFaceTracking' } else { '' }),
    [string]$VrcftInstallDir = 'C:\Program Files (x86)\Steam\steamapps\common\VRCFaceTracking',
    [switch]$Probe,
    [switch]$ProbeActive,
    [ValidatePattern('^[A-Za-z0-9 ._=-]*$')]
    [string]$ProbeArgs = '',
    [switch]$IncludeImages,
    [switch]$Check,
    [switch]$Uninstall,
    [switch]$Purge,
    [switch]$Rollback
)

$ErrorActionPreference = 'Stop'
$ModuleId = '58be7fa6-4102-44ab-adba-25021d3af8b7'
$SettingsName = 'SteamFrameEyeModule.json'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
# Text files staged with LF line ends (they run or are read on Linux).
$LfExtensions = @('.sh', '.py', '.c', '.service', '.rules', '.txt', '.md')
$KnownConflicts = @(
    @{ Id = '360b014b-b57b-450f-8f12-9904618ff370'; Pattern = 'Babble'
       Reason = 'VRCFT-Babble claims expressions and binds UDP 8888, the port Baballonia sends to; uninstall it (this module replaces it)' },
    @{ Id = '2a8c8080-2a76-46af-bf76-1da7c0127ef8'; Pattern = 'Steam ?Link|LinkFT|\bMeta'
       Reason = 'Steam Link / Meta face tracking claims eyes and expressions first; disable or uninstall it' },
    @{ Id = '74f18332-1bcf-4b55-832d-9c91069afa31'; Pattern = 'SRanipal'
       Reason = 'SRanipal claims eyes and lips first; disable or uninstall it' },
    @{ Id = ''; Pattern = 'SteamFrameTracking'
       Reason = 'the official Steam Frame eye module claims the eyes first; disable or uninstall it' },
    @{ Id = ''; Pattern = 'ViveStreaming'
       Reason = 'VIVE Streaming claims eyes and expressions first; disable or uninstall it' }
)
$script:NativeExit = 0
$script:Fails = 0

function Write-Tag([string]$Tag, [string]$Text) {
    if ($Tag -eq 'fail') { $script:Fails++ }
    Write-Host ('[{0}] {1}' -f $Tag, $Text)
}

function Invoke-Native([string]$Exe, [string[]]$ArgList) {
    # Call as a statement: the program writes to the console (a TTY for ssh -t); the exit code
    # goes to $script:NativeExit. Its stderr must never become a terminating error.
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Exe @ArgList } finally { $ErrorActionPreference = $saved }
    $script:NativeExit = [int]$LASTEXITCODE
}

function Read-Native([string]$Exe, [string[]]$ArgList) {
    # Returns @{ Code; Lines } with the program's stdout lines (stderr goes to the console).
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $lines = @(& $Exe @ArgList) } finally { $ErrorActionPreference = $saved }
    return @{ Code = [int]$LASTEXITCODE; Lines = $lines }
}

function Get-ReleaseFiles {
    $list = Join-Path $PSScriptRoot 'release-files.txt'
    if (-not (Test-Path -LiteralPath $list -PathType Leaf)) { throw "release-files.txt not found next to install-frameface.ps1 ($PSScriptRoot)." }
    $files = @([IO.File]::ReadAllLines($list) | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    foreach ($file in $files) {
        if ($file -cnotmatch '^[A-Za-z0-9_][A-Za-z0-9._/-]*$' -or $file -match '(^|/)\.\.(/|$)') { throw "Invalid entry in release-files.txt: $file" }
        if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $file) -PathType Leaf)) { throw "FrameFace release file not found: $file" }
    }
    if ($files.Count -eq 0) { throw 'release-files.txt lists no files.' }
    return , $files
}

function New-Stage([string[]]$Files) {
    # %TEMP%\frameface-upload-<guid> with these files; text files LF-normalised.
    $name = 'frameface-upload-' + [Guid]::NewGuid().ToString('N')
    $root = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $dir = Join-Path $root $name
    [IO.Directory]::CreateDirectory($dir) | Out-Null
    try {
        foreach ($file in $Files) {
            $source = Join-Path $PSScriptRoot $file
            $dest = Join-Path $dir ($file -replace '/', '\')
            [IO.Directory]::CreateDirectory((Split-Path -Parent $dest)) | Out-Null
            if ($LfExtensions -contains [IO.Path]::GetExtension($file).ToLowerInvariant() -or $file -eq 'LICENSE') {
                [IO.File]::WriteAllText($dest, [IO.File]::ReadAllText($source, $Utf8NoBom).Replace("`r`n", "`n"), $Utf8NoBom)
            }
            else { Copy-Item -LiteralPath $source -Destination $dest }
        }
    }
    catch {
        # a half-staged folder never stays behind in %TEMP%
        Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }
    return @{ Name = $name; Dir = $dir; Root = $root }
}

function Remove-Stage($Stage) {
    $resolved = [IO.Path]::GetFullPath($Stage.Dir)
    if ((Split-Path -Parent $resolved).TrimEnd('\', '/') -eq $Stage.Root.TrimEnd('\', '/') -and
        (Split-Path -Leaf $resolved) -eq $Stage.Name -and (Test-Path -LiteralPath $resolved)) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

function Read-Settings([string]$Path) {
    # @{ Exists = $false } for a missing file, @{ Exists = $true; Object = <PSCustomObject> }, or
    # $null if the file is not a JSON object.
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @{ Exists = $false; Object = $null } }
    $obj = $null
    try {
        $raw = [IO.File]::ReadAllText($Path)
        if ($raw.Trim()) { $obj = ConvertFrom-Json -InputObject $raw }
    }
    catch { $obj = $null }
    if ($obj -isnot [System.Management.Automation.PSCustomObject]) { return $null }
    return @{ Exists = $true; Object = $obj }
}

function Find-FaceSource($Object) {
    foreach ($property in @($Object.PSObject.Properties)) {
        if ($property.Name -ceq 'face_source') { return $property }
    }
    return $null
}

function Save-Settings([string]$Path, $Object) {
    $dir = Split-Path -Parent $Path
    if ($dir) { [IO.Directory]::CreateDirectory($dir) | Out-Null }
    $json = ConvertTo-Json -InputObject $Object -Depth 64
    $tmp = '{0}.tmp-{1}' -f $Path, [Guid]::NewGuid().ToString('N')
    [IO.File]::WriteAllText($tmp, $json + [Environment]::NewLine, $Utf8NoBom)
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

function Backup-Settings([string]$Path) {
    $base = '{0}.bak-{1}' -f $Path, (Get-Date -Format 'yyyyMMdd-HHmmss')
    $backup = $base
    $n = 1
    while (Test-Path -LiteralPath $backup) { $backup = '{0}-{1}' -f $base, $n; $n++ }
    Copy-Item -LiteralPath $Path -Destination $backup
    return $backup
}

function Set-FaceSource {
    # Writes "face_source": "babble_osc" only if the key is absent (or with -EnableFace).
    $path = Join-Path $VrcftConfigDir $SettingsName
    $settings = Read-Settings $path
    if ($null -eq $settings) {
        Write-Warning ('{0} is not a valid JSON object; it was left untouched. Fix it, or add "face_source": "babble_osc" yourself with VRCFaceTracking closed.' -f $path)
        return
    }
    $property = $null
    if ($settings.Exists) {
        $property = Find-FaceSource $settings.Object
        if ($property) {
            if ($property.Value -is [string] -and $property.Value -ceq 'babble_osc') {
                Write-Tag ok ('face_source is already "babble_osc" in {0}' -f $path)
                return
            }
            if (-not $EnableFace) {
                Write-Tag info ('face_source is "{0}"; run again with -EnableFace to turn the face path on' -f $property.Value)
                return
            }
        }
    }
    if (-not $PSCmdlet.ShouldProcess($path, 'Set "face_source": "babble_osc"')) { return }
    try {
        if ($settings.Exists) {
            $object = $settings.Object
            if ($property) { $property.Value = 'babble_osc' }
            else { Add-Member -InputObject $object -NotePropertyName 'face_source' -NotePropertyValue 'babble_osc' }
            $backup = Backup-Settings $path
            Save-Settings $path $object
            Write-Tag ok ('face_source = "babble_osc" in {0} (backup {1})' -f $path, $backup)
        }
        else {
            Save-Settings $path ([pscustomobject]@{ face_source = 'babble_osc' })
            Write-Tag ok ('created {0} with "face_source": "babble_osc"' -f $path)
        }
    }
    catch { Write-Warning ('Could not write {0}: {1}' -f $path, $_.Exception.Message) }
}

function Remove-FaceSource {
    $path = Join-Path $VrcftConfigDir $SettingsName
    $settings = Read-Settings $path
    if ($null -eq $settings) {
        Write-Warning ('{0} is not a valid JSON object; it was left untouched (face_source not removed).' -f $path)
        return
    }
    if (-not $settings.Exists) { Write-Tag ok ('{0} does not exist: nothing to change' -f $path); return }
    $property = Find-FaceSource $settings.Object
    if (-not $property) { Write-Tag ok ('face_source is not set in {0} (the face path is off)' -f $path); return }
    if (-not $PSCmdlet.ShouldProcess($path, 'Remove "face_source"')) { return }
    try {
        $backup = Backup-Settings $path
        $settings.Object.PSObject.Properties.Remove($property.Name)
        Save-Settings $path $settings.Object
        Write-Tag ok ('face_source removed from {0} (backup {1}); after a VRCFaceTracking restart the module claims the eyes only' -f $path, $backup)
    }
    catch { Write-Warning ('Could not write {0}: {1}' -f $path, $_.Exception.Message) }
}

function Get-FrameFaceVersion {
    $init = Join-Path $PSScriptRoot 'frameface\__init__.py'
    if (Test-Path -LiteralPath $init -PathType Leaf) {
        $match = [regex]::Match([IO.File]::ReadAllText($init), '__version__\s*=\s*"([^"]+)"')
        if ($match.Success) { return $match.Groups[1].Value }
    }
    return '<version>'
}

function Get-LocalIPv4 {
    # This PC's IPv4 addresses, read from its own interfaces (no network traffic); .NET answers when
    # Get-NetIPAddress (NetTCPIP) cannot.
    try { return @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | ForEach-Object { [string]$_.IPAddress }) }
    catch {
        try {
            return @([System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() |
                    ForEach-Object { $_.GetIPProperties().UnicastAddresses } |
                    Where-Object { $_.Address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork } |
                    ForEach-Object { $_.Address.IPAddressToString })
        }
        catch { return @() }
    }
}

function Select-StreamUrl([string[]]$Urls) {
    # url prints the Frame-link URL (10.35.78.1) and, when allow_peers admits a LAN, the headset's LAN URL.
    # Baballonia on this PC needs the one it reaches: the Frame-link URL if this PC has an IPv4 address in
    # 10.35.78.0/24, otherwise the LAN URL. OnLink is $null when there is nothing to choose.
    $link = @($Urls | Where-Object { $_ -match '^http://10\.35\.78\.1:' })
    $lan = @($Urls | Where-Object { $_ -notmatch '^http://10\.35\.78\.1:' })
    if (-not $link.Count -or -not $lan.Count) { return @{ Url = $Urls[0]; Other = $null; OnLink = $null } }
    if (@(Get-LocalIPv4 | Where-Object { $_ -match '^10\.35\.78\.[0-9]{1,3}$' }).Count) {
        return @{ Url = $link[0]; Other = $lan[0]; OnLink = $true }
    }
    return @{ Url = $lan[0]; Other = $link[0]; OnLink = $false }
}

function Show-NextSteps($Choice) {
    $version = Get-FrameFaceVersion
    $step = 1
    Write-Host ''
    Write-Host 'Next steps:'
    if ($NoStart) {
        # install.sh named the headset's form of this command; from this PC it goes through ssh.
        Write-Host ('{0}. Start the face service (installed with -NoStart), from this PC:' -f $step)
        Write-Host ('     ssh {0} "python3 -I ~/frameface/frameface_main.py start"' -f $remote)
        $step++
    }
    Write-Host ('{0}. Install the module with VRCFaceTracking closed (tray icon -> Exit; no VRCFaceTracking process left),' -f $step)
    Write-Host '   then in the repository folder:'
    Write-Host ('     $m = "' + $VrcftConfigDir + '\CustomLibs\' + $ModuleId + '"')
    Write-Host ('     if (Test-Path $m) { Copy-Item -Recurse $m "' + $VrcftConfigDir + '\SteamFrameEye-backup-$(Get-Date -Format yyyyMMdd-HHmmss)" }')
    Write-Host ('     Expand-Archive -Force .\dist\SteamFrameEyeModule-' + $version + '.zip -DestinationPath $m')
    Write-Host '   Start VRCFaceTracking: Documents\VRCFaceTracking\latest.log shows "SteamFrameEye: claims eye=True expression=True".'
    Write-Host '   Do not install the VRCFT-Babble module.'
    $step++
    $address = 'the URL above'
    if ($Choice -and $Choice.Other) {
        if ($Choice.OnLink) { $address = '{0} (this PC has an address on the Frame link 10.35.78.0/24)' -f $Choice.Url }
        else { $address = '{0} (the headset''s LAN address: this PC has no address on the Frame link 10.35.78.0/24)' -f $Choice.Url }
    }
    Write-Host ('{0}. Baballonia: Face Camera Address = {1}, then "This is a..." = Wireless/IP Camera, then Start Camera.' -f $step, $address)
    if ($Choice -and $Choice.Other) {
        if ($Choice.OnLink) { Write-Host ('   Alternative: {0} (the headset''s LAN address): use it on a PC that reaches the headset only over the home LAN.' -f $Choice.Other) }
        else { Write-Host ('   Alternative: {0} (the Frame link): use it when this PC has an address in 10.35.78.0/24.' -f $Choice.Other) }
    }
    Write-Host '   Settings: Auto Start with SteamVR on; OSC IP 127.0.0.1, port 8888, prefix empty.'
    $step++
    Write-Host ('{0}. Later: install-frameface.ps1 -HeadsetHost {1} -Check checks the whole chain (read-only).' -f $step, $HeadsetHost)
}

function Test-ModuleConflict([string]$Id, [string]$Text) {
    foreach ($conflict in $KnownConflicts) {
        if (($conflict.Id -and $Id -eq $conflict.Id) -or ($Text -match $conflict.Pattern)) { return $conflict.Reason }
    }
    return $null
}

function Invoke-ModuleCheck {
    $libs = Join-Path $VrcftConfigDir 'CustomLibs'
    if (-not (Test-Path -LiteralPath $libs -PathType Container)) {
        Write-Tag warn ('no CustomLibs folder in {0}: no module is installed' -f $VrcftConfigDir)
        return
    }
    $ours = $false
    foreach ($dir in @(Get-ChildItem -LiteralPath $libs -Directory -Force | Sort-Object Name)) {
        $manifest = Join-Path $dir.FullName 'module.json'
        $hasOurDll = Test-Path -LiteralPath (Join-Path $dir.FullName 'SteamFrameEyeModule.dll') -PathType Leaf
        $where = 'CustomLibs\' + $dir.Name
        if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
            if ($dir.Name -eq $ModuleId) {
                $ours = $true
                Write-Tag fail ("{0} has no module.json: an interrupted 'Install local module'. Exit VRCFaceTracking and install the module with the commands of the install's next steps (VRCFaceTracking closed)" -f $where)
            }
            elseif ($hasOurDll) { Write-Tag fail ('{0} holds a copy of SteamFrameEyeModule.dll: move backups out of CustomLibs' -f $where) }
            else { Write-Tag warn ('{0} has no module.json (VRCFaceTracking ignores it)' -f $where) }
            continue
        }
        $module = $null
        try { $module = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($manifest)) } catch { $module = $null }
        if ($module -isnot [System.Management.Automation.PSCustomObject]) { Write-Tag warn ('{0}\module.json is not valid JSON' -f $where); continue }
        $id = [string]$module.ModuleId
        $name = [string]$module.ModuleName
        $version = [string]$module.Version
        $dll = [string]$module.DllFileName
        $label = '{0} {1} ({2}) in {3}' -f $name, $version, $id, $where
        if ($id -eq $ModuleId -and $dir.Name -eq $ModuleId) {
            $ours = $true
            if (-not $dll) { $dll = 'SteamFrameEyeModule.dll' }
            if (Test-Path -LiteralPath (Join-Path $dir.FullName $dll) -PathType Leaf) { Write-Tag ok ('SteamFrameEye module: {0}' -f $label) }
            else { Write-Tag fail ('SteamFrameEye module: {0}, but {1} is missing: install the module again (VRCFaceTracking closed)' -f $label, $dll) }
        }
        elseif ($id -eq $ModuleId -or $hasOurDll -or $dll -eq 'SteamFrameEyeModule.dll') {
            Write-Tag fail ('second copy of SteamFrameEye: {0}; move it out of CustomLibs (VRCFaceTracking loads every folder there)' -f $label)
        }
        else {
            $reason = Test-ModuleConflict $id ('{0} {1}' -f $name, $dll)
            if ($reason) { Write-Tag fail ('{0}: {1}' -f $label, $reason) }
            else { Write-Tag warn ('other module: {0} {1} ({2}) may claim eyes or expressions first' -f $name, $version, $id) }
        }
    }
    foreach ($file in @(Get-ChildItem -LiteralPath $libs -File -Filter '*.dll' -Force | Sort-Object Name)) {
        $label = 'legacy module CustomLibs\' + $file.Name
        if ($file.Name -eq 'SteamFrameEyeModule.dll') { Write-Tag fail ('{0}: a copy of SteamFrameEyeModule.dll directly in CustomLibs; move it out' -f $label) }
        else {
            $reason = Test-ModuleConflict '' $file.Name
            if ($reason) { Write-Tag fail ('{0}: {1}' -f $label, $reason) }
            else { Write-Tag warn ('other module: {0} may claim eyes or expressions first' -f $label) }
        }
    }
    if (-not $ours) { Write-Tag warn ('the SteamFrameEye module is not installed (CustomLibs\{0})' -f $ModuleId) }
}

function Invoke-Check {
    $core = Join-Path $VrcftInstallDir 'VRCFaceTracking.Core.dll'
    if (Test-Path -LiteralPath $core -PathType Leaf) {
        Write-Tag ok ('VRCFaceTracking {0} ({1})' -f (Get-Item -LiteralPath $core).VersionInfo.FileVersion, $core)
    }
    else { Write-Tag warn ('VRCFaceTracking.Core.dll not found in {0} (use -VrcftInstallDir)' -f $VrcftInstallDir) }
    Invoke-ModuleCheck
    $path = Join-Path $VrcftConfigDir $SettingsName
    $settings = Read-Settings $path
    if ($null -eq $settings) { Write-Tag fail ('{0} is not a valid JSON object: the module uses its defaults (face_source off)' -f $path) }
    elseif (-not $settings.Exists) { Write-Tag warn ('{0} does not exist: face_source is off; install FrameFace (it adds the key)' -f $path) }
    else {
        $property = Find-FaceSource $settings.Object
        if (-not $property) { Write-Tag warn 'face_source is not set: the face path is off; run install-frameface.ps1 again (it adds the key)' }
        elseif ($property.Value -is [string] -and $property.Value -ceq 'babble_osc') { Write-Tag ok 'face_source = "babble_osc"' }
        else { Write-Tag warn ('face_source is "{0}": the face path is off; run the install with -EnableFace' -f $property.Value) }
    }
    try {
        $owners = @(Get-NetUDPEndpoint -LocalPort 8888 -ErrorAction Stop | ForEach-Object {
                $process = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
                if ($process) { '{0} (pid {1})' -f $process.ProcessName, $process.Id } else { 'pid {0}' -f $_.OwningProcess }
            } | Sort-Object -Unique)
        if (@($owners | Where-Object { $_ -notmatch '^VRCFaceTracking' }).Count) {
            Write-Tag fail ('UDP 8888 is held by {0}: Baballonia''s OSC cannot reach the module; stop that program' -f ($owners -join ', '))
        }
        else { Write-Tag ok ('UDP 8888 is held by {0}' -f ($owners -join ', ')) }
    }
    catch [System.Management.Automation.CommandNotFoundException] { Write-Tag warn 'cannot list UDP endpoints (Get-NetUDPEndpoint is not available)' }
    catch {
        # Get-NetUDPEndpoint reports "no such endpoint" as ObjectNotFound.
        if ([string]$_.CategoryInfo.Category -eq 'ObjectNotFound') {
            Write-Tag warn 'nothing listens on UDP 8888: VRCFaceTracking with this module and face_source "babble_osc" binds it'
        }
        else { Write-Tag warn ('cannot list UDP endpoints: {0}' -f $_.Exception.Message) }
    }
    $baballonia = @(Get-Process -Name 'Baballonia*' -ErrorAction SilentlyContinue)
    if ($baballonia.Count) { Write-Tag ok ('Baballonia is running ({0})' -f (($baballonia | ForEach-Object { $_.ProcessName } | Sort-Object -Unique) -join ', ')) }
    else { Write-Tag info 'Baballonia is not running (Auto Start with SteamVR starts it with SteamVR)' }
    if ($PSCmdlet.ShouldProcess($remote, 'Read the FrameFace status (ssh)')) {
        $result = Read-Native $ssh.Source ($sshArgs + @($remote, 'python3 -I ~/frameface/frameface_main.py status --json'))
        $text = ($result.Lines -join "`n").Trim()
        $status = $null
        if ($text.StartsWith('{')) { try { $status = ConvertFrom-Json -InputObject $text } catch { $status = $null } }
        if ($status -is [System.Management.Automation.PSCustomObject] -and $status.display) {
            $tag = switch ([string]$status.severity) { 'ok' { 'ok' } 'info' { 'ok' } 'warn' { 'warn' } default { 'fail' } }
            Write-Tag $tag ('FrameFace on {0}: {1}: {2}' -f $HeadsetHost, $status.display, $status.message)
            if ($status.hint) { Write-Host ('       hint: {0}' -f $status.hint) }
        }
        else { Write-Tag fail ('FrameFace on {0} (ssh exit {1}): {2}' -f $HeadsetHost, $result.Code, $text) }
    }
    Write-Host ('Check done: {0} failure(s).' -f $script:Fails)
}

# -- validation (all before any SSH)
$modes = @(@(@('-Probe', $Probe), @('-ProbeActive', $ProbeActive), @('-Check', $Check), @('-Uninstall', $Uninstall),
        @('-Rollback', $Rollback)) | Where-Object { $_[1].IsPresent } | ForEach-Object { $_[0] })
if ($modes.Count -gt 1) { throw ('Choose one mode: {0} cannot be combined.' -f ($modes -join ', ')) }
$mode = if ($modes.Count) { $modes[0].TrimStart('-') } else { 'Install' }
if ($Purge -and -not $Uninstall) { throw '-Purge is only used with -Uninstall.' }
if ($IncludeImages -and -not ($Probe -or $ProbeActive)) { throw '-IncludeImages is only used with -Probe or -ProbeActive.' }
if ($ProbeArgs.Trim() -and -not $ProbeActive) { throw '-ProbeArgs is only used with -ProbeActive.' }
# "-AllowPeer a,b" arrives as one string when the script runs with -File: split on commas.
$AllowPeer = @($AllowPeer | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$installOnly = @(@(@('-NoStart', $NoStart), @('-NewToken', $NewToken), @('-EnableFace', $EnableFace), @('-CopyUrl', $CopyUrl),
        @('-AllowPeer', [switch]($AllowPeer.Count -gt 0))) | Where-Object { $_[1].IsPresent } | ForEach-Object { $_[0] })
if ($mode -ne 'Install' -and $installOnly.Count) { throw ('{0}: only used when installing.' -f ($installOnly -join ', ')) }
if ($SkipModuleSettings -and $mode -notin @('Install', 'Uninstall')) { throw '-SkipModuleSettings is only used when installing or uninstalling.' }
# Values reach a remote shell: check them again, case-sensitively (ValidatePattern ignores case).
if ($HeadsetHost -cnotmatch '^[A-Za-z0-9][A-Za-z0-9.-]*$') { throw "Invalid -HeadsetHost $HeadsetHost." }
if ($User -cnotmatch '^[A-Za-z_][A-Za-z0-9_.-]*$') { throw "Invalid -User $User." }
if ($ProbeArgs -cnotmatch '^[A-Za-z0-9 ._=-]*$') { throw 'Invalid -ProbeArgs (letters, digits, space, ".", "_", "=", "-").' }
foreach ($peer in $AllowPeer) {
    $valid = $peer -cmatch '^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})/([0-9]{1,2})$'
    if ($valid) {
        foreach ($i in 1..4) { if ([int]$Matches[$i] -gt 255) { $valid = $false } }
        if ([int]$Matches[5] -gt 32) { $valid = $false }
    }
    if (-not $valid) { throw "Invalid -AllowPeer $peer (IPv4 CIDRs such as 192.168.4.31/32, comma-separated)." }
}

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
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

switch ($mode) {
    'Install' {
        $files = Get-ReleaseFiles
        $installArgs = @()
        if ($NoStart) { $installArgs += '--no-start' }
        if ($NewToken) { $installArgs += '--new-token' }
        foreach ($peer in $AllowPeer) { $installArgs += @('--allow-peer', $peer) }
        $go = $PSCmdlet.ShouldProcess($remote, 'Install FrameFace (upload, build, selftest, start)')
        $choice = $null
        if ($go) {
            $stage = New-Stage $files
            try {
                Invoke-Native $scp.Source ($scpArgs + @('-r', $stage.Dir, ('{0}:{1}' -f $remote, $stage.Name)))
                if ($script:NativeExit -ne 0) { throw ('Upload failed (scp exit code {0}). The headset installer was not run.' -f $script:NativeExit) }
                # Every interpolated value is validated above or generated here.
                $command = (@('bash', ('~/{0}/install.sh' -f $stage.Name)) + $installArgs) -join ' '
                Invoke-Native $ssh.Source ($sshArgs + @($remote, $command))
                if ($script:NativeExit -ne 0) {
                    throw ('Headset installation failed (ssh exit code {0}). The upload stays at ~/{1} for diagnosis (install.sh --uninstall removes it).' -f $script:NativeExit, $stage.Name)
                }
                $result = Read-Native $ssh.Source ($sshArgs + @($remote, ('rm -rf -- ~/{0} && python3 -I ~/frameface/frameface_main.py url' -f $stage.Name)))
                $urls = @($result.Lines | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ -match '^http://\S+$' })
                if ($result.Code -ne 0 -or $urls.Count -eq 0) {
                    Write-Warning ('Installed, but the URL could not be read (ssh exit code {0}): ssh {1} "python3 -I ~/frameface/frameface_main.py url"' -f $result.Code, $remote)
                }
                else {
                    Write-Host ''
                    Write-Host 'Baballonia Face Camera Address ("This is a..." = Wireless/IP Camera):'
                    foreach ($url in $urls) { Write-Host $url }
                    $choice = Select-StreamUrl $urls
                    if ($CopyUrl) {
                        $which = 'The URL'
                        if ($choice.OnLink -eq $true) { $which = 'The Frame-link URL (10.35.78.1)' }
                        elseif ($choice.OnLink -eq $false) { $which = 'The LAN URL ({0})' -f ([uri]$choice.Url).Host }
                        try {
                            Set-Clipboard -Value $choice.Url
                            Write-Host ('[info] {0} is on the clipboard. Windows clipboard history and cloud clipboard sync may keep a copy (it contains the stream token).' -f $which)
                        }
                        catch { Write-Warning ('Could not copy the URL to the clipboard: {0}' -f $_.Exception.Message) }
                    }
                }
            }
            finally { Remove-Stage $stage }
        }
        if (-not $SkipModuleSettings -and ($go -or $WhatIfPreference)) { Set-FaceSource }
        if ($go) { Show-NextSteps $choice }
    }
    'Probe' {
        $bundle = 'probe-' + $stamp
        $local = Join-Path $PWD.ProviderPath ('frameface-' + $bundle)
        if ($PSCmdlet.ShouldProcess($remote, ('Read-only probe (report ~/.local/state/frameface/{0})' -f $bundle))) {
            if ($IncludeImages) { Write-Host '[info] the read-only probe writes no images; -IncludeImages has no effect here' }
            Invoke-Native $ssh.Source ($sshArgs + @($remote, ('python3 -I ~/frameface/frameface_main.py probe --bundle-name {0}' -f $bundle)))
            $probeExit = $script:NativeExit
            if ($probeExit -eq 255) { throw 'ssh failed (exit code 255): the headset was not reached.' }
            Invoke-Native $scp.Source ($scpArgs + @('-r', ('{0}:.local/state/frameface/{1}' -f $remote, $bundle), $local))
            if ($script:NativeExit -ne 0) { throw ('Could not copy the probe report (scp exit code {0}); it stays at ~/.local/state/frameface/{1} on the headset.' -f $script:NativeExit, $bundle) }
            Write-Host ('Probe report (probe exit code {0}: 0 no FAIL, 1 some FAIL, 2 refused): {1}' -f $probeExit, $local)
        }
    }
    'ProbeActive' {
        $bundle = 'probe-' + $stamp
        $local = Join-Path $PWD.ProviderPath ('frameface-' + $bundle)
        $what = "Turns the tracker's IR LEDs on and stops the face service for 1-3 minutes"
        if ($PSCmdlet.ShouldProcess($remote, $what)) {
            if ($PSBoundParameters.ContainsKey('Confirm') -or $PSCmdlet.ShouldContinue($what + '. Continue?', 'FrameFace active probe')) {
                $command = 'python3 -I ~/frameface/frameface_main.py probe --active --yes --stop-service --bundle-name ' + $bundle
                if ($ProbeArgs.Trim()) { $command += ' ' + $ProbeArgs.Trim() }
                Invoke-Native $ssh.Source ($sshArgs + @('-t', $remote, $command))
                $probeExit = $script:NativeExit
                # Whatever the probe did (an SSH drop kills it before its own restart): start the
                # service again if the probe's marker says it stopped it.
                $restart = 'if [ -e ~/.local/state/frameface/probe-stopped-service ]; then XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user start frameface.service && rm -f ~/.local/state/frameface/probe-stopped-service; fi'
                Invoke-Native $ssh.Source ($sshArgs + @($remote, $restart))
                if ($script:NativeExit -ne 0) { Write-Warning ('Could not restart the face service (ssh exit code {0}); run: ssh {1} "python3 -I ~/frameface/frameface_main.py start"' -f $script:NativeExit, $remote) }
                Invoke-Native $scp.Source ($scpArgs + @('-r', ('{0}:.local/state/frameface/{1}' -f $remote, $bundle), $local))
                if ($script:NativeExit -ne 0) { throw ('Could not copy the probe report (scp exit code {0}); it stays at ~/.local/state/frameface/{1} on the headset.' -f $script:NativeExit, $bundle) }
                if ($IncludeImages) {
                    Invoke-Native $scp.Source ($scpArgs + @('-r', ('{0}:.local/state/frameface/{1}-images' -f $remote, $bundle), ($local + '-images')))
                    if ($script:NativeExit -ne 0) { Write-Warning ('No images copied (scp exit code {0}); only --dump and --pixels write them.' -f $script:NativeExit) }
                    else { Write-Host ('Probe images (raw frames and pixel traces of your face): {0}-images' -f $local) }
                }
                Write-Host ('Probe report (probe exit code {0}: 0 no FAIL, 1 some FAIL, 2 refused): {1}' -f $probeExit, $local)
            }
        }
    }
    'Check' { Invoke-Check }
    default {
        # Uninstall and Rollback: upload a fresh install.sh (~/frameface may be gone) and run it.
        $flags = if ($Rollback) { '--rollback' } elseif ($Purge) { '--uninstall --purge' } else { '--uninstall' }
        $what = if ($Rollback) { 'Roll FrameFace back to the previous version' } elseif ($Purge) { 'Remove FrameFace with its config and state' } else { 'Remove FrameFace (config and state are kept)' }
        $go = $PSCmdlet.ShouldProcess($remote, $what)
        if ($go) {
            $stage = New-Stage @('install.sh')
            try {
                Invoke-Native $scp.Source ($scpArgs + @('-r', $stage.Dir, ('{0}:{1}' -f $remote, $stage.Name)))
                if ($script:NativeExit -ne 0) { throw ('Upload failed (scp exit code {0}). Nothing was changed on the headset.' -f $script:NativeExit) }
                $command = 'bash ~/{0}/install.sh {1}; rc=$?; rm -rf -- ~/{0}; exit $rc' -f $stage.Name, $flags
                Invoke-Native $ssh.Source ($sshArgs + @($remote, $command))
                if ($script:NativeExit -ne 0) { throw ('install.sh {0} failed on the headset (ssh exit code {1}).' -f $flags, $script:NativeExit) }
            }
            finally { Remove-Stage $stage }
        }
        if ($Uninstall -and -not $SkipModuleSettings -and ($go -or $WhatIfPreference)) { Remove-FaceSource }
    }
}
exit 0
