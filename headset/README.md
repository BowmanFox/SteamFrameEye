# FrameEye Bridge

A SteamVR dashboard application for the Valve Steam Frame that forwards the headset's built-in eye tracking to VRCFaceTracking on a PC: per-eye gaze **and eyelid openness**, which the public OpenXR and OpenVR APIs do not expose.

The receiving side is the Steam Frame Eye module for VRCFaceTracking in the top folder of this repository; the [main README](../README.md) covers building and installing it. As an alternative, the app can send the eye parameters straight to VRChat on the PC.

## How it works

```
Steam Frame (headset)                                        PC
/dev/shm/eye-server.mmap --> FrameEye Bridge ---UDP 9021---> Steam Frame Eye module in VRCFaceTracking
(eye-tracking service)       (frameeye_overlay.py)
                                            \---OSC 9000---> VRChat (optional, without VRCFaceTracking)
```

- The headset's eye-tracking service publishes every sample (90 Hz) in the shared-memory object `/dev/shm/eye-server.mmap`: per-eye gaze direction, fixation point and eyelid openness. The memory layout and the gaze mapping follow [konsti219/frameeyeosc](https://github.com/konsti219/frameeyeosc) (MIT).
- FrameEye Bridge (`frameeye_overlay.py`) waits on the service's sequence counter and sends one 96-byte `SFEY` UDP packet per sample to the PC. It also shows a tab in the SteamVR dashboard with the live state and the settings.
- Everything runs as the `steamos` user from the home directory. No root is needed and `steamos-readonly` stays enabled.
- Data only flows while the headset is worn and SteamVR is out of standby.

Tested on a Steam Frame with SteamVR 2.17.10 and the stock Python 3. The eye-server object is an undocumented interface and may change with SteamOS updates.

## Install

### From Windows

Run the PowerShell installer from the PC, using the headset's address and this PC's LAN address:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\headset\install-overlay.ps1 `
    -HeadsetHost 192.168.1.50 -PcAddress 192.168.1.10
```

Requires Windows OpenSSH Client and SSH enabled on the Steam Frame. Normal SSH authentication and host-key prompts apply. The default remote user is `steamos`; use `-User`, `-SshPort` and `-IdentityFile` for another SSH configuration. `-PcAddress` accepts `host` or `host:port` (default 9021). `-NoLaunch` registers without starting immediately; `-WhatIf` previews the operation without connecting. Hostnames and IPv4 addresses are supported, matching the sender's IPv4 UDP transport.

If you downloaded the headset release archive instead of cloning the repository, extract it on Windows and run its installer directly:

```powershell
New-Item -ItemType Directory -Force .\FrameEye-Bridge | Out-Null
tar -xzf .\FrameEye-Bridge-0.5.2.tar.gz -C .\FrameEye-Bridge
powershell -NoProfile -ExecutionPolicy Bypass -File .\FrameEye-Bridge\install-overlay.ps1 `
    -HeadsetHost 192.168.1.50 -PcAddress 192.168.1.10
