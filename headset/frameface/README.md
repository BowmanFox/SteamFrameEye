# FrameFace

FrameFace lets a **VIVE Facial Tracker** (USB `0bb4:0321`, the lip tracker sold for Index, Vive
Pro Eye and Vive Pro 2) plugged into a **Valve Steam Frame** drive mouth tracking in VRChat. It is
a small service on the headset that reads the tracker over USB and serves its camera image as an
MJPEG stream. Baballonia on your Windows PC turns the image into face expressions and sends them
over OSC to the SteamFrameEye VRCFaceTracking module, which also keeps receiving the eyes from
FrameEye Bridge.

```
Steam Frame (user steamos, no root)                                       Windows PC
VFT 0bb4:0321 --USB--> FrameFace (systemd user unit, Python 3 + ctypes usbfs)
                       frame assembly -> health check -> Baballonia-exact pixels
                       -> grayscale JPEG -> MJPEG HTTP on TCP 9023 (allow-list + token)
                            ^--------- the PC connects to the headset ---------- Baballonia ("Wireless/IP Camera")
                                                                                  OSC -> 127.0.0.1:8888
FrameEye Bridge (unchanged) -- eyes, UDP 9021 ----------------------------------> SteamFrameEyeModule 0.6.0
                                                                                  -> VRCFaceTracking -> VRChat
```

What runs where:

- `~/frameface/` on the headset: this folder (the `FrameFace-<version>.tar.gz` release), plus
  `lib/libffproc.so` and `lib/abi_dump`, which `install.sh` compiles on the headset with `gcc`.
- `frameface.service`, a systemd **user** unit (`~/.config/systemd/user/`). No root, no sudo.
- The service only listens; it never connects anywhere. It runs the tracker only while
  Baballonia is connected and the headset is worn, and turns the tracker off otherwise.

## Install

