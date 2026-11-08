# FrameEye Bridge

This standalone fork adds native Windows build/package/install scripts and improved eye smoothing, including direct VRChat OSC. It preserves the history and licenses of `CyrusOtter/SteamFrameEye`. The original GitHub repository was unavailable when this fork was published, so GitHub does not link it as a fork.

Steam Frame eye tracking for VRCFaceTracking: per-eye gaze **and eyelid openness** from the Valve Steam Frame's built-in eye tracking, which the public OpenXR and OpenVR APIs do not expose. Since 0.6.0 the same module can also drive the mouth: a VIVE Facial Tracker plugged into the Frame is read by FrameFace on the headset and turned into face expressions by Baballonia on the PC (see [Face tracking with a VIVE Facial Tracker (FrameFace)](#face-tracking-with-a-vive-facial-tracker-frameface)).

The project has three parts:

- **Headset app** in [`headset/`](headset/README.md): FrameEye Bridge, a SteamVR dashboard app that runs on the Steam Frame and sends the eye data to the PC.
- **Headset face service** in [`headset/frameface/`](headset/frameface/README.md) (optional): FrameFace, a user service that reads a VIVE Facial Tracker over USB and serves its camera image to Baballonia on the PC.
- **VRCFaceTracking module** in this folder: `SteamFrameEyeModule` (shown as "Steam Frame Eye + Face (network)" in VRCFaceTracking), which receives the eye data on the PC, cleans it up and feeds it into VRCFaceTracking the way the Steam Link module feeds a Quest Pro's, and, when the face path is enabled, maps Baballonia's face expressions. It is an ordinary external module that builds against VRCFaceTracking's released assemblies, so no VRCFaceTracking source checkout is needed.

## How it works

```
Steam Frame (headset)                                        PC
/dev/shm/eye-server.mmap --> FrameEye Bridge ---UDP 9021---> SteamFrameEyeModule.dll (in VRCFaceTracking)
(eye-tracking service)       (headset/, SteamVR app)         UnifiedTracking.Data.Eye.{Left,Right}.{Gaze,Openness}
VIVE Facial Tracker --USB--> FrameFace <---TCP 9023, MJPEG--- Baballonia --OSC 127.0.0.1:8888--> same module
(optional, USB-C port)       (headset/frameface/)                                                UnifiedTracking.Data.Shapes
```

- The headset's eye-tracking service publishes every sample (90 Hz) in the shared-memory object `/dev/shm/eye-server.mmap`. FrameEye Bridge waits on its sequence counter and sends one 96-byte `SFEY` packet per sample. It runs as the `steamos` user from the home directory, needs no root and leaves the read-only system untouched. The memory layout follows [konsti219/frameeyeosc](https://github.com/konsti219/frameeyeosc) (MIT). Since v0.5.3 the app reads the object's ABI version and supports versions 4 and 5; version 5 arrived with the SteamOS 0.4.3 beta (see [headset/README.md](headset/README.md)). The SFEY packet and the PC module are unchanged.
- The module listens on UDP 9021, validates each packet, runs it through the processing described below and writes gaze and openness into VRCFaceTracking. Pupil diameter is pinned to 5 mm because the headset does not provide it.
- Data only flows while the headset is worn and SteamVR is out of standby. The module holds the last values and logs when packets stop and resume.
- The face path is optional and off until `"face_source": "babble_osc"` is set (the FrameFace installer does that). FrameFace serves the tracker's image as an MJPEG stream on TCP 9023; Baballonia connects to it, runs its face model and sends 45 expressions as OSC to `127.0.0.1:8888`, where the module maps them to Unified Expressions (mouth, jaw, cheeks, tongue). Without `face_source` the module behaves exactly like 0.5.3.

## Quick start

1. **Windows PC:** run `powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 -VrcftDir .\lib\vrcft-5.4.5`. It writes `dist\SteamFrameEyeModule-0.6.0.zip`, `dist\FrameEye-Bridge-0.6.0.tar.gz`, `dist\FrameFace-0.6.0.tar.gz` and `dist\module.json`. Exit VRCFaceTracking (tray icon -> Exit), unzip `dist\SteamFrameEyeModule-0.6.0.zip` into `%APPDATA%\VRCFaceTracking\CustomLibs\58be7fa6-4102-44ab-adba-25021d3af8b7\` (step 3 of the [face tracking install](#install-frameface-and-the-module) has the commands; 0.6.0 replaces 0.5.3 there) and start VRCFaceTracking again. Allow inbound UDP 9021 for VRCFaceTracking's module process in Windows Firewall.
2. **Headset:** install FrameEye Bridge with the PC's address, as described in [headset/README.md](headset/README.md).
3. Start VRCFaceTracking and put the headset on. The eyelid calibration learns within about a minute of normal blinking and is kept for the next session.
4. **Optional, mouth tracking:** with a VIVE Facial Tracker, install FrameFace and Baballonia as described in [Face tracking with a VIVE Facial Tracker (FrameFace)](#face-tracking-with-a-vive-facial-tracker-frameface).

### Windows scripts

Windows 10/11, Windows PowerShell 5.1 or PowerShell 7, and the .NET 10 SDK are sufficient to build and package. Packaging uses Windows' included `tar.exe` (`System32\tar.exe`, also when another `tar`, such as Git Bash's, comes first on `PATH`) and .NET's ZIP support. Bash, WSL, and Python are not needed on the PC for these steps.

From the repository folder:

```powershell
# Build and create the release archives (module, FrameEye Bridge, FrameFace) and module.json in dist\.
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 -VrcftDir .\lib\vrcft-5.4.5

# Install the headset app over SSH; replace both example addresses.
powershell -NoProfile -ExecutionPolicy Bypass -File .\headset\install-overlay.ps1 `
    -HeadsetHost 192.168.1.50 -PcAddress 192.168.1.10
```

The headset installer needs Windows OpenSSH Client (`ssh.exe` and `scp.exe`) and SSH access to the Steam Frame. It uploads the app, runs the headset's existing installer and prints its status. The eye-data reader and dashboard still run on the Steam Frame: their shared-memory source and Linux SteamVR libraries are on that device. `frameeye_overlay.sh` remains its SteamVR launcher; it is not a Windows PC program.

`build.ps1` accepts `-Version`, `-VrcftDir`, `-OutDir`, `-DownloadUrlBase` and `-PageUrl`. Relative output paths are based on the repository folder, so calling the script from another directory works too. `-VrcftDir .\lib\vrcft-5.4.5` builds against the vendored VRCFaceTracking 5.4.5 reference assemblies, as the releases do; such a build loads in VRCFaceTracking 5.4.x and 6.0. Use `-VrcftDir 'D:\SteamLibrary\steamapps\common\VRCFaceTracking'` to select another VRCFaceTracking installation.

