# FrameEye Bridge

Steam Frame eye tracking for VRCFaceTracking: per-eye gaze **and eyelid openness** from the Valve Steam Frame's built-in eye tracking, which the public OpenXR and OpenVR APIs do not expose.

The project has two parts:

- **Headset app** in [`headset/`](headset/README.md): FrameEye Bridge, a SteamVR dashboard app that runs on the Steam Frame and sends the eye data to the PC. It was called Steam Frame Eye before 2026-09-26.
- **VRCFaceTracking module** in this folder: `SteamFrameEyeModule`, which receives the data on the PC, cleans it up and feeds it into VRCFaceTracking the way the Steam Link module feeds a Quest Pro's. It is an ordinary external module that builds against VRCFaceTracking's released assemblies, so no VRCFaceTracking source checkout is needed.

## How it works

```
Steam Frame (headset)                                        PC
/dev/shm/eye-server.mmap --> FrameEye Bridge ---UDP 9021---> SteamFrameEyeModule.dll (in VRCFaceTracking)
(eye-tracking service)       (headset/, SteamVR app)         UnifiedTracking.Data.Eye.{Left,Right}.{Gaze,Openness}
```

- The headset's eye-tracking service publishes every sample (90 Hz) in the shared-memory object `/dev/shm/eye-server.mmap`. FrameEye Bridge waits on its sequence counter and sends one 96-byte `SFEY` packet per sample. It runs as the `steamos` user from the home directory, needs no root and leaves the read-only system untouched. The memory layout follows [konsti219/frameeyeosc](https://github.com/konsti219/frameeyeosc) (MIT).
- The module listens on UDP 9021, validates each packet, runs it through the processing described below and writes gaze and openness into VRCFaceTracking. Pupil diameter is pinned to 5 mm because the headset does not provide it.
- Data only flows while the headset is worn and SteamVR is out of standby. The module holds the last values and logs when packets stop and resume.

## Quick start

1. **PC:** build the module and install it into VRCFaceTracking (below). Allow inbound UDP 9021 for VRCFaceTracking's module process in Windows Firewall.
2. **Headset:** install FrameEye Bridge with the PC's address, as described in [headset/README.md](headset/README.md).
3. Start VRCFaceTracking and put the headset on. The eyelid calibration learns within about a minute of normal blinking and is kept for the next session.

## Steam Frame compared with a Quest Pro