On the PC, in the repository folder (Windows PowerShell 5.1 or PowerShell 7; SSH to the headset
must work, e.g. `ssh frame`):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 -VrcftDir .\lib\vrcft-5.4.5
powershell -NoProfile -ExecutionPolicy Bypass -File .\headset\frameface\install-frameface.ps1 -HeadsetHost frame
```

`install-frameface.ps1` uploads exactly the files of `release-files.txt` to
`~/frameface-upload-<id>`, runs `install.sh` there, removes the upload, prints the selftest result
and the Baballonia URL, and sets `"face_source": "babble_osc"` in
`%APPDATA%\VRCFaceTracking\SteamFrameEyeModule.json` if that key is absent (with a backup
`SteamFrameEyeModule.json.bak-<yyyyMMdd-HHmmss>` only when it writes). A deliberate
`"face_source": "off"` is kept unless you add `-EnableFace`. Then install the module with
VRCFaceTracking closed and set up Baballonia, as the script's "next steps" and the main README
describe.

Options of `install-frameface.ps1`:

| Option | Effect |
|---|---|
| `-HeadsetHost <name or IP>` | required; `-User` (default `steamos`), `-SshPort`, `-IdentityFile` as for `ssh` |
| `-NoStart` | install and enable the unit without starting it; the next steps give the command that starts it from the PC |
| `-NewToken` | generate a new stream token (Baballonia's address changes) |
| `-AllowPeer <CIDR>[,<CIDR>...]` | also accept these networks: one or more IPv4 CIDRs, comma-separated, e.g. `192.168.4.31/32` for your PC on the home LAN; the printed URLs then include the headset's LAN URL (see `url` and Privacy) |
| `-CopyUrl` | also put the URL on the clipboard; of two printed URLs, the next steps and `-CopyUrl` use the one this PC reaches: the Frame-link URL if the PC has an IPv4 address in `10.35.78.0/24`, else the LAN URL |
| `-EnableFace` | set `face_source` to `babble_osc` even if it is `"off"` |
| `-SkipModuleSettings` | do not touch `SteamFrameEyeModule.json` |
| `-VrcftConfigDir`, `-VrcftInstallDir` | other VRCFaceTracking folders |
| `-Probe` | read-only probe; the report is copied to `.\frameface-probe-<ts>\` |
| `-ProbeActive [-ProbeArgs '...'] [-IncludeImages]` | active probe (IR on, the service stops for 1-3 minutes; asks first) |
| `-Check` | read-only check of the PC side (VRCFaceTracking version, every installed module, `face_source`, UDP 8888) and of the headset service |
| `-Uninstall [-Purge]` | remove FrameFace from the headset and `face_source` from the module settings |
| `-Rollback` | go back to the previously installed FrameFace version |
| `-WhatIf` | show what would happen; no SSH and no file is written |

### Manual install on the headset

Without the PC script, copy `FrameFace-<version>.tar.gz` to the headset and unpack it into a
**new** folder, never into `~/frameface` (`install.sh` refuses to run from `~/frameface`,
`~/frameface.prev` or `~/frameface.new`):

```bash
mkdir -p ~/frameface-upload-manual && tar xzf FrameFace-<version>.tar.gz -C ~/frameface-upload-manual
bash ~/frameface-upload-manual/install.sh
rm -rf ~/frameface-upload-manual
```

`bash install.sh [--no-start] [--new-token] [--reset-profile] [--allow-peer CIDR]...` does, as the
normal user and only under `$HOME`:

1. checks the system (aarch64, `python3` >= 3.10, `systemctl --user`, `gcc`);
2. copies the release files to `~/frameface.new` and compiles `lib/libffproc.so` and
   `lib/abi_dump` there (`lib/build.log`; without `gcc` FrameFace uses its slower Python pixels);
3. runs the **install gate**, `selftest` of the new version in isolation (the running service and
   its state are not touched). Exit 3 (the C helper failed its parity check) removes the helper and
   continues; exit 4 (no libturbojpeg) continues with a FAIL line; any other failure deletes
   `~/frameface.new` and leaves the installed version running unchanged;
4. creates `~/.config/frameface/config.json` with a new token if it does not exist (or with
   `--new-token`/`--allow-peer`); `--reset-profile` forgets the learned tracker profile;
5. stops the service, moves `~/frameface` to `~/frameface.prev` and `~/frameface.new` to
   `~/frameface`, and copies the gate result to `~/.local/state/frameface/selftest.json`;
6. installs and enables the unit, restarts it (not with `--no-start`; start it later with
   `python3 -I ~/frameface/frameface_main.py start`), and prints the status, the URL and a quick
   probe. If the probe reports `[FAIL] P7`, it prints the udev fallback below.

### Upgrade, rollback, uninstall

- **Upgrade**: run the install again. The config (token, `allow_peers`), the learned profile and
  a deliberate `face_source: "off"` are kept.
- **Rollback**: `install-frameface.ps1 -HeadsetHost frame -Rollback`, or
  `bash ~/frameface/install.sh --rollback` on the headset. It swaps `~/frameface` and
  `~/frameface.prev` (a second rollback swaps back), re-installs that version's unit, runs
  `selftest --quick` and starts the service. Without `~/frameface.prev` it changes nothing.
- **Uninstall**: `install-frameface.ps1 -HeadsetHost frame -Uninstall [-Purge]`, or
  `bash ~/frameface/install.sh --uninstall [--purge]`. It disables and removes the unit, removes
  `~/frameface`, `~/frameface.prev`, `~/frameface.new`, every `~/frameface-upload-*` folder and the
  probe images (`~/.local/state/frameface/probe-*-images`). `--purge` also removes
  `~/.config/frameface` (config, token) and `~/.local/state/frameface` (profile, probe reports,
  logs). Running it again is harmless. The PC script also removes `face_source` from the module
  settings (with a backup), so the module behaves like 0.5.3 again after a VRCFaceTracking restart.

## Configuration

`~/.config/frameface/config.json` (mode 0600). Restart the service after an edit:
`python3 -I ~/frameface/frameface_main.py restart` (from the PC:
`ssh frame "python3 -I ~/frameface/frameface_main.py restart"`). An invalid value falls back to its
default with a warning in the log; an unknown key is reported too.

| Key | Default | Valid | Meaning |
|---|---|---|---|
| `enabled` | `true` | bool | `false`: never touch the tracker |
| `http_port` | `9023` | 1024-65535, except ports other software uses (3240, 5555, 6767, 7575, 8085, 8097, 8555, 8888, 8889, 9000, 9001, 9015, 9020, 9021, 9099, 9337, 10400, 10401, 27031-27037) | stream port; Baballonia's address changes with it |
| `allow_peers` | `["10.35.78.0/24", "127.0.0.0/8"]` | IPv4 CIDRs | who may connect (the headset's link to the PC, and itself); add e.g. `"192.168.4.31/32"` for a PC on the home LAN |
| `token` | random | `""` or 16-64 of `A-Z a-z 0-9 _ -` | secret part of the stream URL; `""` disables it |
| `max_stream_clients` | `2` | 1-4 | |
| `jpeg_quality` | `92` | 90-100 | |
| `stream_max_kbps` | `5000` | 500-20000 | total over all clients; frames are skipped above it, quality never drops |
| `stream_fps_max` | `60` | 1-60 | per client |
| `ip_tos` | `0` | 0 or 32 | 32 = background traffic class |
| `placeholder` | `true` | bool | one black frame if no live frame within `placeholder_ms` (Baballonia's start check) |
| `placeholder_ms` | `1500` | 200-10000 | |
| `keepalive_s` | `2.0` | 0.5-4.0 | keepalive parts on an idle stream |
| `camera` | `"left"` | `left`, `right` | which half of the stereo image |
| `y_phase` | `"auto"` | `auto`, `odd`, `even` | byte plane of the image |
| `preprocess` | `"auto"` | `auto`, `c`, `swar` | C helper or Python pixels |
| `strategy` | `"auto"` | `auto`, `S0`..`S5` | a value pins the start sequence |
| `xu_len` | `"auto"` | `auto`, 64, 384 | command length for every start sequence |
| `teardown_magic3` | `false` | bool | extra command after the IR-off writes (set it if probe A12 warns: IR still on, or cannot verify) |
| `n_urbs` | `16` | 4-32 | grows to 32 automatically |
| `urb_bytes` | `0` | 0, or 4096-65536 in steps of 512 | 0 = negotiated |
| `ir_level` | `"auto"` | `auto`, `L0`..`L3` | IR current level |
| `ir_current` | `null` | 0-255 | overrides the level's current |
| `ir_pwm` | `255` | 0-255 | experiments only |
| `settle_s` | `8.0` | 4-30 | pause before restarting the tracker after a stop |
| `enum_settle_s` | `2.0` | 0.5-10 | pause after the tracker appears |
| `power_policy` | `"auto"` | `auto`, `always` | `always` ignores the worn/idle rules |
| `gate_on_worn` | `true` | bool | run only while the headset is worn |
| `worn_timeout_s` | `3.0` | 1-30 | |
| `unworn_linger_s` | `60` | 10-600 | tracker off this long after taking the headset off |
| `no_client_linger_s` | `30` | 5-600 | tracker off this long after Baballonia disconnects |
| `min_battery_pct` | `5` | 0-50 | 0 disables |
| `allow_port_reset` | `true` | bool | |
| `cpu_io`, `cpu_work` | `"0-1"`, `"2-3"` | `auto` or a CPU list | |
| `nice_io` | `-5` | -8..0 | |
| `log_level` | `"info"` | `debug`, `info`, `warning` | |

`~/.config/frameface/disabled` (created by `off`, removed by `on`) switches face tracking off.

## Endpoints (TCP 9023)

| Path | Answer |
|---|---|
| `/stream/<token>` | MJPEG (`multipart/x-mixed-replace; boundary=frame`), grayscale 400x400 JPEGs, keepalive parts while idle |
| `/snap/<token>` | the newest JPEG (at most 2 s old), else 503 |
| `/status` | JSON state (no token, no pixels), at most 10 requests/s per address |

With `"token": ""` the paths are `/stream` and `/snap`. Only addresses in `allow_peers` may connect;
a wrong token gives 404. At most 8 connections and `max_stream_clients` streams; a reconnect from
the same address replaces its oldest stream. Only `GET` is accepted, requests are at most 4 KiB.
There are no control or raw-image endpoints.

## Command line

`python3 -I ~/frameface/frameface_main.py <command>` on the headset (over SSH: `ssh frame
"python3 -I ~/frameface/frameface_main.py status"`):

| Command | Does |
|---|---|
| `status [--json]` | the service state: display, message, hint, fps, clients (exit 3 if it does not answer) |
| `url [--new-token]` | the Baballonia URL(s): `http://10.35.78.1:9023/stream/<token>`, and also `http://<wlan0 address>:9023/stream/<token>` when an `allow_peers` CIDR that is not loopback and not inside the PC link `10.35.78.0/24` overlaps the headset's wlan0 network (so `--allow-peer <PC address>/32` gives the LAN URL too); `--new-token` rotates the token and restarts a running service itself |
| `restart` / `start` | restart / start the service (the systemd user unit, with `XDG_RUNTIME_DIR` set, so this also works over SSH) |
| `on` / `off` | enable / disable face tracking |
| `init-config [--force] [--new-token] [--allow-peer CIDR]...` | write the config (`--allow-peer` appends) |
| `probe ...` | diagnosis, see below |
| `selftest [--quick] [--lib DIR] [--json] [--result FILE]` | the install gate, see below |
| `sim [--scene mouth] [--listen 127.0.0.1] [--port 19023] ...` | the whole service against a simulated tracker, on temporary state (it never touches the real config, state or lock) |
| `run` | the service itself (the unit runs it) |