```

This uploads only release files, normalizes shell-script line endings and runs the Linux installer **on the headset**. Windows needs no Bash or WSL. The dashboard and eye-data reader still run on the headset, where the eye server's shared memory exists. Existing headset settings are retained except for the PC address passed to the installer.

### From a Linux shell

Copy this repository to the headset and run the installer with the PC's address. From the folder that contains the clone:

```
scp -r FrameEye-Bridge steamos@<headset-ip>:
ssh steamos@<headset-ip> 'bash ~/FrameEye-Bridge/headset/install-overlay.sh <pc-ip>:9021'
ssh steamos@<headset-ip> 'python3 ~/frameeye/frameeye_overlay.py --status'
```

The installer copies the app to `~/frameeye`, disables the systemd unit variant if it is enabled, stops a running instance and runs `frameeye_overlay.py --install`. That writes `frameeye.vrmanifest` and `frameeye.conf.json`, registers the app with SteamVR (`~/.config/openvr/config/appconfig.json` and `vrappconfig/ottlabs.steamframeeye.vrappconfig`), enables auto-launch and starts it. From then on SteamVR starts the app with itself and asks it to quit at shutdown. To update, copy the new version over and run the installer again.

`python3 ~/frameeye/frameeye_overlay.py --uninstall` removes the registration and stops the app. The log is `~/frameeye/frameeye_overlay.log`.

On the PC, install the Steam Frame Eye module in VRCFaceTracking (see the [main README](../README.md)) and allow inbound UDP 9021 for VRCFaceTracking's module process in Windows Firewall.

The SteamVR app key is `ottlabs.steamframeeye`, from the app's first name, so installs made before the rename update in place.

## The dashboard tab

The tab is called "FrameEye Bridge". The upper card shows whether eye data is streaming, the sample rate and counters, and both eyes drawn live with gaze and eyelids. The footer shows the SteamVR version and `GL` for the texture path in use.

The settings card is used with the laser pointer. A click anywhere in a row acts on it.

- **Sending to PC** pauses or resumes the UDP output. The panel keeps showing the eye state while paused, so tracking itself can still be checked.
- **PC address** is a text field. A click opens the SteamVR keyboard, pre-filled with the current value. Enter `host` or `host:port`; the port defaults to 9021. The field mirrors the typing and takes the value on *Done*. An unusable entry is ignored and logged.
- **VRChat direct OSC (PC)** additionally sends the eye values as VRCFaceTracking "v2" avatar parameters straight to VRChat's OSC input on the PC, at port 9000 of the PC address. This is a way to run without VRCFaceTracking. It is not a standalone-headset mode, because VRChat does not run on the headset itself. It carries only the parameters listed below, so avatars that need VRCFaceTracking's derived or binary-encoded parameters still need VRCFaceTracking. With VRCFaceTracking running, leave it off, or both will write the same parameters.
- **Direct OSC smoothing** defaults to on. It removes single-sample gaze and eyelid spikes, smooths small gaze movements and follows larger movements faster. During blinks or poor tracking, gaze holds; the open eye steers during a wink. The eye preview shows the smoothed values when direct OSC is active. Turning this off restores the original raw direct OSC values. This switch does not affect the raw feed to VRCFaceTracking.

Direct OSC sends `/avatar/parameters/FT/EyeTrackingActive` (true) and, under `/avatar/parameters/FT/v2/`, `EyeLeftX`, `EyeLeftY`, `EyeRightX`, `EyeRightY`, `EyeX`, `EyeY`, `EyeLidLeft` and `EyeLidRight`. Gaze values are atan2(x, -z) · 4/π clamped to ±1, so ±45° maps to ±1. The prefix `/FT` can be changed with `--prefix`.

Direct OSC smooths per-eye gaze and the fixation-derived combined gaze in radians before mapping to the ±45° scale. A three-sample median followed by an adaptive One Euro filter reduces jitter; sustained movements gain one sample of median delay (about 11 ms at 90 Hz). A faster eyelid filter preserves short blinks and winks. Filters reset after a gap or a headset clock reset, when sending resumes, and when the smoothing switch or OSC target changes. Eyelid levels remain uncalibrated; the VRCFaceTracking module adds learned per-eye calibration and its own gaze mapping.

All settings are written to `~/frameeye/frameeye.conf.json` immediately and survive restarts. They take precedence over the manifest arguments. A later `--install --raw <pc>:<port>` overwrites the address in that file.

```json
{
  "raw": ["192.0.2.10:9021"],
  "target": null,
  "enabled": true,
  "osc_smoothing": true,
  "mouse_origin_bottom": true
}
```

`raw` lists the PCs that get `SFEY` packets, `target` is the VRChat OSC address or `null`, `enabled` is the sending switch, and `osc_smoothing` is the direct OSC filter switch (defaults to `true` for older configurations). `mouse_origin_bottom` is optional and defaults to `true`, because OpenVR reports laser-pointer coordinates with the origin at the bottom left. Add it with `false` if hovering highlights the wrong row, then restart the app.

## Command line

```
frameeye_overlay.py [sender options]            run the app (what SteamVR starts via frameeye_overlay.sh)
frameeye_overlay.py --install --raw <pc>:9021   record the settings, register with SteamVR, enable auto-launch, start
frameeye_overlay.py --install --no-launch ...   the same without starting it now
frameeye_overlay.py --uninstall                 remove the registration and stop
frameeye_overlay.py --status                    show config, registration and the running instance
```

Sender options: `--raw HOST:PORT` (repeatable), `--target HOST:PORT` for direct VRChat OSC, `--no-osc-smoothing` to opt out of the default filter, `--prefix`, `--poll-ms`, `--mmap`, `--verbose`. The overlay's saved `osc_smoothing` setting takes precedence over the command-line default. Overlay options: `--openvr-lib` (path to `libopenvr_api.so`, found automatically), `--log-file`, `--no-gl`.

### Without SteamVR integration

`frameeye_osc.py` is the sender on its own, usable from a shell:

```
python3 frameeye_osc.py --raw <pc-ip>:9021 [--target <pc-ip>:9000] [--verbose]
```

`frameeye-osc.service` runs it as a systemd user unit from `~/frameeye`, where the installer puts the files. Put the PC's address into its `ExecStart` line, copy it to `~/.config/systemd/user/` and run `systemctl --user enable --now frameeye-osc.service`. Use either the unit or the overlay app, not both; the installer disables the unit.

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

## Known issues

- **Do not use `--no-gl`.** The panel is normally streamed into one persistent OpenGL ES texture (`gl_texture.py`, EGL surfaceless on Mesa zink) and handed to SteamVR with `SetOverlayTexture`. With `--no-gl`, or if the GL setup fails, the app falls back to `SetOverlayRaw`. On the Steam Frame with SteamVR 2.17.10, vrcompositor has been observed to crash with SIGBUS shortly after a client that used `SetOverlayRaw` exits, and gamescope can then restart in a loop. If the footer shows `raw` instead of `GL`, check the log for `GL texture path unavailable`.
- **Empty dashboard tab, log says `SteamVR could not share the panel texture`.** SteamVR could not create the image the panel is shown with. On the Steam Frame this happened once the compositor had run out of shared-resource slots (`Ran out of IPC SharedObjectManager entries` in the system journal), used up by another overlay that reloads its image from a file. SteamVR's client library crashes on its next call after that error, so the app stops talking to SteamVR: eye data keeps flowing, the tab stays empty, and the app reconnects by itself after SteamVR has been restarted.
- The eye-server layout is not a public interface. If a SteamOS update changes it, the tab stays on "waiting for the eye tracking server" or shows implausible values.

## Files

- `frameeye_overlay.py`: the SteamVR app: sender in-process, dashboard tab, settings, registration.
- `frameeye_osc.py`: the sender (`SenderRuntime`) and its stand-alone command line.
- `gl_texture.py`: EGL/GLES texture used to stream the panel.
- `openvr_min.py`: minimal ctypes binding to `libopenvr_api.so`. It and `bitmap_font.py` are shared with the FCAM overlay of [CyrusOtter/Babble-Bridge](https://github.com/CyrusOtter/Babble-Bridge).
- `install-overlay.sh`, `frameeye_overlay.sh`: installer and the launcher SteamVR runs.
- `frameeye-osc.service`: systemd user unit for the stand-alone sender.
- `icon.png`, `tools/gen_icon.py`: the dashboard icon and its generator.
- `tools/record_eyes.py`: records raw eye-server samples to CSV for tuning the module's processing (see the main README).

## License

MIT, see `LICENSE`. The shared-memory layout and the OSC contract follow frameeyeosc (MIT, Copyright (c) 2026 konsti219).
