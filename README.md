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
- `headset/frameeye_osc.py` — the headset-side sender (copy to `/home/steamos/frameeye/`).
- `headset/frameeye-osc.service` — user systemd unit for it (`~/.config/systemd/user/`, then `XDG_RUNTIME_DIR=/run/user/1000 systemctl --user enable --now frameeye-osc`). Edit the `--raw` target to your PC's IP.
- `tools/send_test_packets.py` — sends synthetic SFEY packets to `127.0.0.1:9021` to exercise the module without a headset.

## Verified (2026-09-25)

VRCFaceTracking 5.4.5 (Steam build, .NET 10) loaded the module in its sandbox process, logged `Tracking initialized via SteamFrameEyeModule`, bound UDP 9021 and logged `first sample ... lids=(0.86,0.69)` from packets sent by the headset over Wi-Fi. Data only changes while the headset is worn.

## Troubleshooting

- **No module sandboxes start / no log lines from any module:** VRCFT resolves `VRCFaceTracking.ModuleProcess.exe` relative to its current directory. Start it via Steam or with the install folder as working directory.
- **Log location:** `%USERPROFILE%\Documents\VRCFaceTracking\latest.log` (Documents may be redirected to OneDrive).
- **"another module already claimed eye tracking":** a second eye module (for example the Steam Link module) is installed and won the race. Uninstall it in VRCFT's module registry or move its folder out of `CustomLibs`.
- **"no packets for 1.5 s":** headset asleep, not worn, sender stopped, or a firewall between headset and PC. Check with `python tools\send_test_packets.py` (loopback) and with `ss -u` / the service journal on the headset.
- **Port clash:** 9015 is used by Steam Link's face-tracking OSC; this module defaults to 9021. Change it in `%APPDATA%\VRCFaceTracking\SteamFrameEyeModule.json`.