## Probe

`probe` checks the system read-only and is safe while the service runs: system and libraries,
udev rules, USB-C role, power, the tracker on USB with its power state (P6), access to its device
node (P7), other programs holding it (P8), descriptors, the eye server, network and port 9023, the
service and the kernel log. `--quick` runs P2, P6, P7, P8 and P12 (`install.sh` uses it).
`--bundle-name probe-<ts>` writes `report.json`, `summary.txt` and more to
`~/.local/state/frameface/probe-<ts>/`; `install-frameface.ps1 -Probe` copies that folder to the PC.

`probe --active` characterises the hardware: it switches the IR LEDs on, records GET_LEN, the start
sequence, the negotiation and a 10 s stream, and at the end reads the IR registers back after the
teardown (A12). `install-frameface.ps1 -ProbeActive` runs it with `--yes --stop-service`, which
stops the face service for 1-3 minutes and starts it again afterwards (the PC script also restarts
it if the SSH connection drops during the probe).
A12 passes only when the PWM registers 0x02-0x04 all read back 0. Any non-zero value is a WARN
`IR still on`; a WARN `cannot verify` means that the tracker did not answer REG_READ, or that no
REG_READ of these registers during the probe run read a non-zero value although the IR was switched
on (then a 0 proves nothing). In both WARN cases set `"teardown_magic3": true` (if it is
set already and A12 still warns, report the bundle). The USB and battery currents in the report are
information only.
Options: `--matrix` (every start sequence), `--ir-sweep`, `--soak N`, `--dump N` (raw frames),
`--pixels` (full payload trace), `--xu-len 384`, `--strategy S5`, `--teardown-magic3`. Anything
with pixels goes only to `probe-<ts>-images/` (files 0600), which the PC copies only with
`-IncludeImages` and `--uninstall` deletes. `--risky-settle-test`, `--risky-live-ir` and `--xu-len`
values other than 384 can wedge the tracker until a replug; they need a terminal and two
confirmations.

