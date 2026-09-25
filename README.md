# SteamFrameEyeModule

VRCFaceTracking module for the Valve Steam Frame's built-in eye tracking: per-eye gaze **and eyelid openness**, which the public OpenXR/OpenVR APIs do not expose.

## How it works

```
Steam Frame                                   PC
/dev/shm/eye-server.mmap  --> frameeye_osc.py --raw <pc>:9021 --UDP-->  SteamFrameEyeModule.dll (in VRCFT)
(eye-tracking server)         (user service on the headset)             UnifiedTracking.Data.Eye.{Left,Right}.{Gaze,Openness}
```

- The headset side is `frameeye_osc.py` (stdlib Python, runs as a user systemd unit in `/home/steamos`, no root, read-only system untouched). It polls the sequence counter of the eye-tracking server's shared-memory object and sends one 96-byte `SFEY` packet per sample. Layout and gaze mapping follow [konsti219/frameeyeosc](https://github.com/konsti219/frameeyeosc) (MIT).
- This module listens on UDP 9021, validates the packet and writes gaze (atan2(x, -z)·4/π, clamped to ±1) and openness into VRCFT. Pupil diameter is pinned to 5 mm because the headset does not provide it.
- Data only flows while the headset is worn and SteamVR is out of standby; the module holds the last values and logs when packets stop and resume.

## Build

Requires the .NET 10 SDK and an installed VRCFaceTracking 5.4.x (the module references its `VRCFaceTracking.SDK.dll`, `VRCFaceTracking.Core.dll` and `Microsoft.Extensions.Logging.Abstractions.dll`; override the location with `-p:VrcftDir=...`).

```
dotnet build -c Release
copy bin\Release\net10.0\SteamFrameEyeModule.dll %APPDATA%\VRCFaceTracking\CustomLibs\
```

## Configure

Optional `%APPDATA%\VRCFaceTracking\SteamFrameEyeModule.json`:

```json
{ "port": 9021 }
```

or environment variable `SFEYE_PORT`. Windows Firewall must allow inbound UDP on that port for VRCFT's module process.

## Packet format (little-endian, 96 bytes)

| Offset | Type | Field |
|---|---|---|
| 0 | char[4] | `SFEY` |
| 4 | u16 | version = 1 |
| 6 | u16 | flags, bit 0 = sample valid |
| 8 | u32 | sequence |
| 12 | f64 | sample_time (headset clock) |
| 20 | f32[3] | gaze direction left (x right, y up, -z forward) |
| 32 | f32[3] | gaze direction right |
| 44 | f32[3] | fixation point |
| 56 | f32 | openness left |
| 60 | f32 | openness right |
| 64 | f32[3] | gaze covariance diag left |
| 76 | f32[3] | gaze covariance diag right |
| 88 | f64 | send_time (headset monotonic) |

## License

MIT for this module. The shared-memory layout and OSC conventions are from frameeyeosc (MIT, Copyright (c) 2026 konsti219).

## Repository layout

- `SteamFrameEyeModule.cs` / `.csproj` — the VRCFT module (PC side).
- `headset/` — the headset side, a **SteamVR overlay application** (standard-library Python):
  - `frameeye_overlay.py` runs the sender in-process, registers itself with the Frame's SteamVR (`--install`), is auto-launched by SteamVR at startup and quits with it, and shows a dashboard tab with the live state (streaming / idle / no eye server, sample rate, targets, per-eye gaze and eyelids drawn as two eyes).
  - `frameeye_osc.py` — the sender itself (`SenderRuntime`), also usable stand-alone from a shell or through the systemd unit `frameeye-osc.service`.
  - `openvr_min.py` (ctypes binding to `libopenvr_api.so`) and `bitmap_font.py` are shared with the FCAM overlay in the Baballonia `steamframe/overlay` folder.
  - `install-overlay.sh <pc-ip>[:port]` copies everything to `~/frameeye`, retires the systemd unit if enabled, writes `frameeye.vrmanifest` with the sender options, registers it (`appconfig.json` + `vrappconfig/ottlabs.steamframeeye.vrappconfig` and, if SteamVR runs, `AddApplicationManifest` + `SetApplicationAutoLaunch`) and starts it through SteamVR.
  - `tools/gen_icon.py` regenerates `icon.png`.
- `lib/vrcft-5.4.5/` — compile-time reference assemblies (see NOTICE.md), `packaging/` — release packaging, `tools/send_test_packets.py` — loopback test for the module.

### Headset install (SteamVR overlay)

```
scp -r headset steamos@<headset-ip>:~/frameeye-src
ssh steamos@<headset-ip> 'bash ~/frameeye-src/install-overlay.sh <pc-ip>:9021'
ssh steamos@<headset-ip> 'python3 ~/frameeye/frameeye_overlay.py --status'
```

