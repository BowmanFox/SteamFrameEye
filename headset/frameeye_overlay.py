#!/usr/bin/env python3
"""
frameeye_overlay.py - the Steam Frame eye sender as a SteamVR overlay application.

Runs frameeye_osc.SenderRuntime in-process (eye server shared memory -> PC over UDP) and shows
its state as a dashboard overlay: whether eye data is flowing, sample rate, the targets, and the
live per-eye gaze and eyelid openness. SteamVR starts it at boot once it is registered with
--install (frameeye.vrmanifest + auto-launch) and asks it to quit at shutdown.

Standard library only. Follows the FCAM overlay of the Baballonia steamframe folder (same
openvr_min binding and bitmap font).

    frameeye_overlay.py --raw <pc>:9021 [...]     run (what SteamVR launches through frameeye_overlay.sh)
    frameeye_overlay.py --install --raw <pc>:9021 write the manifest with these sender options,
                                                  register with SteamVR, enable auto-launch, start it
    frameeye_overlay.py --uninstall               remove the registration and stop
    frameeye_overlay.py --status                  show registration state
"""

import argparse
import json
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time

try:
    import fcntl
except ImportError:  # not Linux: no single-instance lock (development only)
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import bitmap_font  # noqa: E402
import frameeye_osc  # noqa: E402
import openvr_min as ovr  # noqa: E402

APP_KEY = "ottlabs.steamframeeye"
OVERLAY_KEY = "ottlabs.steamframeeye.dashboard"
OVERLAY_NAME = "Steam Frame Eye"
MANIFEST = os.path.join(HERE, "frameeye.vrmanifest")
ICON = os.path.join(HERE, "icon.png")
DEFAULT_LOG = os.path.join(HERE, "frameeye_overlay.log")
LOCK = os.path.join(HERE, "frameeye_overlay.lock")

PANEL_W, PANEL_H = 640, 400
VR_RETRY_SECONDS = 5.0
REDRAW_SECONDS = 0.2          # while the tab is visible (live eyes)
REDRAW_HIDDEN_SECONDS = 5.0   # while hidden, or after a failed upload (standby)
EVENT_POLL_SECONDS = 0.05

# RGBA colours
BG = (24, 32, 44, 255)
PANEL = (34, 44, 58, 255)
TEXT = (230, 236, 240, 255)
DIM = (150, 160, 170, 255)
ACCENT = (78, 160, 168, 255)
OK = (96, 200, 120, 255)
WARN = (240, 180, 70, 255)
BAD = (230, 90, 90, 255)
SCLERA = (236, 238, 240, 255)
IRIS = (78, 160, 168, 255)
PUPIL = (18, 22, 28, 255)
LID = (52, 64, 80, 255)

log = logging.getLogger("frameeye.overlay")


# ----------------------------------------------------------------------------- software renderer

class Panel:
    """An RGBA8 raster the size of the overlay, drawn with the generated bitmap font."""

    def __init__(self, width, height):
        self.width, self.height = width, height
        self.buf = bytearray(width * height * 4)

    def fill(self, rgba, x0=0, y0=0, x1=None, y1=None):
        x1 = self.width if x1 is None else min(x1, self.width)
        y1 = self.height if y1 is None else min(y1, self.height)
        x0, y0 = max(0, x0), max(0, y0)
        if x1 <= x0 or y1 <= y0:
            return
        row = bytes(rgba) * (x1 - x0)
        stride = self.width * 4
        for y in range(y0, y1):
            start = y * stride + x0 * 4
            self.buf[start:start + len(row)] = row

    def outline(self, rgba, x0, y0, x1, y1, thickness=2):
        self.fill(rgba, x0, y0, x1, y0 + thickness)
        self.fill(rgba, x0, y1 - thickness, x1, y1)
        self.fill(rgba, x0, y0, x0 + thickness, y1)
        self.fill(rgba, x1 - thickness, y0, x1, y1)

    def disc(self, rgba, cx, cy, r):
        for dy in range(-r, r + 1):
            half = int(math.sqrt(max(0, r * r - dy * dy)))
            self.fill(rgba, cx - half, cy + dy, cx + half + 1, cy + dy + 1)

    def text(self, x, y, string, rgba=TEXT, scale=1):
        cell_w, cell_h = bitmap_font.CELL_W, bitmap_font.CELL_H
        pixel = bytes(rgba)
        stride = self.width * 4
        for index, ch in enumerate(string):
            gx = x + index * cell_w * scale
            if gx >= self.width:
                break
            for ry, row in enumerate(bitmap_font.glyph_rows(ch)):
                if not row:
                    continue
                for rx in range(cell_w):
                    if row >> rx & 1:
                        px, py = gx + rx * scale, y + ry * scale
                        for dy in range(scale):
                            yy = py + dy
                            if 0 <= yy < self.height:
                                start = yy * stride + px * 4
                                self.buf[start:start + 4 * scale] = pixel * scale

    def text_width(self, string, scale=1):
        return len(string) * bitmap_font.CELL_W * scale


