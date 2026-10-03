#!/usr/bin/env python3
"""
frameeye_overlay.py - FrameEye Bridge, a SteamVR dashboard application that forwards the Steam
Frame's eye tracking to VRCFaceTracking on the PC.

Runs frameeye_osc.SenderRuntime in-process (eye server shared memory -> PC over UDP) and shows
its state as a dashboard tab: whether eye data is flowing, sample rate, the live per-eye gaze and
eyelid openness, and a small settings form: Sending on/off, the PC address (a text field edited
with the SteamVR keyboard) and a switch for direct OSC to VRChat on the PC. Settings persist in
frameeye.conf.json next to this script. SteamVR starts it at boot once it is registered with
--install (frameeye.vrmanifest + auto-launch) and asks it to quit at shutdown.

The panel is streamed through a persistent OpenGL texture (gl_texture.py, EGL surfaceless). The
raw-pixel path (SetOverlayRaw) recreates the compositor texture on every call and flickers, so it
is only a low-rate fallback.

Standard library only. Follows the FCAM overlay of the Baballonia steamframe folder (same
openvr_min binding and bitmap font).

    frameeye_overlay.py [sender options]          run (what SteamVR launches through frameeye_overlay.sh)
    frameeye_overlay.py --install --raw <pc>:9021 write manifest + config, register with SteamVR,
                                                  enable auto-launch, start it
    frameeye_overlay.py --uninstall               remove the registration and stop
    frameeye_overlay.py --status                  show registration state
"""

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

try:
    import gl_texture  # noqa: E402
except Exception:  # pragma: no cover - platform without ctypes GL support
    gl_texture = None

# The SteamVR identifiers keep the app's first name (Steam Frame Eye), so existing registrations and
# the auto-launch setting carry over to the renamed app.
APP_KEY = "ottlabs.steamframeeye"
OVERLAY_KEY = "ottlabs.steamframeeye.dashboard"
OVERLAY_NAME = "FrameEye Bridge"
MANIFEST = os.path.join(HERE, "frameeye.vrmanifest")
CONFIG = os.path.join(HERE, "frameeye.conf.json")
ICON = os.path.join(HERE, "icon.png")
DEFAULT_LOG = os.path.join(HERE, "frameeye_overlay.log")
LOCK = os.path.join(HERE, "frameeye_overlay.lock")

PANEL_W, PANEL_H = 640, 628
VR_RETRY_SECONDS = 5.0
REDRAW_SECONDS = 0.04         # GL path, tab visible: up to 25 Hz, driven by new samples
REDRAW_SECONDS_RAW = 0.5      # SetOverlayRaw fallback (each upload blanks a frame)
REDRAW_STATIC_SECONDS = 0.5   # visible but nothing new: age counter, idle state
REDRAW_HIDDEN_SECONDS = 5.0   # tab hidden, or after a failed upload (standby)
VISIBLE_CHECK_SECONDS = 0.25
EVENT_POLL_SECONDS = 0.05
DEFAULT_RAW_PORT = 9021
VRCHAT_OSC_PORT = 9000
KEYBOARD_ADDRESS = 1          # user value of the address keyboard

# RGBA colours
BG = (24, 32, 44, 255)
PANEL = (34, 44, 58, 255)
FIELD = (18, 24, 34, 255)
TEXT = (230, 236, 240, 255)
DIM = (150, 160, 170, 255)
ACCENT = (78, 160, 168, 255)
OK = (96, 200, 120, 255)
WARN = (240, 180, 70, 255)
BAD = (230, 90, 90, 255)
BUTTON = (60, 110, 120, 255)
BUTTON_HOT = (90, 150, 160, 255)
BUTTON_OFF = (70, 78, 92, 255)
SCLERA = (236, 238, 240, 255)
IRIS = (78, 160, 168, 255)
PUPIL = (18, 22, 28, 255)
LID = (52, 64, 80, 255)

log = logging.getLogger("frameeye.overlay")


# ----------------------------------------------------------------------------- software renderer