No root, `steamos-readonly` untouched. App key `ottlabs.steamframeeye`; `--uninstall` removes the registration and stops it. The tab appears in the SteamVR dashboard as "Steam Frame Eye". Log: `~/frameeye/frameeye_overlay.log`.

## Verified (2026-09-25)

VRCFaceTracking 5.4.5 (Steam build, .NET 10) loaded the module in its sandbox process, logged `Tracking initialized via SteamFrameEyeModule`, bound UDP 9021 and logged `first sample ... lids=(0.86,0.69)` from packets sent by the headset over Wi-Fi. Data only changes while the headset is worn.

## Troubleshooting

- **No module sandboxes start / no log lines from any module:** VRCFT resolves `VRCFaceTracking.ModuleProcess.exe` relative to its current directory. Start it via Steam or with the install folder as working directory.
- **Log location:** `%USERPROFILE%\Documents\VRCFaceTracking\latest.log` (Documents may be redirected to OneDrive).
- **"another module already claimed eye tracking":** a second eye module (for example the Steam Link module) is installed and won the race. Uninstall it in VRCFT's module registry or move its folder out of `CustomLibs`.
- **"no packets for 1.5 s":** headset asleep, not worn, sender stopped, or a firewall between headset and PC. Check with `python tools\send_test_packets.py` (loopback) and with `ss -u` / the service journal on the headset.
- **Port clash:** 9015 is used by Steam Link's face-tracking OSC; this module defaults to 9021. Change it in `%APPDATA%\VRCFaceTracking\SteamFrameEyeModule.json`.

## How VRCFaceTracking loads modules (5.4.x, from the decompiled binaries)

1. **Discovery** (`ModuleDataService`): two lists, concatenated in this order.
   - *Installed modules*: every sub-folder of `%APPDATA%\VRCFaceTracking\CustomLibs\` that holds a `module.json`; the DLL named by its `DllFileName` is the load path. This is what the in-app registry (`https://registry.vrcft.io/`) and "install local module" produce.
   - *Legacy modules*: every `*.dll` directly in `CustomLibs\`.
2. **One sandbox per module** (`UnifiedLibManager`): for each DLL VRCFT starts `VRCFaceTracking.ModuleProcess.exe --port <ipc-port> --module-path <dll> --parent-pid <pid>` (hidden window). The path to that exe is resolved **relative to VRCFT's working directory**, so VRCFT must be started from its install folder (Steam does this).
3. **Loading inside the sandbox** (`ModuleProcess.ModuleAssembly`): the DLL goes into a collectible `AssemblyLoadContext`; modules referencing `VRCFaceTracking`/`VRCFaceTracking.Core` older than 5.0 are skipped; the first exported type whose base class is `ExtTrackingModule` is instantiated with `Activator.CreateInstance`. `Logger` is a proxy that forwards to the main process.
4. **Handshake and capability negotiation** over the local IPC: `Supported` is queried, then `Initialize(eyeAvailable, expressionAvailable)` is called with `eyeAvailable`/`expressionAvailable` = "no earlier module has claimed that data type yet". Modules are initialized in discovery order, so an earlier module wins the eyes; this module returns `(false, false)` in that case.
5. **Update loop**: the sandbox calls `Update()` in its own thread and, on each host `EventUpdate`, serializes `UnifiedTracking.Data` (eye, expressions, head) back to the main process (`ReplyUpdatePacket`), which applies the eye part only from the module that owns eyes. `Update()` should block or sleep briefly; this module waits up to 20 ms for a new packet.
6. **Teardown**: `Teardown()` is invoked through IPC, then the sandbox receives SIGTERM.

Consequences for packaging: a release zip must contain `module.json` + the DLL at its root (`InstallLocalModule` extracts it, requires `module.json`, and moves the folder to `CustomLibs\<ModuleId>\`), and the module must target the same .NET major as VRCFT (10.0 for 5.4.x).

## Releases (Forgejo Actions)

`.forgejo/workflows/release.yml` runs on the org `ubuntu-latest` runner: push a tag `vX.Y.Z` → `dotnet build` against the vendored reference assemblies in `lib/vrcft-5.4.5` → `packaging/package.sh` → Forgejo release with
`SteamFrameEyeModule-X.Y.Z.zip` (module.json + DLL, VRCFT "install local module" format), `SteamFrameEye-headset-X.Y.Z.tar.gz` (sender + unit) and `module.json`. A manual dispatch without a tag only builds and packages. The `ModuleId` is fixed in `packaging/module-id.txt` so re-installs replace rather than duplicate the module.

## Install options

- **Release zip:** VRCFT → module registry page → install local module → pick the zip; or unzip it into `%APPDATA%\VRCFaceTracking\CustomLibs\<ModuleId>\`.
- **Developer:** copy `bin\Release\net10.0\SteamFrameEyeModule.dll` into `CustomLibs\` (legacy root-DLL loading). Do not keep both variants installed.