A Quest Pro reaches VRCFaceTracking through Steam Link, which sends its eye and face data as OSC, and the Steam Link module ([ykeara/LinkFT](https://github.com/ykeara/LinkFT)) maps it. The Steam Frame's eye-tracking service exposes the following through this project:

| Data | Quest Pro via Steam Link | Steam Frame (eye server) |
|---|---|---|
| Gaze | one combined gaze point for both eyes | per-eye gaze (fused), per-eye gaze before fusion, fixation point, gaze covariance per eye |
| Eyelids | `EyesClosed` per eye, folded with `LidTightener` into openness | openness per eye, 0..1 |
| Eye widen, squint, brows, cheeks | yes, as face expressions (`UpperLidRaiser`, `LidTightener`, `InnerBrowRaiser`, `OuterBrowRaiser`, `BrowLowerer`, `CheekRaiser`) | no |
| Pupil size | no (the module pins 5 mm) | no (the module pins 5 mm) |
| Rate | depends on Steam Link | 90 Hz |

For VRCFaceTracking's eye data (gaze, openness, pupil) the Frame therefore provides everything a Quest Pro provides, and more gaze detail. What it lacks are the upper-face expressions; on a Quest Pro those come from its face cameras and reach VRCFaceTracking as expression data, which in a Frame setup comes from the mouth tracker instead (for example Project Babble). The eight further values in the eye record are not pupil size: four follow the pupil position and four rise while the lids close.

The module writes its values the way the Steam Link module does for a Quest Pro: one combined gaze for both eyes as the angles `atan2(x, -z)` and `atan2(y, -z)` in radians, openness 0 (closed) to 1 (open), and pupil diameter 5 mm on a 0 to 10 mm range. Per-eye gaze is available as an option; with it the avatar's eyes converge on near objects, which the Frame measures at about 7° at a typical indoor distance.

## Smoothing and calibration

Measured on an eight-minute recording from a Steam Frame (see *Tuning with a recording*):

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
  - The calibration learns each eye's open level (85th percentile of the recent values) and closed level (median bottom of the last 30 blinks, where a blink takes the lid at least 40 % below the open level). The two eyes differ a lot: in the recording the left eye closed to about 0.2 and opened to about 0.95, while the right eye stayed at 1.0 when open and only reached about 0.3 to 0.4 in a blink.
  - The open level is learned per band of vertical gaze, because the Frame reports lower openness when looking down or up; a Quest Pro's eyes-closed weight does not do that.
  - A One Euro filter smooths without delaying blinks, then snap zones map the bottom 15 % to fully closed and the top 10 % to fully open.
  - Blink sync closes the second eye when one eye is fully closed and the other is mostly closed. Real winks, where the other eye stays open, are left alone.
  - A closed eye is held closed for 80 ms so short blinks survive VRChat's slower network sync for other players.
- **Gaze:** both eyes are averaged into one gaze (like a Quest Pro via Steam Link) and smoothed with a One Euro filter tuned to let fast eye movements through. Only eyes that are clearly open and tracked steer: during a wink the open eye steers both, during blinks and with both eyes closed the gaze holds.
- **Persistence:** the learned levels are saved to `%APPDATA%\VRCFaceTracking\SteamFrameEyeModule.calibration.json` every minute and when VRCFaceTracking closes, but only after about 30 s of reliable data and a few blinks per eye. A saved file with implausible values is ignored.

VRCFaceTracking's own "Data Filter" mutation smooths every value again with fixed settings; leave it off for the eyes. The module logs its calibration every five minutes, for example `lids left closed 0.19 open 0.95, right closed 0.39 open 1.00 (174/116 blinks); gaze held 7.8 %, low-confidence samples (closed eyes or poor fit) 2.4 %`.

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

### Settings

All settings are optional, in `%APPDATA%\VRCFaceTracking\SteamFrameEyeModule.json`; restart VRCFaceTracking after a change. The environment variable `SFEYE_PORT` overrides the port.

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
| `gaze_min_cutoff`, `gaze_beta` | 1.0, 8.0 | gaze filter: lower cutoff smooths more at rest, higher beta follows fast movements sooner |
| `lid_min_cutoff`, `lid_beta` | 4.0, 1.0 | eyelid filter |
| `calibration` | `auto` | `auto`: learn per eye; `fixed`: use `left_closed`, `left_open`, `right_closed`, `right_open`; `off`: raw values |
| `lid_gaze_compensation` | true | learn the open level per vertical gaze band |
| `closed_snap`, `open_snap` | 0.15, 0.9 | calibrated openness below / above these shows fully closed / open |
| `blink_sync`, `blink_sync_below` | true, 0.35 | close the second eye with the first when it is below this |
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

- VRCFaceTracking 5.4.5 (Steam build, .NET 10) loaded the module in its sandbox process and received the headset's packets over Wi-Fi. With the processing (v0.5.0) it learned both eyes' levels within a minute of wearing the headset (17 and 7 blinks), reported 0.3 % unreliable tracking and saved the calibration on exit.
- On the Steam Frame (SteamVR 2.17.10) the headset app streams its panel through an OpenGL ES texture (Mesa zink on Turnip, Adreno 750) without flicker, refreshes at up to 25 Hz while the tab is visible and uses no measurable CPU while it is hidden.

## Repository layout

- `SteamFrameEyeModule.cs`, `SteamFrameEyeModule.csproj`: the VRCFaceTracking module; `EyeProcessing.cs`: its smoothing, calibration and quality check.
- `headset/`: FrameEye Bridge, the headset app (standard-library Python). See [headset/README.md](headset/README.md). `headset/tools/record_eyes.py` records raw eye data.
- `tools/ReplayProbe/`: replays a recording through `EyeProcessing.cs` and prints before/after statistics.
- `lib/vrcft-5.4.5/`: compile-time reference assemblies (see its `NOTICE.md`).
- `packaging/`: release packaging (`module.json` template, fixed `ModuleId`, `package.sh`).
- `tools/send_test_packets.py`: loopback test sender for the module.
- `.forgejo/workflows/release.yml`: release workflow of the primary Forgejo repository. A tag `vX.Y.Z` builds the module against `lib/vrcft-5.4.5` and publishes `SteamFrameEyeModule-X.Y.Z.zip`, `FrameEye-Bridge-X.Y.Z.tar.gz` (the headset app) and `module.json` as a release.

This GitHub repository is a mirror of that Forgejo repository (maintainers: remote `github`, `git push github main --tags`).

## License

MIT, see `LICENSE`. The shared-memory layout and the OSC conventions follow frameeyeosc (MIT, Copyright (c) 2026 konsti219). The reference assemblies in `lib/vrcft-5.4.5` are VRCFaceTracking (Apache License 2.0) and Microsoft.Extensions.Logging.Abstractions (MIT); see the `NOTICE.md` there.