class Panel:
    """An RGBA8 raster the size of the overlay, drawn with the generated bitmap font."""

    _glyph_cache = {}

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

    @classmethod
    def glyph_rows_bytes(cls, ch, rgba, scale, bg):
        """Pre-rendered rows (bytes) of one glyph with the background baked in; one slice copy per row."""
        key = (ch, rgba, scale, bg)
        rows = cls._glyph_cache.get(key)
        if rows is None:
            cell_w = bitmap_font.CELL_W
            on, off = bytes(rgba), bytes(bg)
            rows = []
            for row in bitmap_font.glyph_rows(ch):
                line = b"".join((on if row >> rx & 1 else off) * scale for rx in range(cell_w))
                rows.extend([line] * scale)
            cls._glyph_cache[key] = rows
        return rows

    def text(self, x, y, string, rgba=TEXT, scale=1, bg=PANEL):
        cell_w = bitmap_font.CELL_W
        stride = self.width * 4
        glyph_w = cell_w * scale * 4
        for index, ch in enumerate(string):
            gx = x + index * cell_w * scale
            if gx + cell_w * scale > self.width:
                break
            rows = self.glyph_rows_bytes(ch, rgba, scale, bg)
            for ry, line in enumerate(rows):
                yy = y + ry
                if 0 <= yy < self.height:
                    start = yy * stride + gx * 4
                    self.buf[start:start + glyph_w] = line

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
        for dy in range(-r, -r + closed):
            half = int(math.sqrt(max(0, r * r - dy * dy)))
            panel.fill(LID, cx - half, cy + dy, cx + half + 1, cy + dy + 1)


# ----------------------------------------------------------------------------- layout

CELL_W, CELL_H = bitmap_font.CELL_W, bitmap_font.CELL_H
STATUS_CARD = (16, 16, PANEL_W - 16, 352)
SETTINGS_CARD = (16, 364, PANEL_W - 16, PANEL_H - 16)
ROW_SENDING = (32, 376, 608, 428)      # whole row is clickable
ROW_ADDRESS = (32, 436, 608, 480)
ROW_VRCHAT = (32, 488, 608, 540)
ROW_SMOOTHING = (32, 548, 608, 600)
TOGGLE_X0, TOGGLE_X1 = 496, 608
FIELD_X0, FIELD_X1 = 200, 608
CAPTION_CHARS = (TOGGLE_X0 - 8 - 32) // CELL_W
CONTROLS = (("sending", ROW_SENDING), ("address", ROW_ADDRESS), ("vrchat", ROW_VRCHAT),
            ("smoothing", ROW_SMOOTHING))


def control_at(x, y):
    """Control id under a panel position (top-down pixel coordinates)."""
    for cid, (x0, y0, x1, y1) in CONTROLS:
        if x0 <= x < x1 and y0 <= y < y1:
            return cid
    return None