To package an already built DLL:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\packaging\package.ps1 `
    -Version 0.6.0 -DownloadUrlBase 'https://example.com/releases/download/v0.6.0' `
    -PageUrl 'https://example.com/project'
```

Omit the URL arguments for a local package (`IsLocal: true`, no download URL). The outputs are `SteamFrameEyeModule-<version>.zip`, `FrameEye-Bridge-<version>.tar.gz`, `FrameFace-<version>.tar.gz` and `module.json`, matching the Linux packaging layout; they are published together only after all of them were built. `-DllPath` and `-OutDir` accept absolute paths or paths relative to the repository, and `-TarPath` selects another `tar` program. Each headset archive holds exactly the files of its `release-files.txt` (`headset/` and `headset/frameface/`): the installers are included; personal settings, logs, probe reports, compiled helpers and other generated files are not.

## Face tracking with a VIVE Facial Tracker (FrameFace)

Since 0.6.0 the module can also drive the mouth. A **VIVE Facial Tracker** (USB `0bb4:0321`, the lip tracker sold for Index, Vive Pro Eye and Vive Pro 2) plugged into the Steam Frame is read by **FrameFace**, a small service on the headset, which serves the tracker's camera image as an MJPEG stream. **Baballonia** on the PC turns the image into 45 face expressions and sends them as OSC to the module, which maps them to VRCFaceTracking's Unified Expressions. The eyes keep coming from FrameEye Bridge; nothing changes for them.

```
Steam Frame (user steamos, no root)                                          Windows PC
VFT 0bb4:0321 --USB2 bulk--> FrameFace (systemd user unit, Python 3.12 stdlib + ctypes usbfs)
                             driver -> frame assembly -> health gate -> Baballonia-exact pixels (C helper | SWAR)
                             -> libturbojpeg gray q92 -> MJPEG HTTP 0.0.0.0:9023 (peer allow-list + path token)
                                       ^------------- TCP, the PC connects out ----------- Baballonia (user-installed, stock)
                                                                                           "Wireless/IP Camera", faceModel.onnx
                                                                                           OSC -> 127.0.0.1:8888 (45 x "/name ,f")
/dev/shm/eye-server.mmap -> FrameEye Bridge (UNCHANGED) -- SFEY UDP 9021 -------------> SteamFrameEyeModule 0.6.0 (VRCFT 6.0)
                                                                                           same ModuleId 58be7fa6-..., replaces 0.5.3,
                                                                                           eyes (unchanged) + face (Babble45 -> UE)
                                                                                           -> VRChat (FT/v2 or /tracking/face/v1)
```

Requirements:

- A VIVE Facial Tracker, plugged straight into the Frame's USB-C port: no hub, and not while the headset charges (the port then works in charging/PC mode and cannot run the tracker).
- On the headset: FrameEye Bridge set up as usual, and SSH from the PC as `steamos` (the examples use the SSH host name `frame`; use the headset's address or your own `~/.ssh/config` alias). FrameFace needs no sudo: SteamOS already lets `steamos` use USB devices. It uses the headset's `python3` (3.10 or newer), `libturbojpeg`, and `gcc` if present (for its fast C pixel helper; without it a slower Python path runs at up to 30 fps).
- On the PC: VRCFaceTracking 5.4.x or 6.0; Baballonia, installed by you (Steam app 4091970 or its installer; it is not part of this project); Windows PowerShell 5.1 or PowerShell 7 with the Windows OpenSSH client; the .NET 10 SDK to build. Do **not** install the VRCFT-Babble module: this module replaces it, and both would claim the expressions and UDP 8888.

### Install FrameFace and the module

```powershell
cd C:\path\to\SteamFrameEye
# 1. Build all release archives against the vendored VRCFT 5.4.5 references (loads in 5.4.x and 6.0)
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 -VrcftDir .\lib\vrcft-5.4.5
#    -> dist\SteamFrameEyeModule-0.6.0.zip, dist\FrameEye-Bridge-0.6.0.tar.gz, dist\FrameFace-0.6.0.tar.gz, dist\module.json
# 2. Install FrameFace on the headset and enable the face path of the module (no sudo)
powershell -NoProfile -ExecutionPolicy Bypass -File .\headset\frameface\install-frameface.ps1 -HeadsetHost frame
```

Step 2 uploads the FrameFace files, runs the headset installer `install.sh` (it builds the C helper, runs the install selftest of the new version and only then swaps it in, and installs the user service `frameface.service`), and prints the selftest result and the Baballonia URL `http://10.35.78.1:9023/stream/<token>`. It also adds `"face_source": "babble_osc"` to `%APPDATA%\VRCFaceTracking\SteamFrameEyeModule.json` if that key is absent (with a backup `SteamFrameEyeModule.json.bak-<yyyyMMdd-HHmmss>`, made only when it writes); a deliberate `"face_source": "off"` stays unless you add `-EnableFace`. With `-CopyUrl` the URL is also put on the clipboard. When the headset also prints its LAN URL (see `-AllowPeer` under [Privacy](#privacy)), the next steps and `-CopyUrl` use the URL this PC reaches: the Frame-link URL if this PC has an IPv4 address in `10.35.78.0/24`, otherwise the LAN URL. `install-frameface.ps1` never copies the module into VRCFaceTracking; that is step 3.

**3. Install the module** (same ModuleId, replaces 0.5.3). **Primary, safe way:** exit VRCFaceTracking (tray icon -> Exit; no `VRCFaceTracking` process left in Task Manager), then in the repository folder:

```powershell
$m = "$env:APPDATA\VRCFaceTracking\CustomLibs\58be7fa6-4102-44ab-adba-25021d3af8b7"
if (Test-Path $m) { Copy-Item -Recurse $m "$env:APPDATA\VRCFaceTracking\SteamFrameEye-backup-$(Get-Date -Format yyyyMMdd-HHmmss)" }
Expand-Archive -Force .\dist\SteamFrameEyeModule-0.6.0.zip -DestinationPath $m
```

The backup goes next to `CustomLibs`, never inside it: a copy with `module.json` inside `CustomLibs` would load as a second SteamFrameEye. Start VRCFaceTracking. The module appears as "Steam Frame Eye + Face (network)", and `Documents\VRCFaceTracking\latest.log` shows `SteamFrameEye: claims eye=True expression=True (eye UDP 9021, face 127.0.0.1:8888, face_source=babble_osc)`.

**Alternative:** VRCFaceTracking -> Module Registry -> **Install local module** -> the zip. VRCFaceTracking 6.0 deletes the module folder before it re-initialises the modules, while the running 0.5.3 still holds its DLL; Windows then refuses to delete the DLL after `module.json`, `README.md` and `LICENSE` are gone, and the eyes stop because a folder without `module.json` is skipped. If that happens, run "Install local module" once more (the old DLL is no longer loaded) or use the primary way; `install-frameface.ps1 -HeadsetHost frame -Check` reports the state. Please confirm once which way works on your VRCFaceTracking before relying on the in-app one.