def draw_eye(panel, cx, cy, r, gaze_x, gaze_y, openness, active):
    """A schematic eye: sclera disc, iris/pupil displaced by the gaze angles (x right, y up),
    and an upper lid covering (1 - openness) of the disc."""
    panel.disc(SCLERA if active else DIM, cx, cy, r)
    if active:
        ox = int(gaze_x * r * 0.55)
        oy = int(-gaze_y * r * 0.55)
        panel.disc(IRIS, cx + ox, cy + oy, int(r * 0.5))
        panel.disc(PUPIL, cx + ox, cy + oy, int(r * 0.22))
    closed = int((1.0 - max(0.0, min(1.0, openness))) * 2 * r)
    if closed > 0:
        # lid: clip everything above the lid line back to the panel colour, keeping the disc outline
        for dy in range(-r, -r + closed):
            half = int(math.sqrt(max(0, r * r - dy * dy)))
            panel.fill(LID, cx - half, cy + dy, cx + half + 1, cy + dy + 1)


def draw_panel(panel, snapshot, vr_version):
    panel.fill(BG)
    panel.fill(PANEL, 16, 16, PANEL_W - 16, PANEL_H - 16)

    panel.text(32, 28, "Steam Frame Eye", ACCENT, scale=2)
    state = snapshot.get("state", "?")
    if state == "streaming":
        state_text, state_color = "eye data streaming, %.0f Hz" % snapshot.get("rate", 0.0), OK
    elif state == "idle":
        state_text, state_color = "eye server idle (headset off / standby)", WARN
    else:
        state_text, state_color = "waiting for the eye tracking server", BAD
    panel.text(32, 90, state_text, state_color)

    targets = snapshot.get("raw_targets") or []
    osc = snapshot.get("osc_target")
    values = snapshot.get("values")
    age = snapshot.get("age")
    rows = [
        ("vrcft", ", ".join(targets) or "no --raw target"),
        ("vrchat", osc or "off (VRCFT sends the parameters)"),
        ("samples", "%d sent, %d valid, %d send errors" % (snapshot.get("samples", 0), snapshot.get("valid", 0),
                                                           snapshot.get("send_errors", 0))),
        ("last", "%.1f s ago (seq %s)" % (age, snapshot.get("seq")) if age is not None else "none yet"),
    ]
    if values:
        lx, ly, rx, ry, cx, cy, lid_l, lid_r = values
        rows.append(("left", "gaze %+.2f %+.2f  lid %.2f" % (lx, ly, lid_l)))
        rows.append(("right", "gaze %+.2f %+.2f  lid %.2f" % (rx, ry, lid_r)))
    y = 130
    for label, value in rows:
        panel.text(32, y, label.rjust(8), DIM)
        panel.text(32 + 9 * bitmap_font.CELL_W, y, value[:44], TEXT)
        y += bitmap_font.CELL_H + 6

    active = state == "streaming" and values is not None
    lx, ly, rx, ry = (values[0], values[1], values[2], values[3]) if values else (0, 0, 0, 0)
    lid_l, lid_r = (values[6], values[7]) if values else (1.0, 1.0)
    # the panel is what the wearer sees: their left eye on the left (bottom right corner, below the rows)
    eye_y, eye_r = PANEL_H - 16 - 58, 40
    draw_eye(panel, 470, eye_y, eye_r, lx, ly, lid_l, active)
    draw_eye(panel, 572, eye_y, eye_r, rx, ry, lid_r, active)
    panel.text(470 - 6 - eye_r - 14, eye_y - bitmap_font.CELL_H // 2, "L", DIM)
    panel.text(572 + eye_r + 8, eye_y - bitmap_font.CELL_H // 2, "R", DIM)

    panel.text(32, PANEL_H - 16 - 30, "SteamVR %s" % (vr_version or "?"), DIM)


# ----------------------------------------------------------------------------- overlay app

class OverlayApp:
    def __init__(self, args):
        self.args = args
        self.runtime = frameeye_osc.SenderRuntime(args)
        self.session = None
        self.main_handle = None
        self.thumb_handle = None
        self.panel = Panel(PANEL_W, PANEL_H)
        self.stop = threading.Event()
        self.last_vr_attempt = 0.0
        self.vr_error_logged = None
        self.upload_failures = 0
        self.upload_failed_since = None

    # -- SteamVR connection

    def connect_vr(self):
        now = time.monotonic()
        if now - self.last_vr_attempt < VR_RETRY_SECONDS:
            return False
        self.last_vr_attempt = now
        try:
            session = ovr.Session(self.args.openvr_lib).init(ovr.VRApplication_Overlay)
        except ovr.OpenVRError as e:
            message = str(e)
            if message != self.vr_error_logged:
                log.info("SteamVR not available yet: %s", message)
                self.vr_error_logged = message
            return False
        self.vr_error_logged = None
        try:
            overlay = session.overlay
            existing = overlay.find_overlay(OVERLAY_KEY)
            if existing:
                overlay.destroy_overlay(existing)
            main, thumb = overlay.create_dashboard_overlay(OVERLAY_KEY, OVERLAY_NAME)
            overlay.set_width_meters(main, 1.5)
            if os.path.exists(ICON):
                try:
                    overlay.set_from_file(thumb, ICON)
                except ovr.OpenVRError as e:
                    log.warning("thumbnail not set: %s", e)
            try:
                session.applications.identify(os.getpid(), APP_KEY)
            except ovr.OpenVRError as e:
                log.debug("IdentifyApplication: %s", e)
        except ovr.OpenVRError as e:
            log.error("overlay setup failed: %s", e)
            session.shutdown()
            return False
        self.session = session
        self.main_handle, self.thumb_handle = main, thumb
        log.info("SteamVR connected (%s, %s, %s, runtime %s, %s)", session.system.version, session.overlay.version,
                 session.applications.version, session.system.runtime_version(), session.library_path)
        self.redraw()
        return True

    def disconnect_vr(self):
        if self.session is None:
            return
        try:
            if self.main_handle:
                self.session.overlay.destroy_overlay(self.main_handle)
        except ovr.OpenVRError:
            pass
        self.session.shutdown()
        self.session = None
        self.main_handle = self.thumb_handle = None

    # -- drawing

    def redraw(self):
        """Draws the panel and uploads it. Returns True when the upload succeeded."""
        if self.session is None:
            return False
        snapshot = self.runtime.snapshot()
        version = self.session.system.runtime_version()
        draw_panel(self.panel, snapshot, version)
        try:
            self.session.overlay.set_raw(self.main_handle, self.panel.buf, PANEL_W, PANEL_H)
        except ovr.OpenVRError as e:
            # Happens while the compositor is suspended (headset in standby); log once per episode.
            self.upload_failures += 1
            if self.upload_failed_since is None:
                self.upload_failed_since = time.monotonic()
                log.warning("overlay upload failed (%s); retrying every %.0f s", e, REDRAW_HIDDEN_SECONDS)
            return False
        if self.upload_failed_since is not None:
            log.info("overlay uploads work again after %.0f s (%d failed)",
                     time.monotonic() - self.upload_failed_since, self.upload_failures)
            self.upload_failed_since = None
        return True

    def handle_events(self):
        """Returns False when SteamVR asked us to quit."""
        session = self.session
        while True:
            event = session.system.poll_next_event()
            if event is None:
                break
            # Only VREvent_Quit is addressed to us; ProcessQuit (701) is broadcast whenever any
            # other VR process exits and must be ignored.
            if event.eventType == ovr.VREvent_Quit:
                log.info("SteamVR requested quit")
                session.system.acknowledge_quit_exiting()
                return False
        while session.overlay.poll_next_overlay_event(self.main_handle) is not None:
            pass  # no interactive elements on this panel
        return True

    # -- main loop

    def run(self):
        log.info("Steam Frame Eye overlay starting (pid %d)", os.getpid())
        self.runtime.start()
        next_redraw = 0.0
        try:
            while not self.stop.is_set():
                if self.session is None:
                    self.connect_vr()
                    self.stop.wait(0.5 if self.session is None else 0)
                    continue
                try:
                    if not self.handle_events():
                        break
                    now = time.monotonic()
                    if now >= next_redraw:
                        visible = self.session.overlay.is_visible(self.main_handle)
                        ok = self.redraw()
                        next_redraw = now + (REDRAW_SECONDS if visible and ok else REDRAW_HIDDEN_SECONDS)
                except ovr.OpenVRError as e:
                    log.warning("SteamVR call failed, reconnecting: %s", e)
                    self.disconnect_vr()
                    continue
                self.stop.wait(EVENT_POLL_SECONDS)
        finally:
            self.disconnect_vr()
            self.runtime.stop()
            log.info("Steam Frame Eye overlay stopped")


# ----------------------------------------------------------------------------- single instance

def acquire_lock():
    """Holds an exclusive lock for this install directory. Returns the lock file, or None when another
    instance already runs (for example one started by hand before SteamVR auto-launched us)."""
    if fcntl is None:
        return open(os.devnull, "w")
    handle = open(LOCK, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def running_pid():
    """Pid of the instance holding the lock, or 0."""
    if fcntl is None or not os.path.exists(LOCK):
        return 0
    try:
        with open(LOCK) as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return int(handle.read().strip() or 0)
            fcntl.flock(handle, fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass
    return 0


def start_detached(run_args):
    """Starts the overlay in its own session, surviving the caller's logout."""
    with open(os.devnull, "rb") as stdin, open(os.devnull, "ab") as stdout:
        process = subprocess.Popen([sys.executable, os.path.abspath(__file__)] + run_args, cwd=HERE, stdin=stdin,
                                   stdout=stdout, stderr=stdout, start_new_session=True, close_fds=True)
    return process.pid


# ----------------------------------------------------------------------------- registration

def config_dir():
    path = os.path.expanduser("~/.config/openvr/openvrpaths.vrpath")
    try:
        with open(path, encoding="utf-8") as fh:
            dirs = json.load(fh).get("config") or []
        if dirs:
            return dirs[0]
    except (OSError, ValueError):
        pass
    return os.path.expanduser("~/.config/openvr/config")


def write_manifest(run_args):
    """frameeye.vrmanifest next to this script; SteamVR launches frameeye_overlay.sh with run_args."""
    manifest = {
        "source": "builtin",
        "applications": [{
            "app_key": APP_KEY,
            "launch_type": "binary",
            "binary_path_linux": "frameeye_overlay.sh",
            "binary_path_linux_arm": "frameeye_overlay.sh",
            "arguments": " ".join(run_args),
            "is_dashboard_overlay": True,
            "image_path": "icon.png",
            "strings": {"en_us": {
                "name": OVERLAY_NAME,
                "description": "Sends the Steam Frame's eye tracking (per-eye gaze, eyelid openness) to the PC "
                               "for VRCFaceTracking / VRChat",
            }},
        }],
    }
    with open(MANIFEST, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent="\t")
        fh.write("\n")
    return MANIFEST


def register_files(enable):
    """Registers (or removes) the manifest and auto-launch flag directly in SteamVR's config files,
    which SteamVR reads at startup. Used so the install works with SteamVR stopped too."""
    cfg = config_dir()
    os.makedirs(os.path.join(cfg, "vrappconfig"), exist_ok=True)
    appconfig = os.path.join(cfg, "appconfig.json")
    try:
        with open(appconfig, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    paths = [p for p in data.get("manifest_paths", []) if os.path.abspath(p) != os.path.abspath(MANIFEST)]
    if enable:
        paths.append(os.path.abspath(MANIFEST))
    data["manifest_paths"] = paths
    with open(appconfig, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=3)
    appcfg = os.path.join(cfg, "vrappconfig", APP_KEY + ".vrappconfig")
    with open(appcfg, "w", encoding="utf-8") as fh:
        json.dump({"autolaunch": bool(enable), "last_launch_time": "0"}, fh, indent=3)
    return appconfig, appcfg


def register_live(enable, launch, library, run_args):
    """Same through the running SteamVR, so it takes effect immediately. Returns a status text."""
    try:
        session = ovr.Session(library).init(ovr.VRApplication_Background)
    except ovr.OpenVRError as e:
        note = "SteamVR not running (%s); registration takes effect at the next SteamVR start" % e
        if enable and launch:
            pid = running_pid()
            if pid:
                note += "; overlay already running (pid %d)" % pid
            else:
                note += "; started detached (pid %d), it connects when SteamVR comes up" % start_detached(run_args)
        elif not enable:
            pid = running_pid()
            if pid:
                os.kill(pid, signal.SIGTERM)
                note += "; stopped pid %d" % pid
        return note
    try:
        apps = session.applications
        if enable:
            apps.add_manifest(MANIFEST)
            apps.set_auto_launch(APP_KEY, True)
            state = "registered with SteamVR, auto-launch %s" % apps.get_auto_launch(APP_KEY)
            if launch:
                pid = apps.process_id(APP_KEY) or running_pid()
                if pid:
                    state += ", already running (pid %d)" % pid
                else:
                    apps.launch(APP_KEY)
                    time.sleep(2.0)
                    state += ", launched (pid %d)" % apps.process_id(APP_KEY)
            return state
        pid = apps.process_id(APP_KEY) or running_pid()
        if pid:
            os.kill(pid, signal.SIGTERM)
        apps.set_auto_launch(APP_KEY, False)
        apps.remove_manifest(MANIFEST)
        return "removed from SteamVR" + (", stopped pid %d" % pid if pid else "")
    except ovr.OpenVRError as e:
        return "SteamVR registration failed: %s" % e
    finally:
        session.shutdown()


def status(library):
    cfg = config_dir()
    lines = ["manifest: %s" % MANIFEST, "config dir: %s" % cfg, "running instance (lock): pid %d" % running_pid()]
    try:
        with open(MANIFEST, encoding="utf-8") as fh:
            lines.append("manifest arguments: %s" % json.load(fh)["applications"][0].get("arguments", ""))
    except (OSError, ValueError, KeyError, IndexError):
        lines.append("manifest: not written yet (run --install)")
    try:
        with open(os.path.join(cfg, "appconfig.json"), encoding="utf-8") as fh:
            lines.append("appconfig.json lists it: %s" %
                         (os.path.abspath(MANIFEST) in map(os.path.abspath, json.load(fh).get("manifest_paths", []))))
    except (OSError, ValueError):
        lines.append("appconfig.json: unreadable")
    try:
        session = ovr.Session(library).init(ovr.VRApplication_Background)
        apps = session.applications
        lines.append("SteamVR: installed=%s autolaunch=%s pid=%d" %
                     (apps.is_installed(APP_KEY), apps.get_auto_launch(APP_KEY), apps.process_id(APP_KEY)))
        session.shutdown()
    except ovr.OpenVRError as e:
        lines.append("SteamVR: not reachable (%s)" % e)
    return "\n".join(lines)


# ----------------------------------------------------------------------------- entry point

def truncate_log(path, limit=1024 * 1024):
    try:
        if os.path.getsize(path) > limit:
            os.replace(path, path + ".old")
    except OSError:
        pass


def main(argv=None):
    parser = frameeye_osc.build_parser()
    parser.description = "Steam Frame eye sender as a SteamVR overlay."
    parser.add_argument("--install", action="store_true",
                        help="write the manifest with the given sender options, register with SteamVR, "
                             "enable auto-launch and start")
    parser.add_argument("--uninstall", action="store_true", help="remove the SteamVR registration and stop")
    parser.add_argument("--status", action="store_true", help="print registration state and exit")
    parser.add_argument("--no-launch", action="store_true", help="with --install: do not start the overlay now")
    parser.add_argument("--openvr-lib", default=None, help="path to libopenvr_api.so (auto-detected)")
    parser.add_argument("--log-file", default=DEFAULT_LOG, help="log file (default: next to this script)")
    args = parser.parse_args(argv)

    handlers = [logging.StreamHandler()]
    if args.log_file:
        truncate_log(args.log_file)
        handlers.append(logging.FileHandler(args.log_file))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%H:%M:%S",
                        handlers=handlers)

    if args.status:
        print(status(args.openvr_lib))
        return 0
    run_args = frameeye_osc.runtime_args(args)
    if args.install or args.uninstall:
        enable = bool(args.install)
        if enable:
            if not args.raw and not args.target:
                parser.error("--install needs the sender options to record, e.g. --raw <pc-ip>:9021")
            print("wrote %s (arguments: %s)" % (write_manifest(run_args), " ".join(run_args)))
        appconfig, appcfg = register_files(enable)
        print("%s %s and %s" % ("updated" if enable else "cleaned", appconfig, appcfg))
        print(register_live(enable, launch=not args.no_launch, library=args.openvr_lib, run_args=run_args))
        return 0

    if not args.raw and not args.target:
        parser.error("give --raw <pc-ip>:9021 and/or --target <pc-ip>:9000")
    lock = acquire_lock()
    if lock is None:
        log.info("another Steam Frame Eye overlay instance is running (pid %d); exiting", running_pid())
        return 0

    try:
        app = OverlayApp(args)
    except ValueError as e:
        log.error("%s", e)
        lock.close()
        return 1

    def request_stop(signum, _frame):
        log.info("signal %d, stopping", signum)
        app.stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    app.run()
    lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