## Selftest

`selftest` is the install gate and needs no tracker: it compares `lib/abi_dump` with the Python
usbfs layout (exit 2 on a mismatch), checks that the C pixel helper gives exactly the Python
result (exit 3 if not; the service then uses the Python pixels), encodes a JPEG with libturbojpeg
(exit 4 if the library is missing) and, unless `--quick`, streams a simulated tracker through the
whole service (exit 2 on a failure). It runs on a temporary tree, never on the real config, state
or lock, so it can run while the service is live. The result goes to `--result FILE`, by default
`~/.local/state/frameface/selftest.json`, which always describes the installed version.

## Privacy

- The stream is your face. Only the headset's link to this PC (`10.35.78.0/24`) and the headset
  itself may connect, and `/stream` needs the 128-bit token in the URL (config file 0600).
- The token-bearing URL ends up in Baballonia's log on every reconnect warning: run
  `python3 -I ~/frameface/frameface_main.py url --new-token` and update Baballonia's address before
  you share Baballonia logs. It restarts a running service itself, so the old token stops working;
  should that restart fail, run `python3 -I ~/frameface/frameface_main.py restart`.
- `install-frameface.ps1` prints the URL; with `-CopyUrl` it also puts it on the clipboard, where
  Windows clipboard history and cloud clipboard sync may keep it.
- Adding your home LAN to `allow_peers` (`-AllowPeer`) sends the unencrypted face stream (HTTP,
  token in the path) over that LAN.
- The tracker is stopped as soon as the headset is not worn or face tracking is off. Every stop
  sends the IR-off register sequence, but whether the IR LEDs really switch off is **unverified**
  until the active probe's A12 step has read the registers back. A12 passes only when PWM 0x02-0x04
  all read back 0; if it warns `IR still on` (a non-zero value) or `cannot verify` (REG_READ did
  not answer, or never read these registers as non-zero during the probe run), set `"teardown_magic3": true`.