def draw_toggle(panel, x0, y0, x1, y1, on, hot):
    color = BUTTON_HOT if hot else (BUTTON if on else BUTTON_OFF)
    panel.fill(color, x0, y0, x1, y1)
    panel.outline(ACCENT if (on or hot) else DIM, x0, y0, x1, y1)
    label = "ON" if on else "OFF"
    panel.text(x0 + (x1 - x0 - panel.text_width(label)) // 2, y0 + (y1 - y0 - CELL_H) // 2, label, TEXT, bg=color)


def draw_field(panel, x0, y0, x1, y1, value, hot, editing):
    panel.fill(FIELD, x0, y0, x1, y1)
    panel.outline(WARN if editing else ACCENT if hot else DIM, x0, y0, x1, y1)
    ty = y0 + (y1 - y0 - CELL_H) // 2
    hint = "typing" if editing else "edit"
    hint_x = x1 - 12 - panel.text_width(hint)
    max_chars = (hint_x - 8 - (x0 + 12)) // CELL_W
    if editing:
        shown = (value + "_")[-max_chars:]
        panel.text(x0 + 12, ty, shown, TEXT, bg=FIELD)
    elif value:
        panel.text(x0 + 12, ty, value[:max_chars], TEXT, bg=FIELD)
    else:
        panel.text(x0 + 12, ty, "not set - click to enter"[:max_chars], DIM, bg=FIELD)
    panel.text(hint_x, ty, hint, WARN if editing else TEXT if hot else DIM, bg=FIELD)


def draw_panel(panel, snapshot, settings, hot, editing_text, vr_version, texture_path):
    panel.fill(BG)
    panel.fill(PANEL, *STATUS_CARD)
    panel.fill(PANEL, *SETTINGS_CARD)

    # -- status card
    panel.text(32, 28, OVERLAY_NAME, ACCENT, scale=2)
    state = snapshot.get("state", "?")
    rate = snapshot.get("rate", 0.0)
    if state == "streaming" and not settings.enabled:
        state_text, state_color = "eye data %.0f Hz, sending paused" % rate, WARN
    elif state == "streaming":
        state_text, state_color = "eye data streaming, %.0f Hz" % rate, OK
    elif state == "idle":
        state_text, state_color = "eye server idle (headset off / standby)", WARN
    else:
        state_text, state_color = "waiting for the eye tracking server", BAD
    panel.text(32, 90, state_text, state_color)

    values = snapshot.get("values")
    age = snapshot.get("age")
    rows = [
        ("samples", "%d read, %d valid, %d send errors" % (snapshot.get("samples", 0), snapshot.get("valid", 0),
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
        panel.text(32 + 9 * CELL_W, y, value[:44], TEXT)
        y += CELL_H + 6

    active = state == "streaming" and values is not None
    lx, ly, rx, ry = (values[0], values[1], values[2], values[3]) if values else (0, 0, 0, 0)
    lid_l, lid_r = (values[6], values[7]) if values else (1.0, 1.0)
    eye_y, eye_r = 292, 40
    draw_eye(panel, 462, eye_y, eye_r, lx, ly, lid_l, active)      # wearer's left eye on the left
    draw_eye(panel, 564, eye_y, eye_r, rx, ry, lid_r, active)
    panel.text(462 - eye_r - 20, eye_y - CELL_H // 2, "L", DIM)
    panel.text(564 + eye_r + 8, eye_y - CELL_H // 2, "R", DIM)
    panel.text(32, STATUS_CARD[3] - 12 - CELL_H, "SteamVR %s  %s" % (vr_version or "?", texture_path), DIM)

    # -- settings card
    host = settings.pc_host()
    x0, y0, x1, y1 = ROW_SENDING
    panel.text(32, y0 + 4, "Sending to PC", TEXT)
    caption = ("-> %s (VRCFT module)" % settings.raw[0]) if settings.raw else "no PC address set"
    panel.text(32, y0 + 27, caption[:CAPTION_CHARS], DIM)
    draw_toggle(panel, TOGGLE_X0, y0 + 4, TOGGLE_X1, y1 - 4, settings.enabled, hot == "sending")

    x0, y0, x1, y1 = ROW_ADDRESS
    panel.text(32, y0 + (y1 - y0 - CELL_H) // 2, "PC address", TEXT)
    draw_field(panel, FIELD_X0, y0, FIELD_X1, y1,
               editing_text if editing_text is not None else (settings.raw[0] if settings.raw else ""),
               hot == "address", editing_text is not None)

    x0, y0, x1, y1 = ROW_VRCHAT
    panel.text(32, y0 + 4, "VRChat direct OSC (PC)", TEXT)
    if settings.target:
        caption = "FT/v2 -> %s (no VRCFT)" % settings.target
    elif host:
        caption = "no VRCFT: FT/v2 -> %s:%d" % (host, VRCHAT_OSC_PORT)
    else:
        caption = "needs the PC address"
    panel.text(32, y0 + 27, caption[:CAPTION_CHARS], DIM)
    draw_toggle(panel, TOGGLE_X0, y0 + 4, TOGGLE_X1, y1 - 4, bool(settings.target), hot == "vrchat")

    x0, y0, x1, y1 = ROW_SMOOTHING
    panel.text(32, y0 + 4, "Direct OSC smoothing", TEXT)
    panel.text(32, y0 + 27, "Steadier gaze and eyelids" if settings.target else "Used when direct OSC is on", DIM)
    draw_toggle(panel, TOGGLE_X0, y0 + 4, TOGGLE_X1, y1 - 4, settings.osc_smoothing, hot == "smoothing")


# ----------------------------------------------------------------------------- settings

class Settings:
    """raw targets, direct VRChat target and the sending switch; persisted in frameeye.conf.json."""

    def __init__(self, args):
        self.raw = [r for r in args.raw if r and r != "none"]
        self.target = args.target if args.target and args.target != "none" else None
        self.enabled = True
        self.osc_smoothing = args.osc_smoothing
        self.mouse_origin_bottom = True   # OpenVR mouse events are in GL space (0,0 = bottom left)
        cfg = load_config()
        if isinstance(cfg.get("raw"), list):
            self.raw = [str(r) for r in cfg["raw"]]
        if "target" in cfg:
            self.target = cfg["target"] or None
        if "enabled" in cfg:
            self.enabled = bool(cfg["enabled"])
        if isinstance(cfg.get("osc_smoothing"), bool):
            self.osc_smoothing = cfg["osc_smoothing"]
        if "mouse_origin_bottom" in cfg:
            self.mouse_origin_bottom = bool(cfg["mouse_origin_bottom"])

    def save(self):
        cfg = load_config()
        cfg.update({"raw": self.raw, "target": self.target, "enabled": self.enabled,
                    "osc_smoothing": self.osc_smoothing})
        save_config(cfg)

    def apply(self, runtime):
        runtime.set_targets(raw=self.raw, osc=self.target)
        runtime.enabled = self.enabled
        runtime.osc_smoothing = self.osc_smoothing

    def pc_host(self):
        return self.raw[0].rsplit(":", 1)[0] if self.raw else None


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(data):
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, CONFIG)


def normalize_address(text, default_port):
    """'host', 'host:port' -> 'host:port' or None when unusable."""
    text = text.strip()
    if not text or " " in text:
        return None
    if ":" in text:
        host, _, port = text.rpartition(":")
        if not host or not port.isdigit() or not 0 < int(port) < 65536:
            return None
        return "%s:%d" % (host, int(port))
    return "%s:%d" % (text, default_port)


# ----------------------------------------------------------------------------- overlay app

class OverlayApp:
    def __init__(self, args):
        self.args = args
        self.settings = Settings(args)
        self.runtime = frameeye_osc.SenderRuntime(args)
        self.settings.apply(self.runtime)
        self.session = None
        self.main_handle = None
        self.thumb_handle = None
        self.gl = None
        self.panel = Panel(PANEL_W, PANEL_H)
        self.stop = threading.Event()
        self.hot = None
        self.editing_text = None      # str while the address keyboard is open
        self.clicks_logged = 0
        self.last_vr_attempt = 0.0
        self.vr_error_logged = None
        self.upload_failures = 0
        self.upload_failed_since = None
        self.vr_frozen = None         # reason, once calls into SteamVR had to stop (see freeze_vr)
        self.frozen_pids = {}
        self.restart_requested = False
        self.visible = False
        self.visible_checked = 0.0
        self.dirty = True

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
            overlay.set_input_method(main, ovr.VROverlayInputMethod_Mouse)
            overlay.set_mouse_scale(main, PANEL_W, PANEL_H)
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
        self.gl = None
        if gl_texture is not None and not self.args.no_gl:
            try:
                self.gl = gl_texture.GlTexture(PANEL_W, PANEL_H)
                log.info("GL texture path: %s", self.gl.info)
            except Exception as e:  # GlError, OSError (missing libs)
                log.warning("GL texture path unavailable (%s); falling back to SetOverlayRaw at %.1f Hz",
                            e, 1.0 / REDRAW_SECONDS_RAW)
                self.gl = None
        self.editing_text = None
        self.hot = None
        self.dirty = True
        self.redraw()
        return True

    def disconnect_vr(self):
        if self.session is None or self.vr_frozen:
            return
        try:
            if self.main_handle:
                if self.gl is not None:
                    self.session.overlay.clear_texture(self.main_handle)
                self.session.overlay.destroy_overlay(self.main_handle)
        except ovr.OpenVRError:
            pass
        if self.gl is not None:
            self.gl.close()
            self.gl = None
        self.session.shutdown()
        self.session = None
        self.main_handle = self.thumb_handle = None

    # -- drawing

    def redraw(self, snapshot=None):
        """Draws the panel and hands it to the compositor. Returns True when that succeeded."""
        if self.session is None or self.vr_frozen:
            return False
        if snapshot is None:
            snapshot = self.runtime.snapshot()
        version = self.session.system.runtime_version()
        draw_panel(self.panel, snapshot, self.settings, self.hot, self.editing_text, version,
                   "GL" if self.gl else "raw")
        try:
            if self.gl is not None:
                self.gl.upload(self.panel.buf)
                self.session.overlay.set_gl_texture(self.main_handle, self.gl.texture)
            else:
                self.session.overlay.set_raw(self.main_handle, self.panel.buf, PANEL_W, PANEL_H)
        except Exception as e:  # OpenVRError, GlError
            if self.gl is not None and getattr(e, "code", None) == ovr.VROverlayError_InvalidTexture:
                self.freeze_vr(str(e))
                return False
            # Other failures happen while the compositor is suspended (standby); log once per episode.
            self.upload_failures += 1
            if self.upload_failed_since is None:
                self.upload_failed_since = time.monotonic()
                log.warning("overlay upload failed (%s); retrying every %.0f s", e, REDRAW_HIDDEN_SECONDS)
            return False
        if self.upload_failed_since is not None:
            log.info("overlay uploads work again after %.0f s (%d failed)",
                     time.monotonic() - self.upload_failed_since, self.upload_failures)
            self.upload_failed_since = None
        self.dirty = False
        return True

    def freeze_vr(self, reason):
        """Stops all calls into SteamVR's client library for the rest of this process.

        When SteamVR cannot create the shared image for the panel texture (seen when vrcompositor ran out
        of IPC SharedObjectManager entries), SetOverlayTexture fails with InvalidTexture and the next
        vrclient call, about a second later, segfaults the process, eye sender included. So the app
        leaves the SteamVR session alone from here on: eye data keeps flowing, the dashboard tab stays
        empty, and once the SteamVR processes it was connected to have exited, the app re-executes
        itself to reconnect with a fresh client library."""
        self.vr_frozen = reason
        self.frozen_pids = steamvr_pids()
        log.error("SteamVR could not share the panel texture (%s). To keep eye data flowing the app stops "
                  "talking to SteamVR until SteamVR restarts; the dashboard tab stays empty until then "
                  "(watching %s)", reason, ", ".join("%s %d" % (c, p) for p, c in sorted(self.frozen_pids.items())))

    # -- controls

    def toggle_sending(self):
        self.settings.enabled = not self.settings.enabled
        self.runtime.enabled = self.settings.enabled
        self.settings.save()
        log.info("sending %s", "enabled" if self.settings.enabled else "paused")

    def toggle_vrchat(self):
        if self.settings.target:
            self.settings.target = None
        else:
            host = self.settings.pc_host()
            if not host:
                log.warning("set a PC address first")
                return
            self.settings.target = "%s:%d" % (host, VRCHAT_OSC_PORT)
        self.settings.apply(self.runtime)
        self.settings.save()
        log.info("direct VRChat OSC %s", self.settings.target or "off")

    def toggle_smoothing(self):
        self.settings.osc_smoothing = not self.settings.osc_smoothing
        self.runtime.osc_smoothing = self.settings.osc_smoothing
        self.settings.save()
        log.info("direct OSC smoothing %s", "on" if self.settings.osc_smoothing else "off")

    def edit_address(self):
        existing = self.settings.raw[0] if self.settings.raw else ""
        try:
            # Buffered (non-minimal) keyboard: SteamVR shows its own input line pre-filled with the
            # current address; the result is read with GetKeyboardText on VREvent_KeyboardDone.
            self.session.overlay.show_keyboard(self.main_handle, "PC address for VRCFT (host or host:port)",
                                               existing_text=existing, max_chars=64, user_value=KEYBOARD_ADDRESS,
                                               flags=ovr.KeyboardFlag_Modal)
        except ovr.OpenVRError as e:
            log.warning("keyboard: %s", e)
            return
        self.editing_text = existing

    def keyboard_done(self):
        text = self.session.overlay.keyboard_text()
        self.editing_text = None
        address = normalize_address(text, DEFAULT_RAW_PORT)
        if not address:
            log.warning("ignored PC address %r", text)
            return
        self.settings.raw = [address]
        if self.settings.target:
            self.settings.target = "%s:%d" % (address.rsplit(":", 1)[0], VRCHAT_OSC_PORT)
        self.settings.apply(self.runtime)
        self.settings.save()
        log.info("PC address set to %s", address)

    def keyboard_event(self, event):
        """Keyboard events arrive on the system queue and/or the overlay queue; handle either."""
        et = event.eventType
        if et == ovr.VREvent_KeyboardDone:
            self.keyboard_done()
        elif et == ovr.VREvent_KeyboardCharInput:
            if self.editing_text is not None:
                try:
                    self.editing_text = self.session.overlay.keyboard_text()
                except ovr.OpenVRError:
                    pass
        elif et == ovr.VREvent_KeyboardClosed:
            self.editing_text = None
        else:
            return False
        self.dirty = True
        return True

    def panel_point(self, event):
        """Mouse event -> top-down panel pixel. OpenVR reports GL-space coordinates (0,0 bottom left)."""
        x, y, _ = event.mouse()
        if self.settings.mouse_origin_bottom:
            y = PANEL_H - y
        return int(x), int(y)

    def handle_click(self, x, y):
        cid = control_at(x, y)
        if self.clicks_logged < 5:
            self.clicks_logged += 1
            log.info("click at %d,%d -> %s", x, y, cid)
        if cid == "sending":
            self.toggle_sending()
        elif cid == "vrchat":
            self.toggle_vrchat()
        elif cid == "address":
            self.edit_address()
        elif cid == "smoothing":
            self.toggle_smoothing()
        self.dirty = True

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
            self.keyboard_event(event)
        while True:
            event = session.overlay.poll_next_overlay_event(self.main_handle)
            if event is None:
                break
            et = event.eventType
            if et == ovr.VREvent_MouseMove:
                x, y = self.panel_point(event)
                hot = control_at(x, y)
                if hot != self.hot:
                    self.hot = hot
                    self.dirty = True
            elif et == ovr.VREvent_MouseButtonDown:
                x, y = self.panel_point(event)
                self.handle_click(x, y)
            elif et in (ovr.VREvent_OverlayHidden, ovr.VREvent_FocusLeave):
                if self.hot is not None:
                    self.hot = None
                    self.dirty = True
            else:
                self.keyboard_event(event)
        return True

    def is_visible(self, now):
        if now - self.visible_checked >= VISIBLE_CHECK_SECONDS:
            self.visible = self.session.overlay.is_visible(self.main_handle)
            self.visible_checked = now
        return self.visible

    # -- main loop

    def run(self):
        log.info("FrameEye Bridge starting (pid %d), targets raw %s vrchat %s, sending %s",
                 os.getpid(), self.settings.raw, self.settings.target, "on" if self.settings.enabled else "paused")
        log.info("direct OSC smoothing %s (gaze median + adaptive filter, eyelid smoothing)",
                 "on" if self.settings.osc_smoothing else "off")
        self.runtime.start()
        next_redraw = 0.0
        next_static = 0.0
        last_key = None
        try:
            while not self.stop.is_set():
                if self.vr_frozen:
                    if self.frozen_pids and not all(pid_alive(p, c) for p, c in self.frozen_pids.items()):
                        log.info("SteamVR processes exited; restarting to reconnect")
                        self.restart_requested = True
                        break
                    self.stop.wait(2.0)
                    continue
                if self.session is None:
                    self.connect_vr()
                    self.stop.wait(0.5 if self.session is None else 0)
                    continue
                sleep = EVENT_POLL_SECONDS
                try:
                    if not self.handle_events():
                        break
                    now = time.monotonic()
                    if not self.is_visible(now):
                        if now >= next_redraw:
                            self.redraw()
                            next_redraw = now + REDRAW_HIDDEN_SECONDS
                        sleep = min(EVENT_POLL_SECONDS, max(0.01, next_redraw - now))
                    else:
                        snap = self.runtime.snapshot()
                        key = (snap.get("seq"), snap.get("state"))
                        interval = REDRAW_SECONDS if self.gl is not None else REDRAW_SECONDS_RAW
                        if ((key != last_key or self.dirty) and now >= next_redraw) or now >= next_static:
                            last_key = key
                            ok = self.redraw(snap)
                            next_static = now + (REDRAW_STATIC_SECONDS if ok else REDRAW_HIDDEN_SECONDS)
                            next_redraw = now + (interval if ok else REDRAW_HIDDEN_SECONDS)
                        sleep = 0.008
                except ovr.OpenVRError as e:
                    log.warning("SteamVR call failed, reconnecting: %s", e)
                    self.disconnect_vr()
                    continue
                self.stop.wait(sleep)
        finally:
            if self.vr_frozen:
                # No vrclient calls at all, not even VR_Shutdown, and no C++ exit handlers.
                self.runtime.stop()
                log.info("FrameEye Bridge stopped (SteamVR session left untouched)")
                logging.shutdown()
                if self.restart_requested:
                    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
                os._exit(0)
            self.disconnect_vr()
            self.runtime.stop()
            log.info("FrameEye Bridge stopped")


# ----------------------------------------------------------------------------- SteamVR processes

def steamvr_pids():
    """{pid: name} of the running vrserver and vrcompositor processes (Linux /proc)."""
    found = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if entry.isdigit():
            try:
                with open("/proc/%s/comm" % entry) as fh:
                    name = fh.read().strip()
            except OSError:
                continue
            if name in ("vrserver", "vrcompositor"):
                found[int(entry)] = name
    return found


def pid_alive(pid, name):
    try:
        with open("/proc/%d/comm" % pid) as fh:
            return fh.read().strip() == name
    except OSError:
        return False


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
                "description": "Forwards the Steam Frame's eye tracking (per-eye gaze, eyelid openness) to "
                               "VRCFaceTracking on the PC",
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
    lines = ["manifest: %s" % MANIFEST, "config: %s -> %s" % (CONFIG, json.dumps(load_config())),
             "config dir: %s" % cfg, "running instance (lock): pid %d" % running_pid()]
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
    parser.description = "FrameEye Bridge: Steam Frame eye tracking for VRCFaceTracking, as a SteamVR dashboard app."
    parser.add_argument("--install", action="store_true",
                        help="write manifest + config with the given sender options, register with SteamVR, "
                             "enable auto-launch and start")
    parser.add_argument("--uninstall", action="store_true", help="remove the SteamVR registration and stop")
    parser.add_argument("--status", action="store_true", help="print registration state and exit")
    parser.add_argument("--no-launch", action="store_true", help="with --install: do not start the overlay now")
    parser.add_argument("--no-gl", action="store_true", help="force the SetOverlayRaw path (debugging)")
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
            cfg = load_config()
            if args.raw:
                cfg["raw"] = [r for r in args.raw if r and r != "none"]
            if args.target:
                cfg["target"] = None if args.target == "none" else args.target
            if not cfg.get("raw") and not cfg.get("target"):
                parser.error("--install needs a PC address to record, e.g. --raw <pc-ip>:9021")
            cfg.setdefault("enabled", True)
            save_config(cfg)
            print("wrote %s (%s) and %s" % (write_manifest(run_args), " ".join(run_args) or "no arguments", CONFIG))
        appconfig, appcfg = register_files(enable)
        print("%s %s and %s" % ("updated" if enable else "cleaned", appconfig, appcfg))
        print(register_live(enable, launch=not args.no_launch, library=args.openvr_lib, run_args=run_args))
        return 0

    lock = acquire_lock()
    if lock is None:
        log.info("another FrameEye Bridge instance is running (pid %d); exiting", running_pid())
        return 0

    try:
        app = OverlayApp(args)
    except ValueError as e:
        log.error("%s", e)
        lock.close()
        return 1
    if not app.settings.raw and not app.settings.target:
        log.warning("no PC address configured yet; enter it in the dashboard tab (PC address field)")

    def request_stop(signum, _frame):
        log.info("signal %d, stopping", signum)
        app.stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    sys.setswitchinterval(0.001)  # renders must not hold the GIL away from the sender thread for long
    app.run()
    lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
