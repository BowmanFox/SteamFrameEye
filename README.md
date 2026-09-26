# FrameEye Bridge

Steam Frame eye tracking for VRCFaceTracking: per-eye gaze **and eyelid openness** from the Valve Steam Frame's built-in eye tracking, which the public OpenXR and OpenVR APIs do not expose.

The project has two parts:

- **Headset app** in [`headset/`](headset/README.md): FrameEye Bridge, a SteamVR dashboard app that runs on the Steam Frame and sends the eye data to the PC. It was called Steam Frame Eye before 2026-09-26.
- **VRCFaceTracking module** in this folder: `SteamFrameEyeModule`, which receives the data on the PC and feeds it into VRCFaceTracking. It is an ordinary external module that builds against VRCFaceTracking's released assemblies, so no VRCFaceTracking source checkout is needed.

## How it works

```
Steam Frame (headset)                                        PC
/dev/shm/eye-server.mmap --> FrameEye Bridge ---UDP 9021---> SteamFrameEyeModule.dll (in VRCFaceTracking)
(eye-tracking service)       (headset/, SteamVR app)         UnifiedTracking.Data.Eye.{Left,Right}.{Gaze,Openness}
```

- The headset's eye-tracking service publishes every sample (90 Hz) in the shared-memory object `/dev/shm/eye-server.mmap`. FrameEye Bridge waits on its sequence counter and sends one 96-byte `SFEY` packet per sample. It runs as the `steamos` user from the home directory, needs no root and leaves the read-only system untouched. The memory layout and the gaze mapping follow [konsti219/frameeyeosc](https://github.com/konsti219/frameeyeosc) (MIT).
- The module listens on UDP 9021, validates each packet and writes gaze and openness into VRCFaceTracking. Gaze is mapped as atan2(x, -z) · 4/π and clamped to ±1. Pupil diameter is pinned to 5 mm because the headset does not provide it.
- Data only flows while the headset is worn and SteamVR is out of standby. The module holds the last values and logs when packets stop and resume.

## Quick start

1. **PC:** build the module and install it into VRCFaceTracking (below). Allow inbound UDP 9021 for VRCFaceTracking's module process in Windows Firewall.
2. **Headset:** install FrameEye Bridge with the PC's address, as described in [headset/README.md](headset/README.md).
3. Start VRCFaceTracking and put the headset on.

## The VRCFaceTracking module

### Build

Requires the .NET 10 SDK.

```
dotnet build -c Release
```

The output is `bin\Release\net10.0\SteamFrameEyeModule.dll`. The project references `VRCFaceTracking.SDK.dll`, `VRCFaceTracking.Core.dll` and `Microsoft.Extensions.Logging.Abstractions.dll` without copying them. It takes them from an installed VRCFaceTracking in the default Steam folder, otherwise from the reference copies of VRCFaceTracking 5.4.5 in `lib/vrcft-5.4.5`. To target another installation, pass `-p:VrcftDir=<VRCFaceTracking install folder>`.

The module only uses `ExtTrackingModule` and the `UnifiedTracking` eye fields, which are unchanged in the VRCFaceTracking 6.0 sources, so a 5.4.5 build should also load in 6.x. This has not been tested yet.

### Install