**4. Baballonia**, once: see [Baballonia setup](#baballonia-setup).

**4a. Optional sim check, no tracker needed.** It answers whether your Baballonia build accepts the stream and whether the stream disturbs the headset's PC link. During a PC VR session (SteamVR Home is enough; if VRChat is open with `face_source` on, **your avatar's mouth follows the synthetic scene**, so use a private world or close VRChat), run:

```powershell
ssh -t frame "python3 -I ~/frameface/frameface_main.py sim --listen 0.0.0.0 --port 19023 --allow 10.35.78.0/24 --scene mouth --seconds 900"
```

It prints `http://10.35.78.1:19023/stream/<token>` (a one-time token; the real service keeps running untouched). Put that into Baballonia's Face Camera Address (Wireless/IP Camera) and press Start Camera; afterwards put the real URL back. Note whether Baballonia shows the gray synthetic mouth (a dark ellipse opening and closing every 2 s) within about 2 s, whether it ever reconnects, `stream.fps_out` and `stream.kbps` from `http://10.35.78.1:19023/status` (no token needed), the vrlink resets before and after (`ssh frame "grep -ci reset ~/.local/share/Steam/logs/vrlink.txt"`), the SteamVR performance overlay, and the sim's final `{"sim_exit":...}` line. Expected: `kbps` at most 5000 (frames above that are skipped, so `fps_out` may stay below 60), no additional vrlink resets, no Baballonia reconnect.

**5. Probe.** Plug the tracker straight into the Frame's USB-C port, put the headset on, then run the read-only probe: `powershell -NoProfile -ExecutionPolicy Bypass -File .\headset\frameface\install-frameface.ps1 -HeadsetHost frame -Probe` (report in `.\frameface-probe-<ts>\`). Optional characterisation (switches the tracker's IR LEDs on and stops the face service for 1-3 minutes; asks for confirmation): `... -ProbeActive`, with `-ProbeArgs '--matrix --soak 300'` or `-IncludeImages` as needed. If the probe reports `[FAIL] P7 node access`, use the udev fallback of [headset/frameface/README.md](headset/frameface/README.md) (one sudo step that you type yourself).

**6. Use:** start PC VR from the Frame; Baballonia starts with SteamVR and opens its camera; your mouth moves in VRChat.

**Upgrade:** run steps 1-3 again; the headset config, the token, the learned tracker profile and a deliberate `face_source: "off"` are kept. **Uninstall:** `install-frameface.ps1 -HeadsetHost frame -Uninstall [-Purge]`; it also removes `face_source` from the module settings (with a backup), so the module behaves like 0.5.3 and the expressions are free after a VRCFaceTracking restart. **Module rollback:** the step-3 procedure with `SteamFrameEyeModule-0.5.3.zip`, or copy the backup folder back (VRCFaceTracking closed). **Headset rollback:** `install-frameface.ps1 -HeadsetHost frame -Rollback`. **Without the PC script:** copy `FrameFace-0.6.0.tar.gz` to the headset, unpack it into a new folder such as `~/frameface-upload-manual` (never into `~/frameface`) and run `bash ~/frameface-upload-manual/install.sh` from there. Everything else about the headset side (config, endpoints, command line, probe, selftest, logs) is in [headset/frameface/README.md](headset/frameface/README.md).

| Headset | Module | Result |
|---|---|---|
| FrameEye Bridge, no FrameFace | 0.5.3 | eyes only |
| FrameEye Bridge, no FrameFace | 0.6.0 without `face_source` | identical to 0.5.3 |
| FrameEye Bridge + FrameFace | 0.6.0 + `face_source: babble_osc` | eyes + face |
| FrameEye Bridge + FrameFace | 0.5.3 | eyes; Baballonia gets images but nothing listens on 8888 |

### Baballonia setup

The labels are those of Baballonia's main branch; the Steam build may differ slightly.