- Baballonia's Face Camera Address must use the printed IP URL (or the headset's own host name /
  `<hostname>.local`): any other DNS name for the headset gets 404, and
  `W http: bad host peer=<ip>` appears in `journalctl --user -u frameface`. A web page that points
  its own name at the headset's address therefore cannot read the stream.
- The service never writes images to disk and logs neither the token nor request paths. Only the
  active probe writes pixels, and only into `probe-<ts>-images/`.
- No telemetry, no update checks, no outbound connections. The unit runs as your user with
  `NoNewPrivileges=yes` and `UMask=0077`; the only compiled code is the C source in this archive.

## udev fallback

Normally nothing is needed: SteamOS gives USB devices group `input` with read-write access, and
`steamos` is in `input`. Only if the probe reports `[FAIL] P7 node access`, type these commands on
the headset yourself (FrameFace never runs sudo), then unplug and replug the tracker:

```bash
sudo install -m 0644 ~/frameface/70-vive-facial-tracker.rules /etc/udev/rules.d/
sudo mkdir -p /etc/atomic-update.conf.d
echo /etc/udev/rules.d/70-vive-facial-tracker.rules | sudo tee /etc/atomic-update.conf.d/vive-facial-tracker.conf
sudo udevadm control --reload && sudo udevadm trigger --action=add --subsystem-match=usb --attr-match=idVendor=0bb4
# undo: sudo rm /etc/udev/rules.d/70-vive-facial-tracker.rules /etc/atomic-update.conf.d/vive-facial-tracker.conf && sudo udevadm control --reload
```

The entry in `/etc/atomic-update.conf.d` keeps the rule across SteamOS updates.

## Logs and files

| What | Where |
|---|---|
| service log | `journalctl --user -u frameface` |
| events (rotated at 256 KiB to `events.jsonl.1`) | `~/.local/state/frameface/events.jsonl` |
| runtime state, learned profile | `~/.local/state/frameface/runtime.json`, `profile.json` |
| installed version's selftest result | `~/.local/state/frameface/selftest.json` |
| probe reports / probe pixels | `~/.local/state/frameface/probe-<ts>/` / `probe-<ts>-images/` |
| build log of the C helpers | `~/frameface/lib/build.log` |
| config, kill switch | `~/.config/frameface/config.json`, `~/.config/frameface/disabled` |
| unit | `~/.config/systemd/user/frameface.service` |

## Troubleshooting

`status` (and `/status`, the probe and `install-frameface.ps1 -Check`) shows one of these states:

| State | Meaning and what to do |
|---|---|
| `disabled` | face tracking is off: `python3 -I ~/frameface/frameface_main.py on` |
| `no_device` | plug the tracker straight into the Frame's USB-C port (no hub, no charger) |
| `port_device_mode` | the USB-C port is in charging/PC mode: the tracker cannot run while the headset charges |
| `no_permission` | see "udev fallback" |
| `busy` | another program holds the tracker (vftstream, Babble Frame Runtime, vive-face-init): stop it |
| `standby` | the headset is not worn ("worn" = the eye tracker's counter moves; without eye tracking set `"gate_on_worn": false`) |
| `waiting_client` | ready; point Baballonia's Face Camera Address at the URL, "This is a..." = Wireless/IP Camera |
| `settling`, `starting`, `recovering` | wait a few seconds |
| `streaming` | working |
| `test_pattern`, `dark_start` | the tracker shows test bars or a black image; FrameFace tries the next start sequence by itself |
| `dark` | the image went black: is the tracker facing your mouth, is the lens covered? |
| `brownout` | the tracker lost power; IR lowered; use a direct USB-C connection |
| `wedged` | unplug the tracker for 10 s |
| `no_ir` | every start sequence gave a black image: replug, then run `-ProbeActive` and report the bundle |
| `port_in_use` | another program uses port 9023: stop it, or set another `http_port` and run `python3 -I ~/frameface/frameface_main.py restart` |
| `unsupported` | run `-Probe` and report the bundle |
| `library` | libturbojpeg is missing: update SteamOS, then run the install again |

## Licence

MIT, see `LICENSE`. The protocol facts come from MIT-licensed prior work; the notices and the
attribution chain are in `NOTICE.md`. Baballonia (Babble Software Distribution License 1.0) is
installed by the user and never bundled; its face model is never redistributed.