- **Module zip:** `packaging/package.sh <version> <download-url-base> <page-url>` builds `SteamFrameEyeModule-<version>.zip` with `module.json` and the DLL. Install it with VRCFaceTracking's "install local module", or unzip it into `%APPDATA%\VRCFaceTracking\CustomLibs\<ModuleId>\`. The `ModuleId` is fixed in `packaging/module-id.txt`, so a reinstall replaces the module instead of adding a second copy.
- **Single DLL:** copy `SteamFrameEyeModule.dll` into `%APPDATA%\VRCFaceTracking\CustomLibs\` and restart VRCFaceTracking.

Do not keep both variants installed.

### Configure

The port can be changed in `%APPDATA%\VRCFaceTracking\SteamFrameEyeModule.json`:

```json
{ "port": 9021 }
```

The environment variable `SFEYE_PORT` overrides the file.

### Troubleshooting

- **No module starts and no module writes log lines:** VRCFaceTracking resolves `VRCFaceTracking.ModuleProcess.exe` relative to its working directory. Start it through Steam or with its install folder as working directory.
- **Log location:** `%USERPROFILE%\Documents\VRCFaceTracking\latest.log`. Documents may be redirected to OneDrive.
- **"another module already claimed eye tracking":** a second eye module, for example the Steam Link module, is installed and was initialized first. Disable or remove it to use the Steam Frame's eyes.
- **"no packets for 1.5 s":** the headset is asleep or not worn, FrameEye Bridge has sending switched off, or a firewall blocks UDP between headset and PC. `python tools\send_test_packets.py [host] [port] [count]` sends synthetic packets (a slow horizontal sweep with blinks) to test the PC side without a headset; loopback needs no firewall rule.
- **Port clash:** Steam Link's face-tracking OSC output uses UDP 9015. The module defaults to 9021 to stay clear of it.

### How VRCFaceTracking loads modules (5.4.x)

1. **Discovery** (`ModuleDataService`): two lists, concatenated in this order.
   - *Installed modules*: every sub-folder of `%APPDATA%\VRCFaceTracking\CustomLibs\` that holds a `module.json`; the DLL named by its `DllFileName` is loaded. This is what the in-app registry (`https://registry.vrcft.io/`) and "install local module" produce.
   - *Legacy modules*: every `*.dll` directly in `CustomLibs\`.
2. **One sandbox per module** (`UnifiedLibManager`): for each DLL VRCFaceTracking starts `VRCFaceTracking.ModuleProcess.exe --port <ipc-port> --module-path <dll> --parent-pid <pid>` in a hidden window.
3. **Loading inside the sandbox** (`ModuleProcess.ModuleAssembly`): the DLL goes into a collectible `AssemblyLoadContext`; modules referencing `VRCFaceTracking` or `VRCFaceTracking.Core` older than 5.0 are skipped; the first exported type derived from `ExtTrackingModule` is instantiated. `Logger` forwards to the main process.
4. **Capability negotiation** over the local IPC: `Supported` is queried, then `Initialize(eyeAvailable, expressionAvailable)` is called, where each flag means that no earlier module has claimed that data yet. An earlier module therefore wins the eyes; this module returns `(false, false)` in that case.
5. **Update loop**: the sandbox calls `Update()` in its own thread and, on each host update, sends `UnifiedTracking.Data` back to the main process. `Update()` should block or sleep briefly; this module waits up to 20 ms for a new packet.
6. **Teardown**: `Teardown()` is called through IPC, then the sandbox is terminated.

A module zip must therefore contain `module.json` and the DLL at its root, and the module must target the same .NET major version as VRCFaceTracking (10.0 for 5.4.x).

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
| 64 | f32[3] | gaze covariance diagonal left |
| 76 | f32[3] | gaze covariance diagonal right |
| 88 | f64 | send_time (headset monotonic clock) |

## Verified (2026-09-25 / 26)

- VRCFaceTracking 5.4.5 (Steam build, .NET 10) loaded the module in its sandbox process, logged `Tracking initialized via SteamFrameEyeModule`, bound UDP 9021 and logged `first sample ... lids=(0.86,0.69)` from packets the headset sent over Wi-Fi.
- On the Steam Frame (SteamVR 2.17.10) the headset app streams its panel through an OpenGL ES texture (Mesa zink on Turnip, Adreno 750) without flicker, refreshes at up to 25 Hz while the tab is visible and uses no measurable CPU while it is hidden.

## Repository layout

- `SteamFrameEyeModule.cs`, `SteamFrameEyeModule.csproj`: the VRCFaceTracking module.
- `headset/`: FrameEye Bridge, the headset app (standard-library Python). See [headset/README.md](headset/README.md).
- `lib/vrcft-5.4.5/`: compile-time reference assemblies (see its `NOTICE.md`).
- `packaging/`: release packaging (`module.json` template, fixed `ModuleId`, `package.sh`).
- `tools/send_test_packets.py`: loopback test sender for the module.
- `.forgejo/workflows/release.yml`: release workflow of the primary Forgejo repository. A tag `vX.Y.Z` builds the module against `lib/vrcft-5.4.5` and publishes `SteamFrameEyeModule-X.Y.Z.zip`, `FrameEye-Bridge-X.Y.Z.tar.gz` (the headset app) and `module.json` as a release.

This GitHub repository is a mirror of that Forgejo repository (maintainers: remote `github`, `git push github main --tags`).

## License

MIT, see `LICENSE`. The shared-memory layout and the OSC conventions follow frameeyeosc (MIT, Copyright (c) 2026 konsti219). The reference assemblies in `lib/vrcft-5.4.5` are VRCFaceTracking (Apache License 2.0) and Microsoft.Extensions.Logging.Abstractions (MIT); see the `NOTICE.md` there.