1. Install Baballonia (Steam app 4091970 or its installer). Start it; finish or skip the onboarding (its firmware slides are for Babble hardware). Do **not** install the VRCFT-Babble module.
2. Home -> Face camera card -> **Face Camera Address**: paste `http://10.35.78.1:9023/stream/<token>` (printed by `install-frameface.ps1`, on the clipboard only with `-CopyUrl`; also `ssh frame "python3 -I ~/frameface/frameface_main.py url"`). Leave the left and right eye camera addresses empty. Use the printed IP URL (or put the headset's own host name or `<hostname>.local` in place of the IP): FrameFace answers any other DNS name for the headset with 404 and logs `W http: bad host peer=<ip>` (`ssh frame "journalctl --user -u frameface"`). Baballonia writes this full URL, token included, into its log on every reconnect: run `ssh frame "python3 -I ~/frameface/frameface_main.py url --new-token"` (and update the address) before you share Baballonia logs.
3. In **This is a...** choose **Wireless/IP Camera**. Do it after typing the address: changing the address resets the choice. (On Windows "Vive Facial Tracker" and "Normal Camera" also accept `http://`.)
4. Put the headset on and press **Start Camera**. The gray IR image appears in about 2 s. If the tracker is unplugged or the headset is off, the preview shows a black placeholder and real frames follow by themselves.
5. Optional: **Cropping** -> drag a box around lips and chin -> **Tracking**. Use **Image Rotation** if the tracker is mounted upside down; never use the flips.
6. App settings: **Auto Start with SteamVR** on, **Use GPU acceleration** on; OSC **IP Address** `127.0.0.1`, **Send Port** `8888`, **OSC Prefix** empty (the defaults). DFR off.
7. VRChat page: **Use Native VRC Eye Tracking** off (the eyes come from the Frame).
8. Optional **Calibration** page; keep auto-calibration off.

Check: a smile with only your own left mouth corner must raise `mouthSmileLeft` in Baballonia; if the right one rises, check the rotation; if the jaw looks sideways, try `"camera": "right"` in the headset config (`~/.config/frameface/config.json`, then restart the service: `ssh frame "python3 -I ~/frameface/frameface_main.py restart"`). No Windows Firewall rule is needed for the face path (Baballonia connects out to the headset, and its OSC stays on this PC); the existing inbound UDP 9021 rule for the eyes stays.

### Face settings of the module

These keys go into `%APPDATA%\VRCFaceTracking\SteamFrameEyeModule.json` next to the eye settings (all optional; restart VRCFaceTracking after a change):

| Key | Default | Valid | Meaning |
|---|---|---|---|
| `eye_enabled` | `true` | bool | `false`: do not claim the eyes |
| `face_source` | `"off"` when absent | `"babble_osc"`, `"off"` | `install-frameface.ps1` writes `"babble_osc"`; `"off"` behaves exactly like 0.5.3 |
| `face_port` | `8888` | 1024-65535 except 9000, 9001, 9021 | UDP port on 127.0.0.1 for Baballonia's OSC; the environment variable `SFFACE_PORT` overrides it |
| `face_min_rate_hz` | `10` | 1-60 | the face counts as live only at this many updates per second, so Baballonia's single start-up inference never reaches the avatar |
| `face_stale_ms` | `500` | 100-5000 | after this long without data the face relaxes |
| `face_decay_ms` | `300` | 0-5000 | fade-out to neutral |
| `face_fade_in_ms` | `150` | 0-2000 | fade-in when data starts |
| `smile_slant` | `false` | bool | also drive the mouth-corner slant from the smile (a full smile then shows 1.0 instead of 0.8 in VRChat's `v2/MouthSmile`) |
| `status_keeper` | `"off"` | `"off"`, `"face"`, `"all"` | optional status keeper: `face` sends `LipTrackingActive` and `ExpressionTrackingActive` to VRChat itself, `all` also `EyeTrackingActive` (see below) |
| `status_keeper_port` | `9000` | 1024-65535 | VRChat's OSC input port |

Baballonia's 45 expressions map to 55 Unified Expressions (57 with `smile_slant`) without extra gains; "left" is the wearer's left. The face path writes only mouth, jaw, cheek, nose-sneer and tongue shapes, never the eyes, brows or head.

### Troubleshooting the face path

`ssh frame "python3 -I ~/frameface/frameface_main.py status"` shows the state of the headset service (also `/status`, the probe, and `install-frameface.ps1 -HeadsetHost frame -Check`, which in addition checks the PC side: every installed module, `face_source`, UDP 8888 and Baballonia):

| State | Message | What to do |
|---|---|---|
| `disabled` | Face tracking is off | Run `python3 -I ~/frameface/frameface_main.py on` on the headset |
| `no_device` | Tracker not plugged in | Plug the VIVE Facial Tracker straight into the Frame's USB-C port (no hub, no charger) |
| `port_device_mode` | USB-C port is in charging/PC mode | Unplug the charger or PC cable, then plug in the tracker; it cannot run while the headset charges |
| `no_permission` | No permission to use the tracker | See "udev fallback" in [headset/frameface/README.md](headset/frameface/README.md) (one sudo step) |
| `busy` | Tracker used by another program | Stop `<owner>` (vftstream, Babble Frame Runtime, vive-face-init) |
| `standby` | Paused: headset not worn | Starts again when you put the headset on. "Worn" means the eye tracker's sample counter is moving; if you wear it and eye tracking is off, set `"gate_on_worn": false` in `~/.config/frameface/config.json` |
| `waiting_client` | Ready, waiting for Baballonia | Face Camera Address `<url>`, "This is a..." = Wireless/IP Camera |
| `settling` | Tracker settling (`<n>` s) | Wait; the tracker needs a pause after a stop |
| `starting` | Starting the tracker | Wait |
| `streaming` | Streaming `<f>` fps to `<n>` client(s) | Working. With a hub the hint says "connected through a hub; connect it directly if it drops out" |
| `recovering` | Tracker stalled, restarting | Wait |
| `test_pattern` | Tracker shows test bars, trying `<S>` | If it persists, unplug the tracker for 10 s |
| `dark_start` | Tracker image black, trying `<S>` | The IR light did not switch on with the last start sequence; FrameFace tries the next one by itself |
| `dark` | Tracker image is black | It was bright before: is the tracker facing your mouth, or is the lens covered? If it persists, unplug it for 10 s |
| `brownout` | Tracker lost power, IR lowered to `<L>` | Use a direct C-to-C connection; if it keeps happening unplug it for 5-10 min or reboot the headset |
| `wedged` | Tracker does not respond | Unplug the tracker for 10 s and plug it back in |
| `no_ir` | Tracker IR light did not switch on | Every start sequence gave a black image. Unplug the tracker for 10 s and plug it back in; if it stays black run `install-frameface.ps1 -ProbeActive` and report the bundle |
| `port_in_use` | Face stream port `<p>` is in use | Stop `<owner>` or set another `http_port` in `~/.config/frameface/config.json`, then `python3 -I ~/frameface/frameface_main.py restart`; Baballonia's address changes with it |
| `unsupported` | Unsupported tracker or system | Run `install-frameface.ps1 -Probe` and report the bundle |
| `library` | libturbojpeg is missing | Update SteamOS, then run the install again |

If the hint adds "reduced frame rate (python pixels)", the C helper is missing or failed its check: run the install again (it needs `gcc` on the headset).

More problems on the PC side:

- **Port 8888 in use:** `latest.log` shows `cannot bind 127.0.0.1:8888 (...); is the VRCFT-Babble module or another Baballonia listener running? Face tracking is off` and the claims line ends with `expression=False`. `-Check` names the program that holds UDP 8888. Stop or uninstall it and restart VRCFaceTracking (or move both ends: `face_port` here and Baballonia's **Send Port**).
- **VRCFT-Babble installed:** the log warns `the VRCFT-Babble module is installed`. It claims the expressions and binds 8888 itself. With VRCFaceTracking closed, remove `%APPDATA%\VRCFaceTracking\CustomLibs\360b014b-b57b-450f-8f12-9904618ff370`; this module includes the face path.
- **Charging blocks the tracker:** while the headset charges or is cabled to a PC, its USB-C port works as a device, not as a host, and cannot run the tracker; the state is `port_device_mode`. Unplug the charger, then plug the tracker in.
- **Expressions claimed by another module:** the log warns `expressions were already claimed by another module`. Remove the other expression module (VRCFT-Babble, the Steam Link module, SRanipal, VIVE Streaming); `-Check` lists every installed module and flags the known conflicts.
- **Claims lost (VRCFaceTracking 6.0):** the log warns `another module won this module's ... claim`: VRCFaceTracking gave that claim (eye, expression or both) to another module (the Steam Link module, VRCFT-Babble) and ignores this module's data for it, and the status keeper sends nothing for it. Remove the other module and restart VRCFaceTracking.
- **Baballonia shows the face but the avatar's mouth does not move:** `face_source` must be `"babble_osc"` (`-Check` prints it), the claims line must say `expression=True`, and Baballonia must send to `127.0.0.1:8888` with an empty OSC prefix. `python tools\send_test_face.py --port 8888` (with Baballonia closed) drives the module without the headset.
- **The face relaxes on its own:** 0.5 s after Baballonia stops sending (Baballonia stopped, the tracker unplugged, or the headset taken off) the module starts fading the face to neutral (reached after 0.8 s with the default `face_stale_ms` and `face_decay_ms`) and logs `no face data for ...`. The line is logged only after the face was shown and at most once a minute (a longer stop is reported when the minute is over); `face data resumed` follows such a line.

VRCFaceTracking 6.0 behaviour that affects the face (verified in its code and with its binaries):

- "Force Re-Initialize" (Settings -> Risky) stops all modules until VRCFaceTracking is restarted; it does not start them again. Restart VRCFaceTracking instead.
- `EyeTrackingActive`/`LipTrackingActive` (and `ExpressionTrackingActive`) are sent at most once per VRCFaceTracking run, always as true, and not again after an avatar or world change. Give them `Default = true` or `Saved` on your avatar, or try the optional status keeper (`"status_keeper": "face"` or `"all"`): it sends them to VRChat itself while the module is active and the data is live, and false once when it stops. With 6.0 it sends nothing while another module holds the claims. It is untested with VRChat.
- Switching a module off and on in VRCFaceTracking does not move its eye or expression claim to another module: restart VRCFaceTracking.

### Privacy

- The stream is your face. By default only the headset's link to this PC (`10.35.78.0/24`) and the headset itself may connect, and `/stream` needs the 128-bit token in the URL.
- Baballonia writes the token-bearing URL into its log on every reconnect. Before you share Baballonia logs, run `ssh frame "python3 -I ~/frameface/frameface_main.py url --new-token"` and update Baballonia's address. It restarts a running service itself, so the old token stops working; should that restart fail, run `ssh frame "python3 -I ~/frameface/frameface_main.py restart"`.
- `install-frameface.ps1 -CopyUrl` puts the URL on the clipboard, where Windows clipboard history and cloud clipboard sync may keep it.
- Allowing your home LAN in `allow_peers` (`-AllowPeer <this PC's address>/32`, e.g. `-AllowPeer 192.168.4.31/32`; the installer then also prints the headset's LAN URL) sends the unencrypted face stream (HTTP, token in the path) over that LAN.
- FrameFace makes no outbound connections, never writes images except in an active probe that you start, and logs neither the token nor request paths. [headset/frameface/README.md](headset/frameface/README.md) has the details, including that switching the IR LEDs off at a stop stays unverified until the active probe has read the registers back.

### Licences

Our code, FrameFace included, is MIT (`LICENSE`). Baballonia (Babble Software Distribution License 1.0, BSDL 1.0) is installed by you and never bundled; its face model `faceModel.onnx` is never redistributed. FrameFace implements the tracker protocol from MIT-licensed prior work; the notices and attributions are in [headset/frameface/NOTICE.md](headset/frameface/NOTICE.md).

## Steam Frame compared with a Quest Pro

A Quest Pro reaches VRCFaceTracking through Steam Link, which sends its eye and face data as OSC, and the Steam Link module ([ykeara/LinkFT](https://github.com/ykeara/LinkFT)) maps it. The Steam Frame's eye-tracking service exposes the following through this project:

| Data | Quest Pro via Steam Link | Steam Frame (eye server) |
|---|---|---|
| Gaze | one combined gaze point for both eyes | per-eye gaze (fused), per-eye gaze before fusion, fixation point, gaze covariance per eye |
| Eyelids | `EyesClosed` per eye, folded with `LidTightener` into openness | openness per eye, 0..1 |
| Eye widen, squint, brows, cheeks | yes, as face expressions (`UpperLidRaiser`, `LidTightener`, `InnerBrowRaiser`, `OuterBrowRaiser`, `BrowLowerer`, `CheekRaiser`) | no |
| Pupil size | no (the module pins 5 mm) | no (the module pins 5 mm) |
| Rate | depends on Steam Link | 90 Hz |

For VRCFaceTracking's eye data (gaze, openness, pupil) the Frame therefore provides everything a Quest Pro provides, and more gaze detail. What it lacks are the upper-face expressions; on a Quest Pro those come from its face cameras and reach VRCFaceTracking as expression data, which in a Frame setup comes from a mouth tracker instead (for example a VIVE Facial Tracker with FrameFace and Baballonia, see [above](#face-tracking-with-a-vive-facial-tracker-frameface)). The eight further values in the eye record are not pupil size: four follow the pupil position and four rise while the lids close.

The module writes its values the way the Steam Link module does for a Quest Pro: one combined gaze for both eyes as the angles `atan2(x, -z)` and `atan2(y, -z)` in radians, openness 0 (closed) to 1 (open), and pupil diameter 5 mm on a 0 to 10 mm range. Per-eye gaze is available as an option; with it the avatar's eyes converge on near objects, which the Frame measures at about 7° at a typical indoor distance.

## Smoothing and calibration

The following measurements are from the original v0.5.0 processing on an eight-minute Steam Frame recording (see *Tuning with a recording*). They are historical results, not measurements of the newer gaze filter:

| | Raw eye-server values | After the module |
|---|---|---|
| Gaze noise while looking at one spot (median std over 170 ms) | 0.24° | 0.13° |
| Gaze change between samples, eyes open (median) | 0.22° | 0.07° |
| Gaze jumps during blinks (90th percentile per sample) | 2.8° | 0 (held) |
| Delay after a fast eye movement (> 5°) until within 10 % of the target | | 20 ms median, 50 ms at the 90th percentile |
| Two-eye blinks shown fully closed on both eyes | 19 of 56 | 53 of 56 |
| Eyelid flicker from single-sample dropouts | frequent on the left eye | none |
| Eyes open, looking down more than 12°: median openness shown | left 0.75 to 0.82 | 0.98 |

What the module does with every sample:

- **Quality check:** the eye server reports a gaze covariance per eye: about 0.0004 with good tracking, and 0.02 to 0.05 when the eye is closed (the pupil is hidden), at the bottom of a blink or when the headset does not sit properly. Above 0.03 that eye's gaze is not used and the calibration pauses learning. Eyelids are never overridden, so a wink or eyes kept closed stay closed for as long as they are closed.
- **Eyelids, per eye:**
  - A three-sample median removes the single-sample dropouts the Frame produces.
  - The calibration learns each eye's open level (85th percentile of the recent values) and closed level (median bottom of the last 30 blinks, where a blink takes the lid at least 40 % below the open level and its bottom reaches at least 65 % below it). Since v0.5.3, shallower dips no longer count: looking down, squints and one eye sagging to about 0.5 with the SteamOS 0.4.3 eye server had pulled the closed level up to about 0.5, which showed relaxed eyes half shut. The two eyes differ a lot: in the recording the left eye closed to about 0.2 and opened to about 0.95, while the right eye stayed at 1.0 when open and only reached about 0.3 to 0.4 in a blink.
  - The open level is learned per band of vertical gaze, because the Frame reports lower openness when looking down or up; a Quest Pro's eyes-closed weight does not do that.
  - A One Euro filter smooths without delaying blinks, then snap zones map the bottom 15 % to fully closed and the top 10 % to fully open.
  - Blink sync closes the second eye when one eye is fully closed and the other is mostly closed. Real winks, where the other eye stays open, are left alone.
  - A closed eye is held closed for 80 ms so short blinks survive VRChat's slower network sync for other players.
  - **Linked eyelids** (`"eyelids": "linked"`): with the SteamOS 0.4.3 eye server, one eye's lid can read 0.15 to 0.5, or 0, for seconds while that eye is open. Linked mode shows both eyes at the more open eye's value, so blinks still close both eyes. Winks are recognised by the hidden pupil rather than the lids: the winking eye's gaze covariance rises above 0.015 (0.02 to 0.13 was measured) while the other eye's stays below 0.01, for 100 ms. The open eye then shows open even if the eye server reads its lid as closed too, which it often does during a wink. A lid misread keeps its pupil tracked (covariance about 0.001), and in recorded blinks neither pupil looked hidden while the other was tracked. One-sided dropouts on open eyes show the same pattern for less than 200 ms, so a wink needs 200 ms of it and, because the tracker pauses at the start of a closure, appears about 0.45 s after it starts. Winks do not count for two seconds after the tracker locks on, when one eye can settle before the other. It then shows for at least 0.6 s, because the tracker often reports a held right wink only briefly, and lasts until the lid reopens, the pupil is tracked again, or the other pupil is hidden too, for 100 ms. A "wink" longer than 5 s is treated as one eye not being tracked. To turn winks off, use the **Wink detection** switch in the headset's dashboard tab or set `"wink_detection": false`. The **Eyelid symmetry** switch next to it turns the matching off: each eyelid then follows its own reading, so a misread lid shows, and only stuck lids (below) are still handled.
  - **Stuck lids** (`lid_rescue`, needs linked eyelids): after the headset goes back on, the eye server can report both lids near 0 for minutes on open eyes, with the pupils tracked perfectly; moving the headset tends to end it. On six recordings this went with a large difference between two undocumented record values (`e1 - e3` above 0.4 when stuck, below 0.25 when working), which suggests it depends on how the headset sits. The module starts out ignoring the lids after every gap in the data, and also when both read closed for a second on eyes that do not look closed, until they read open again. Without them it uses what measurably still works: closed eyes turn the two gaze directions 21 to 28 degrees apart with a raised gaze covariance (under 2 degrees on open eyes while the tracker is half set up), a blink freezes the whole record for about 245 ms, and without a tracker lock (covariance above 0.04 on both eyes) the eyes show open and the gaze holds. Eyes closed while the tracker is half set up cannot be told from open ones, and show open. The periodic log line reports how much of the time this runs.
- **Gaze:** both eyes are averaged into one gaze (like a Quest Pro via Steam Link). Since v0.5.2, a three-sample median rejects isolated spikes before a One Euro filter (minimum cutoff 0.8 Hz, beta 5). The median delays a sustained movement by one sample, about 11 ms at 90 Hz. Only eyes that are clearly open and tracked steer: during a wink the open eye steers both, during blinks and with both eyes closed the gaze holds. Median history clears when the contributing eyes change, and all filters reset after a clock reset or reconnect.
- **Persistence:** the learned levels are saved to `%APPDATA%\VRCFaceTracking\SteamFrameEyeModule.calibration.json` every minute and when VRCFaceTracking closes, but only after about 30 s of reliable data and a few blinks per eye. A saved file with implausible values is ignored.

VRCFaceTracking's own "Data Filter" mutation smooths every value again with fixed settings; leave it off for the eyes. The module logs its calibration every five minutes, for example `lids left closed 0.19 open 0.95, right closed 0.39 open 1.00 (174/116 blinks); gaze held 7.8 %, low-confidence samples (closed eyes or poor fit) 2.4 %`.

**Direct VRChat OSC** also has gaze and eyelid smoothing in the headset app, enabled by default. Use its **Direct OSC smoothing** dashboard switch to change it; the choice is saved. The sender's binary SFEY feed remains raw so the VRCFaceTracking module applies its own processing once. Direct OSC does not include the module's learned eyelid calibration. See [headset/README.md](headset/README.md).

Run `dotnet run --project tools/SmoothingChecks -c Release` and `python -m unittest discover -s tools/tests -p test_osc_smoothing.py -v` for deterministic smoothing, blink, reconnect and actual loopback OSC checks. These synthetic checks do not establish how it feels on a wearer.

## The VRCFaceTracking module

### Build

Requires the .NET 10 SDK.

```
dotnet build -c Release
```

The output is `bin\Release\net10.0\SteamFrameEyeModule.dll`. The project references `VRCFaceTracking.SDK.dll`, `VRCFaceTracking.Core.dll` and `Microsoft.Extensions.Logging.Abstractions.dll` without copying them. It takes them from an installed VRCFaceTracking in the default Steam folder, otherwise from the reference copies of VRCFaceTracking 5.4.5 in `lib/vrcft-5.4.5`. To target another installation, pass `-p:VrcftDir=<VRCFaceTracking install folder>`.

The module only uses `ExtTrackingModule`, the `UnifiedTracking` eye fields and, for the face path, `UnifiedTracking.Data.Shapes`, which are unchanged in the VRCFaceTracking 6.0 sources. A 5.4.5 build loads in 6.0: version 0.5.3, built against `lib/vrcft-5.4.5`, was verified running on VRCFaceTracking 6.0 (the Steam build 6.0.0.0). Releases and the documented local build therefore use `-VrcftDir lib/vrcft-5.4.5`; a build against 6.0's assemblies would most likely not load in 5.4.x.

### Install

- **Module zip:** on Windows, `build.ps1` builds and packages, or `packaging/package.ps1 -Version <version>` packages a previously built DLL. On Linux, use `packaging/package.sh <version> <download-url-base> <page-url>`. Exit VRCFaceTracking, then unzip the zip into `%APPDATA%\VRCFaceTracking\CustomLibs\<ModuleId>\` (step 3 of [Install FrameFace and the module](#install-frameface-and-the-module) has the commands, with a backup of the old version), or use VRCFaceTracking's "install local module" (when it replaces a loaded older version it may need a second run). The `ModuleId` is fixed in `packaging/module-id.txt`, so a reinstall replaces the module instead of adding a second copy.
- **Single DLL:** copy `SteamFrameEyeModule.dll` into `%APPDATA%\VRCFaceTracking\CustomLibs\` and restart VRCFaceTracking.

Do not keep both variants installed, and keep backups of the module folder outside `CustomLibs`.

### Settings

All settings are optional, in `%APPDATA%\VRCFaceTracking\SteamFrameEyeModule.json`; restart VRCFaceTracking after a change. The environment variable `SFEYE_PORT` overrides the port. The keys of the face path (`face_source`, `face_port`, ...) are listed under [Face settings of the module](#face-settings-of-the-module).

```json
{ "port": 9021, "gaze": "combined", "blink_hold_ms": 80 }
```

| Key | Default | Meaning |
|---|---|---|
| `port` | 9021 | UDP port for the headset's packets |
| `gaze` | `combined` | `combined`: both eyes get the averaged gaze (Quest Pro style); `per_eye`: each eye its own gaze |
| `gaze_scale` | 1.0 | multiplier on the gaze angle in radians |
| `gaze_offset_x_deg`, `gaze_offset_y_deg` | 0 | constant gaze correction in degrees |
| `smoothing` | true | One Euro filters for gaze and eyelids |
| `gaze_median_filter` | true | reject single-sample gaze spikes before smoothing; set false to skip the one-sample median delay |
| `gaze_min_cutoff`, `gaze_beta` | 0.8, 5.0 | gaze filter: lower cutoff smooths more at rest, higher beta follows fast movements sooner |
| `lid_min_cutoff`, `lid_beta` | 4.0, 1.0 | eyelid filter |
| `calibration` | `auto` | `auto`: learn per eye; `fixed`: use `left_closed`, `left_open`, `right_closed`, `right_open`; `off`: raw values |
| `lid_gaze_compensation` | true | learn the open level per vertical gaze band |
| `closed_snap`, `open_snap` | 0.15, 0.9 | calibrated openness below / above these shows fully closed / open |
| `blink_sync`, `blink_sync_below` | true, 0.35 | close the second eye with the first when it is below this |
| `eyelids` | `per_eye` | `per_eye`: each eye its own openness; `linked`: both eyes show the more open eye, except during a recognised wink; `left` / `right`: both eyes follow that eye. With `linked`, `left` or `right`, the **Eyelid symmetry** switch in the headset's dashboard tab turns the matching off without a restart |
| `wink_detection` | true | with `linked` eyelids: let recognised winks through; false keeps both eyes always matched. The **Wink detection** switch in the headset's dashboard tab does the same without a restart |
| `lid_rescue` | true | with `linked` eyelids: when the headset's lid values stick near closed, take open and closed from gaze movement and pupil tracking until they recover |
| `blink_hold_ms` | 80 | keep a closed eye closed this long (0 disables) |
| `gaze_hold_below` | 0.6 | an eye's gaze is only used while its calibrated openness is at least this |
| `gaze_single_eye_above` | 0.8 | ...and at least this when it is the only eye steering (a wink) |
| `quality_limit` | 0.03 | an eye's gaze covariance above this keeps its gaze out and pauses learning (0 disables) |

To restart the calibration from scratch, delete `SteamFrameEyeModule.calibration.json` while VRCFaceTracking is closed.

### Troubleshooting

- **No module starts and no module writes log lines:** VRCFaceTracking resolves `VRCFaceTracking.ModuleProcess.exe` relative to its working directory. Start it through Steam or with its install folder as working directory.
- **Log location:** `%USERPROFILE%\Documents\VRCFaceTracking\latest.log`. Documents may be redirected to OneDrive.
- **Another module gets the eyes:** VRCFaceTracking 5.4.5 starts all modules at once and gives the eyes to whichever answers first, so with the Steam Link module installed the Frame's eyes may go to Steam Link in some sessions. Without a Quest streaming through Steam Link, the Steam Link module receives nothing and only logs connection errors; remove it when you use the Frame. In 5.4.5 the log line `Got init for module ... Eye: ... Expr: ...` prints the eye flag twice, so its `Expr` value is not the expression claim.
- **"no packets for 1.5 s":** the headset is asleep or not worn, FrameEye Bridge has sending switched off, or a firewall blocks UDP between headset and PC. `python tools\send_test_packets.py [host] [port] [count]` sends synthetic packets (a slow horizontal sweep with blinks) to test the PC side without a headset; loopback needs no firewall rule.
- **The log reports a high share of low-confidence samples while your eyes are open:** the headset does not sit properly on the face. Check the fit. Closed eyes count as low confidence too, so the share is also high after keeping your eyes closed.
- **Port clash:** Steam Link's face-tracking OSC output uses UDP 9015. The module defaults to 9021 to stay clear of it.
- **Face path:** see [Troubleshooting the face path](#troubleshooting-the-face-path).

### How VRCFaceTracking loads modules (5.4.x)

1. **Discovery** (`ModuleDataService`): two lists, concatenated in this order.
   - *Installed modules*: every sub-folder of `%APPDATA%\VRCFaceTracking\CustomLibs\` that holds a `module.json`; the DLL named by its `DllFileName` is loaded. This is what the in-app registry (`https://registry.vrcft.io/`) and "install local module" produce.
   - *Legacy modules*: every `*.dll` directly in `CustomLibs\`.
2. **One sandbox per module** (`UnifiedLibManager`): for each DLL VRCFaceTracking starts `VRCFaceTracking.ModuleProcess.exe --port <ipc-port> --module-path <dll> --parent-pid <pid>` in a hidden window.
3. **Loading inside the sandbox** (`ModuleProcess.ModuleAssembly`): the DLL goes into a collectible `AssemblyLoadContext`; modules referencing `VRCFaceTracking` or `VRCFaceTracking.Core` older than 5.0 are skipped; the first exported type derived from `ExtTrackingModule` is instantiated. `Logger` forwards to the main process.
4. **Capability negotiation** over the local IPC: `Supported` is queried, then `Initialize(eyeAvailable, expressionAvailable)` is called. All modules are started together and are usually all told that eyes and expressions are available; the first module whose init reply claims eyes becomes the eye source, likewise for expressions.
5. **Update loop**: the sandbox calls `Update()` in its own thread and, about every 10 ms, sends `UnifiedTracking.Data` back to the main process, which applies the eye part only from the eye source. `Update()` should block or sleep briefly; this module waits up to 20 ms for a new packet.
6. **Teardown**: `Teardown()` is called through IPC, then the sandbox is terminated.

A module zip must therefore contain `module.json` and the DLL at its root, and the module must target the same .NET major version as VRCFaceTracking (10.0 for 5.4.x).

## Tuning with a recording

1. On the headset, record raw eye-server samples while wearing it (read-only, 90 rows per second):

   ```
   python3 ~/FrameEye-Bridge/headset/tools/record_eyes.py ~/eyes.csv 300
   ```

2. Copy the CSV to the PC and replay it through the module's processing:

   ```
   dotnet run --project tools/ReplayProbe -c Release -- eyes.csv [settings.json] [out.csv]
   ```

   It prints the learned calibration minute by minute, gaze noise, eye-movement delay, blink capture and eyelid flicker before and after processing, using the same settings file format as the module. `out.csv` holds raw and processed values per sample.

## Packet format (little-endian, 96 bytes)

| Offset | Type | Field |
|---|---|---|
| 0 | char[4] | `SFEY` |
| 4 | u16 | version = 1 |
| 6 | u16 | flags, bit 0 = sample valid, bit 1 = wink detection off, bit 2 = eyelid symmetry off |
| 8 | u32 | sequence |
| 12 | f64 | sample_time (headset clock) |
| 20 | f32[3] | gaze direction left (x right, y up, -z forward) |
| 32 | f32[3] | gaze direction right |
| 44 | f32[3] | fixation point |
| 56 | f32 | openness left |
| 60 | f32 | openness right |
| 64 | f32[3] | gaze covariance diagonal left |
| 76 | f32[3] | gaze covariance diagonal right |
| 88 | f64 | send_time (headset monotonic clock) |

## Verified (2026-09-25 / 26 and 2026-11)

- VRCFaceTracking 5.4.5 (Steam build, .NET 10) loaded the module in its sandbox process and received the headset's packets over Wi-Fi. With the processing (v0.5.0) it learned both eyes' levels within a minute of wearing the headset (17 and 7 blinks), reported 0.3 % unreliable tracking and saved the calibration on exit.
- On the Steam Frame (SteamVR 2.17.10) the headset app streams its panel through an OpenGL ES texture (Mesa zink on Turnip, Adreno 750) without flicker, refreshes at up to 25 Hz while the tab is visible and uses no measurable CPU while it is hidden.
- VRCFaceTracking 6.0 (Steam build 6.0.0.0, 2026-11) loads and runs the 0.5.3 module built against `lib/vrcft-5.4.5`; the module claims the eyes (`Got init for module SteamFrameEyeModule. Eye: True Expr: False`). FrameFace and the 0.6.0 face path are so far tested with a simulated tracker and the checks in `tools/`; the probe (`install-frameface.ps1 -Probe`) records what a real tracker on the Frame does.

## Repository layout

- `SteamFrameEyeModule.cs`, `SteamFrameEyeModule.csproj`: the VRCFaceTracking module; `EyeProcessing.cs`: its smoothing, calibration and quality check. The face path is in `ModuleClaims.cs`, `FaceOsc.cs`, `FaceMapping.cs`, `FaceSettings.cs`, `FaceReceiver.cs` and `StatusKeeper.cs`, and `TestPorts.cs` is the port guard of the checks; these files do not use VRCFaceTracking, so `tools/FaceChecks` tests them without it.
- `headset/`: FrameEye Bridge, the headset app (standard-library Python). See [headset/README.md](headset/README.md). `headset/tools/record_eyes.py` records raw eye data.
- `headset/frameface/`: FrameFace, the headset service for a VIVE Facial Tracker (standard-library Python and a small C pixel helper), with its installer `install.sh`, the Windows wrapper `install-frameface.ps1`, the unit, the udev fallback rule, `release-files.txt`, `LICENSE` and `NOTICE.md`. See [headset/frameface/README.md](headset/frameface/README.md).
- `tools/ReplayProbe/`: replays a recording through `EyeProcessing.cs` and prints before/after statistics.
- `tools/SmoothingChecks/`, `tools/FaceChecks/`: checks of the eye processing and of the module's face path (OSC parsing, the Babble-to-Unified-Expressions mapping, the rate gate and fades, settings, claims). `tools/ModuleChecks/`: runs the module itself (`SteamFrameEyeModule.cs`) against stand-ins for the VRCFaceTracking types, so it needs no VRCFaceTracking; the eye rows, which bind UDP on all interfaces, run only with `MODULECHECKS_ALLOW_WILDCARD=1` (CI). `tools/ModuleHarness/`: an optional run of the whole module against the vendored VRCFaceTracking 5.4.5 (CI only); it always skips, because `lib/vrcft-5.4.5` lacks the VRCFaceTracking core's dependencies.
- `tools/send_test_face.py`, `tools/osc_monitor.py`: send a synthetic Baballonia-style face to a port, and print the OSC that arrives on a port (both need `--port`).
- `tools/tests/`: the Python tests (`python -m unittest discover -s tools/tests -v`); `ffutil.py` keeps every test away from the ports of software running on the PC, `ffsup.py` holds the compact supervisor the driver matrix runs on, the `test_ff_*` files cover FrameFace, the PC tools and repository hygiene.
- `tools/docker/`: runs the FrameFace tests on Linux/arm64 in a container; `tools/parity/` and `tools/OpenCvParity/`: pixel parity with OpenCvSharp (CI job).
- `lib/vrcft-5.4.5/`: compile-time reference assemblies (see its `NOTICE.md`).
- `build.ps1`: Windows build and packaging entry point.
- `packaging/`: release packaging (`module.json` template, fixed `ModuleId`, native PowerShell `package.ps1` and Linux `package.sh`; both build the module zip and both headset archives).
- `headset/install-overlay.ps1`: install the headset app from Windows over SSH; `headset/release-files.txt` lists the headset distribution files shared by both packagers (`headset/frameface/release-files.txt` likewise for FrameFace).
- `tools/send_test_packets.py`: loopback test sender for the module.
- `.github/workflows/build.yml`: builds, tests (Windows with both PowerShells and Python 3.x and 3.10, Ubuntu, arm64) and publishes GitHub releases.
- `.forgejo/workflows/release.yml`: release workflow of the primary Forgejo repository. A tag `vX.Y.Z` builds the module against `lib/vrcft-5.4.5` and publishes `SteamFrameEyeModule-X.Y.Z.zip`, `FrameEye-Bridge-X.Y.Z.tar.gz` (the headset app for the eyes), `FrameFace-X.Y.Z.tar.gz` (the headset service for a VIVE Facial Tracker) and `module.json` as a release.

The original project used a GitHub mirror of its Forgejo repository. This fork is maintained separately at [BowmanFox/SteamFrameEye](https://github.com/BowmanFox/SteamFrameEye); its GitHub Actions workflow builds and packages releases.

## License

MIT, see `LICENSE`. The shared-memory layout and the OSC conventions follow frameeyeosc (MIT, Copyright (c) 2026 konsti219). The reference assemblies in `lib/vrcft-5.4.5` are VRCFaceTracking (Apache License 2.0) and Microsoft.Extensions.Logging.Abstractions (MIT); see the `NOTICE.md` there. FrameFace is MIT as well; the notices of the MIT-licensed prior work it builds on are in [headset/frameface/NOTICE.md](headset/frameface/NOTICE.md). Baballonia (Babble Software Distribution License 1.0) is installed by the user and never bundled, and its `faceModel.onnx` is never redistributed.
