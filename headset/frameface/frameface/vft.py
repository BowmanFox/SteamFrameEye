# SPDX-License-Identifier: MIT
"""The VIVE Facial Tracker driver (spec §4.5-§4.9, §4.12): identity, Block A (XU activation),
Block B (probe/commit), the URB pump (usbfs.UrbQueue) and Block C (teardown, LOST path, port reset).
The XU protocol and the XU sequences of Blocks A and C are the mixin xublocks.XuBlocks.

Every control transfer goes through UsbDevice; every sleep inside a control sequence goes through
_sleep(), which in inline mode keeps pumping URBs (so the teardown XU sequence runs while URBs are
reaped and resubmitted, exactly like the reaper thread does in threaded mode). A stop request
(`abort`) abandons an activation or negotiation at the next control-transfer boundary (Aborted),
except from MAGIC #2 until STREAM_ON's SET_CUR (the cadence window; the wait is bounded, §4.6 stop
budget, see xublocks); Block C itself is never abandoned. An ESHUTDOWN (flushed URBs) is a loss only
once confirm_gone() sees one.

EP0 (`ep0_silent`, §4.6 stop budget, §4.12 step 11): a control request of the session that ends with ETIMEDOUT makes
the tracker's control endpoint silent until one completes (or STALLs). While it is silent or a stop is pending, Block C
skips step 11's CLEAR_HALT and the reaper keeps a halt after an EPIPE (_halt_kept: a CLEAR_HALT waits up to the kernel's
5 s whenever EP0 stops answering). With a stop pending, a control transfer times out after teardown_ack_ms instead of
ctrl_timeout_ms, OPENING's and Block B's transfers to a silent EP0 are not sent (_ctrl), and Block C's XU part ends at
its first ETIMEDOUT (xublocks); Block A's XU commands and Block C's first transfer always go out.
"""
from dataclasses import dataclass
import errno
import threading
from typing import Callable

from . import uvc
from .descriptors import DescriptorError, diff_expected, parse, sha1_hex, unsupported
from .policy import IR_LEVELS, Failure
from .usbfs import SPEEDS, DeviceGone, UrbQueue, errno_name
from .xublocks import Aborted, XuBlocks, XuError, XuTimeout, _soft

__all__ = ["XuTimeout", "XuError", "ProbeReject", "ActivationError", "Aborted", "DriverConfig", "Identity",
           "Activation", "StreamParams", "VftDriver", "level_name"]


class ProbeReject(Exception):
    """Probe/commit was refused (STALL at every length, or another format/frame answered)."""


class ActivationError(Exception):
    """OPENING failed in a way the policy classifies: PERMISSION, BUSY or UNSUPPORTED (`failure`)."""

    def __init__(self, failure, message="", **info):
        super().__init__(message or str(failure))
        self.failure = failure
        self.info = info


@dataclass
class DriverConfig:
    n_urbs: int = 16
    urb_bytes: int = 0
    ack_timeout_ms: int = 1000
    stream_poll_ms: int = 100
    teardown_ack_ms: int = 200
    ctrl_timeout_ms: int = 1000
    interval: int = 166666
    teardown_magic3: bool = False           # Block C step 7b (§4.12)


@dataclass
class Identity:
    get_len: int
    xu_info: int
    xu_unit: int
    layout: object
    speed: int
    capabilities: int
    warnings: list
    set_config: str = "skipped"             # "ok" | "skipped" | "error:<errno name>"


@dataclass
class Activation:
    strategy: str
    ir_level: str
    xu_len: int
    cmds: list
    stream_off_ack: str                     # "done" | "timeout" | "error:<hex>" | "not_polled" ("deferred": S3)
    stream_on_ack: str
    total_ms: float


@dataclass
class StreamParams:
    probe_hex: str
    probe_len: int
    max_frame: int
    max_payload: int
    interval: int
    framing_info: int


class _NullLog:
    def d(self, component, event, **kv):
        pass

    i = w = e = d


def level_name(current: int) -> str:
    """The IR level whose current this is ("L0".."L3"), else "custom"."""
    return next((k for k, v in IR_LEVELS.items() if v == current), "custom")


class VftDriver(XuBlocks):
    """One tracker session: open() -> activate() -> negotiate() -> start() -> pump()... -> teardown().
    A new session (new fd) is needed after teardown(); reset_port() needs it closed.

    Threaded mode (inline=False): start() spawns the reaper thread of `urbs` (usbfs.UrbQueue, pump(0.1)
    in a loop); its error ends it and stays in urbs.reaper_error (DeviceGone also sets `gone`),
    urbs.reaper_beat is its heartbeat and urbs.on_reaper_start (if set) runs first in that thread
    (CPU affinity and nice). teardown() joins it. Inline mode starts no thread: the caller pumps.
    The watchdog data (urbs.epipe_times, xfer_err_times, starved_times, first_data_t) lives in `urbs`."""

    def __init__(self, dev, node, clock, log, cfg: DriverConfig, inline: bool = False):
        self.dev, self.node, self.clock, self.cfg, self.inline = dev, node, clock, cfg, inline
        self.log = log if log is not None else _NullLog()
        self.identity = self.layout = self.strategy = self.sink = self.last_activation = None
        self.n = None                           # the activation's XU length (Block C reuses it)
        self.unit, self.ep, self.vs_if = uvc.XU_UNIT, 0x81, 1
        self.urbs = UrbQueue(dev, clock, self.log, self._recover, inline)
        self.abort = threading.Event()          # stop request (SIGTERM): abandon Block A/B
        self.teardown_path = None               # "clean" | "lost" after teardown()
        self.ep0_silent = False                 # an EP0 request ended with ETIMEDOUT and none answered since (_ep0)
        self._gone = self._rhythm = self._in_teardown = False
        self._phase = None                      # how far Block A got: "magic1" | "magic2" | "magic3" (xublocks)
        self._stop_t = None                     # when the driver first saw the stop request
        self._current = IR_LEVELS["L0"]

    stats = property(lambda self: self.urbs.stats)
    inflight = property(lambda self: self.urbs.inflight)
    urb_bytes = property(lambda self: self.urbs.size)
    slots = property(lambda self: self.urbs.slots)

    @property
    def gone(self):
        """DeviceGone was seen: only the LOST path is left."""
        return self._gone or self.urbs.gone

    # -- helpers

    def _now(self):
        return self.clock.monotonic()

    def _sleep(self, s, abortable=True):
        """Sleep s seconds (inline: pumping URBs). A stop raises Aborted if abortable; it ends a sleep inside the
        cadence window once the window's stop limit is spent (xublocks)."""
        end = self._now() + s
        while True:
            if abortable:
                self._check_abort()
            elif self._window_spent():
                return
            rest = end - self._now()
            if rest <= 0:
                return
            if self.inline and self.urbs.slots:
                self.urbs.pump(0)
            self.clock.sleep(min(rest, 0.005 if self.inline else 0.05))

    def _ctrl_in(self, setup):
        return self._ctrl(self.dev.control_in, setup)

    def _ctrl_out(self, setup, data):
        return self._ctrl(self.dev.control_out, setup, data)

    def _ctrl(self, fn, *args):
        """One control transfer of the driver; its outcome tracks EP0 (_ep0). Its timeout is cfg.ctrl_timeout_ms, and
        teardown_ack_ms once a stop is pending: no transfer of the stop path waits longer than an XU ack may (§4.6 stop
        budget; a healthy tracker answers within a few ms). With a stop pending, a transfer to a silent EP0 outside
        Block A's commands (`_phase` set) and Block C (OPENING's GET_INFO and GET_LEN, Block B, the raw SET_INTERFACE,
        the probe's raw queries) is not sent: it fails at once with ETIMEDOUT, as it would after its timeout. Block A
        keeps its sequence; Block C ends at its first ETIMEDOUT (xublocks._c_ends)."""
        tmo = self.cfg.ctrl_timeout_ms
        if self.abort.is_set():
            if self.ep0_silent and not self._in_teardown and self._phase is None:
                raise OSError(errno.ETIMEDOUT, "not sent: EP0 silent and a stop pending")
            tmo = min(tmo, self.cfg.teardown_ack_ms)
        try:
            r = fn(*args, tmo)
        except OSError as e:                    # ESHUTDOWN: flushed, gone only if confirm_gone() says so
            self._ep0(e)
            if e.errno == errno.ESHUTDOWN and not isinstance(e, DeviceGone) and self.urbs.confirm_gone():
                raise DeviceGone(errno.ENODEV, "control transfer ESHUTDOWN and the device is gone") from None
            raise
        self._ep0(None)
        return r

    def _ep0(self, e):
        """Track EP0 from an EP0 request's outcome (e: None if it completed, else its OSError): ETIMEDOUT makes it
        silent; a completed request or a STALL (EPIPE, the device's own answer) makes it answering; any other error
        leaves it as it was. Thread-safe enough: one bool, the reaper's CLEAR_HALT may write it too."""
        if e is None or e.errno == errno.EPIPE:
            self.ep0_silent = False
        elif e.errno == errno.ETIMEDOUT:
            self.ep0_silent = True

    def _error_code(self, e):
        """VC REQUEST_ERROR_CODE after a STALL (for the log), or None."""
        if e.errno != errno.EPIPE:
            return None
        b, err = _soft(self._ctrl_in, uvc.vc_error_code_setup())
        return b[0] if b else None

    # -- OPENING (§4.6)

    def open(self, set_config: bool = False) -> Identity:
        """open(O_RDWR), GET_SPEED, GET_CAPABILITIES, [SETCONFIGURATION(1)], CLAIM 0 and 1 (EBUSY:
        GETDRIVER, then DISCONNECT_CLAIM except usbfs), descriptors, XU GET_INFO and GET_LEN. Raises
        ActivationError (PERMISSION, BUSY, UNSUPPORTED) or DeviceGone; the fd is closed on every error.
        A driver reused after teardown() starts the new session with fresh stream state."""
        if self.teardown_path is not None:
            self._new_session()
        try:
            return self._open(set_config)
        except BaseException:
            self._close_quietly()
            raise

    def _new_session(self):
        hook = self.urbs.on_reaper_start        # a fresh queue: counters, first_data_t, gone; the same hook
        self.urbs = UrbQueue(self.dev, self.clock, self.log, self._recover, self.inline)
        self.urbs.on_reaper_start = hook
        self.identity = self.layout = self.strategy = self.sink = self.last_activation = self.n = None
        self.teardown_path = self._phase = self._stop_t = None
        self._gone = self._rhythm = self._in_teardown = self.ep0_silent = False

    def _open(self, set_config):
        try:
            self.dev.open()
        except PermissionError as e:
            raise ActivationError(Failure.PERMISSION, "open %s: %s" % (self.node.devpath, errno_name(e)),
                                  errno=e.errno) from None
        except FileNotFoundError:
            raise DeviceGone(errno.ENODEV, "node vanished") from None
        code, err = _soft(self.dev.speed)
        caps, err2 = _soft(self.dev.capabilities)
        for e in (err, err2):
            if e is not None:
                if e.errno in (errno.EPERM, errno.EACCES):
                    raise ActivationError(Failure.PERMISSION, "ioctl %s" % errno_name(e), errno=e.errno)
                raise e
        speed = SPEEDS.get(code, code)
        self.log.i("usb", "opened", speed=speed, capabilities="0x%x" % caps)
        sc = "skipped"
        if set_config:
            _, err = _soft(self.dev.set_configuration, 1)
            self._ep0(err)                      # SET_CONFIGURATION is an EP0 request (kernel timeout 5 s)
            sc = "ok" if err is None else "error:" + errno_name(err)
            if err is not None:
                self.log.w("usb", "setconfig failed errno=%s" % errno_name(err))
        for i in (0, 1):
            self._claim(i)
        try:
            layout = parse(self.node.descriptors)
        except DescriptorError as e:
            raise ActivationError(Failure.UNSUPPORTED, "descriptors: %s" % e) from None
        why = unsupported(layout)
        if why:
            raise ActivationError(Failure.UNSUPPORTED, why)
        warnings = list(layout.warnings) + diff_expected(layout, "ss" if speed >= 5000 else "hs")
        for w in warnings:
            self.log.w("usb", "descriptor %s" % w)
        self.layout, self.ep, self.vs_if = layout, layout.vs_endpoint, layout.vs_interface
        info, get_len, self.unit = self._get_len(layout)
        self.identity = Identity(get_len, info, self.unit, layout, speed, caps, warnings, sc)
        self.log.i("usb", "identity", get_len=get_len, xu_unit=self.unit, ep="0x%02x" % self.ep,
                   max_packet=layout.max_packet, bcd_uvc="0x%04x" % layout.bcd_uvc,
                   desc_sha1=sha1_hex(self.node.descriptors))
        return self.identity

    def _claim(self, i):
        _, e = _soft(self.dev.claim, i)
        if e is None:
            return
        if e.errno in (errno.EPERM, errno.EACCES):
            raise ActivationError(Failure.PERMISSION, "claim %d: %s" % (i, errno_name(e)))
        if e.errno != errno.EBUSY:
            raise e
        driver, _ = _soft(self.dev.get_driver, i)
        self.log.w("usb", "busy interface=%d driver=%s" % (i, driver))
        _, e = _soft(self.dev.disconnect_claim, i)       # flags EXCEPT_DRIVER, "usbfs": never another usbfs owner
        if e is not None:
            raise ActivationError(Failure.BUSY, "interface %d busy (%s)" % (i, driver or errno_name(e)),
                                  interface=i, driver=driver)
        self.log.i("usb", "took interface from kernel driver", interface=i, driver=driver)

    def _get_len(self, layout):
        units = [layout.xu_unit] + [u.unit_id for u in layout.xu_units if u.unit_id != layout.xu_unit]
        for unit in units:
            b, e = _soft(self._ctrl_in, uvc.xu_setup(uvc.GET_INFO, 1, unit))
            info = b[0] if b else None
            if e is not None:
                self.log.w("usb", "get_info failed", unit=unit, errno=errno_name(e))
            value = None
            for attempt in (0, 1):
                b, e = _soft(self._ctrl_in, uvc.xu_setup(uvc.GET_LEN, 2, unit))
                if e is None:
                    value = int.from_bytes(b[:2], "little") if len(b) >= 2 else None
                    break
                self.log.w("usb", "get_len failed", unit=unit, errno=errno_name(e), error_code=self._error_code(e))
                if attempt == 0:
                    self._sleep(0.1, abortable=False)
            if value in (64, 384):
                return info, value, unit
            if value is not None and 16 <= value <= 1024:
                self.log.w("usb", "get_len unexpected value=%d (expected 64 or 384); using it" % value)
                return info, value, unit
            self.log.w("usb", "get_len invalid", unit=unit, value=value)
        raise ActivationError(Failure.UNSUPPORTED, "no extension unit answered GET_LEN")

    def _set_interface(self):
        """SETINTERFACE(vs_if, 0), an EP0 request (kernel timeout 5 s); if it fails, the raw request (_ctrl: not sent
        with a stop pending once the ioctl has found EP0 silent)."""
        self._check_abort()
        if self.urbs.inflight:
            raise RuntimeError("SETINTERFACE with %d URBs in flight" % self.urbs.inflight)
        _, e = _soft(self.dev.set_interface, self.vs_if, 0)
        self._ep0(e)
        if e is not None:
            self.log.w("usb", "setinterface failed errno=%s; raw request" % errno_name(e))
            _, e = _soft(self._ctrl_out, uvc.set_interface_setup(self.vs_if, 0), b"")
            if e is not None:
                self.log.w("usb", "raw set_interface failed errno=%s" % errno_name(e))

    # -- Block A (§4.7)

    def activate(self, s, pwm: int, current: int, n: int) -> Activation:
        """Block A at XU length n: [SETINTERFACE], MAGIC, STREAM_OFF, 250 ms, MAGIC, 11 REG_WRITE,
        100 ms, MAGIC, STREAM_ON, 500 ms, [SETINTERFACE (S5)]. S3 (xu_between) runs only its
        SETINTERFACE here; negotiate() runs Block A between the probe and the commit."""
        self.strategy, self.n, self._current, self._rhythm = s, n, current, bool(s.rhythm)
        if s.si_before:
            self._set_interface()
        if s.xu_between:
            return Activation(s.name, level_name(current), n, [], "deferred", "deferred", 0.0)
        return self._activation(s, pwm, current, n)

    def _activation(self, s, pwm, current, n):
        """Block A's XU sequence (xublocks: the cadence window, a bounded stop, the cadence repair after an early
        end), [SETINTERFACE (S5)], then the activation report."""
        t0 = self._now()
        cmds, off, on = self._block_a(s, pwm, current, n)
        if s.si_mid:
            self._set_interface()
        act = Activation(s.name, level_name(current), n, cmds, off, on, round((self._now() - t0) * 1000, 1))
        self.last_activation = act
        self.log.i("usb", "activation", strategy=s.name, xu_len=n, ir=act.ir_level, ms=int(act.total_ms),
                   busy_polls=sum(c["busy"] or 0 for c in cmds), stream_off_ack=off, stream_on_ack=on)
        return act

    # -- Block B (§4.8)

    def _probe_get(self, request, length):
        b, e = _soft(self._ctrl_in, uvc.vs_setup(request, uvc.VS_PROBE, length, self.vs_if))
        if e is not None:
            self.log.d("usb", "probe get failed", request="0x%02x" % request, length=length, errno=errno_name(e))
        return b

    def negotiate(self, s, pwm: int, current: int, n: int) -> StreamParams:
        """Block B: probe (EPIPE: the other lengths, 48 then 26 for UVC 1.1), fix-ups, [S3: Block A],
        commit (EPIPE: the other lengths once each), [SETINTERFACE]. ProbeReject if refused."""
        layout = self.layout
        fmt, frm = layout.format_index, layout.frame_index
        first = uvc.probe_length(layout.bcd_uvc)
        g = length = None
        for length in [first] + [x for x in (48, 34, 26) if x != first]:
            self._check_abort()
            base = self._probe_get(uvc.GET_CUR, length)
            if base is None:
                base = self._probe_get(uvc.GET_DEF, length)
            req = uvc.request_probe(base, length, fmt, frm, self.cfg.interval)
            _, e = _soft(self._ctrl_out, uvc.vs_setup(uvc.SET_CUR, uvc.VS_PROBE, length, self.vs_if), req)
            if e is not None:
                self.log.w("usb", "probe set_cur failed", length=length, errno=errno_name(e),
                           error_code=self._error_code(e))
                if e.errno == errno.EPIPE:
                    continue
                raise ProbeReject("PROBE SET_CUR: %s" % errno_name(e))
            ans = self._probe_get(uvc.GET_CUR, length)
            if ans is None:
                raise ProbeReject("PROBE GET_CUR failed after SET_CUR")
            g = uvc.unpack_probe(ans)
            if (g.format_index, g.frame_index) != (fmt, frm):
                raise ProbeReject("probe answered format %d frame %d" % (g.format_index, g.frame_index))
            break
        if g is None:
            raise ProbeReject("PROBE SET_CUR stalled at every length")
        for req in (uvc.GET_MAX, uvc.GET_MIN):
            self._probe_get(req, length)                    # logged only (SRanipal does it)
        device_size = g.max_video_frame_size
        g, warns = uvc.fix_probe(g, layout.frame_buffer_size)
        for w in warns:
            self.log.w("usb", w)
        if s.xu_between:
            self._activation(s, pwm, current, n)
        for clen in [length] + [x for x in (48, 34, 26) if x != length]:
            self._check_abort()
            _, e = _soft(self._ctrl_out, uvc.vs_setup(uvc.SET_CUR, uvc.VS_COMMIT, clen, self.vs_if),
                         uvc.pack_probe(g, clen))
            if e is None:
                break
            if e.errno != errno.EPIPE:
                raise ProbeReject("COMMIT: %s" % errno_name(e))
            self.log.w("usb", "commit stalled", length=clen, error_code=self._error_code(e))
        else:
            raise ProbeReject("COMMIT stalled at every length")
        if s.si_after:
            self._set_interface()
        params = StreamParams(g.raw.hex(), length, g.max_video_frame_size, g.max_payload, g.frame_interval,
                              g.framing_info)
        self.log.i("usb", "negotiated", max_frame=params.max_frame, max_payload=params.max_payload,
                   interval=params.interval, device_frame=device_size, probe=params.probe_hex)
        return params

    # -- URB pump (§4.9; usbfs.UrbQueue)

    def start(self, params: StreamParams, sink) -> None:
        """Submit n_urbs URBs of cfg.urb_bytes or round_up(max_payload, packet) (cap 64 KiB) at once."""
        packet = self.layout.max_packet or 512
        size = min(65536, self.cfg.urb_bytes or -(-params.max_payload // packet) * packet)
        self.sink = sink
        self.urbs.start(self.cfg.n_urbs, size, self.ep, self.vs_if, sink)
        self.log.i("usb", "streaming", urbs=self.cfg.n_urbs, urb_bytes=self.urbs.size)

    def add_urbs(self, n: int) -> None:
        """Grow the queue by n URBs while streaming (starvation, §4.9)."""
        if self.urbs.add(n):
            self.log.i("usb", "urbs grown", urbs=self.urbs.stats["urbs"])

    def pump(self, timeout_s: float) -> int:
        """Wait up to timeout_s, reap every completion, feed and resubmit; the batch size. DeviceGone on
        ENODEV, an ENODEV status, HUP/ERR with nothing to reap, or a flush (ESHUTDOWN) confirmed as a loss."""
        return self.urbs.pump(timeout_s)

    def _halt_kept(self):
        """Why no CLEAR_HALT goes out (the reaper's, step 11), else None: "ep0_timeout" while EP0 is silent, "stopping"
        once a stop is pending. A CLEAR_HALT waits up to the kernel's 5 s holding the usbfs device lock when EP0 does
        not answer, which no earlier answer rules out (§4.6 stop budget); the next session's SETINTERFACE resets the
        endpoint anyway (every strategy sends one before streaming)."""
        return "ep0_timeout" if self.ep0_silent else "stopping" if self.abort.is_set() else None

    def _recover(self, ep):
        """The reaper's halt recovery after an EPIPE completion (usbfs.UrbQueue, §4.9): CLEAR_HALT(ep), except while
        EP0 is silent or a stop is pending (_halt_kept; it would also hold up the teardown's ioctls). True if sent (the
        URB is resubmitted); False if the halt is kept (the URB is not: it would only stall again). DeviceGone
        propagates."""
        why = self._halt_kept()
        if why is not None:
            self.log.w("usb", "epipe not recovered", why=why)
            return False
        _, e = _soft(self.dev.clear_halt, ep)
        self._ep0(e)
        if e is not None:
            self.log.w("usb", "clear_halt failed errno=%s" % errno_name(e))
        return True

    # -- Block C (§4.12)

    def teardown(self, clean: bool = True, after_xu: "Callable | None" = None) -> None:
        """Block C. clean=True: 30 ms, [11 OFF writes, 100 ms, MAGIC if Block A ended after MAGIC #2 and before
        MAGIC #3], XU off sequence (MAGIC, STREAM_OFF, 100 ms, MAGIC, 11 OFF writes, 200 ms, [MAGIC if teardown_magic3])
        while URBs keep being reaped and resubmitted, after_xu(self), then stop resubmission, DISCARDURB all,
        reap to zero (<= 1 s), CLEAR_HALT (step 11; skipped while EP0 is silent, W usb: clear_halt skipped
        why=ep0_timeout, or a stop is pending, I ... why=stopping), RELEASE 1, RELEASE 0 (host-side at alt 0), join
        the reaper, close. The first XU timeout/error skips to the discard step; ENODEV anywhere, a confirmed loss
        (`gone`; a flush alone is not one) or clean=False takes the LOST path: no control request, discard, reap
        <= 200 ms, close. `teardown_path` tells which path ran ("clean" or "lost")."""
        if self.dev.fd is None:
            self.teardown_path = self.teardown_path or "closed"
            return
        self._in_teardown = True
        path = "clean" if clean and not self.gone else "lost"
        try:
            if path == "clean":
                try:
                    self._sleep(0.03, abortable=False)
                    if self._teardown_xu() and after_xu is not None:
                        try:
                            after_xu(self)
                        except DeviceGone:
                            raise
                        except Exception as e:          # probe readback only: never fatal
                            self.log.w("usb", "after_xu hook failed", error=repr(e))
                except DeviceGone:
                    path, self._gone = "lost", True
            self.urbs.stopping = True
            self.urbs.discard_all()
            self.urbs.drain(1.0 if path == "clean" else 0.2)
            if path == "clean":
                why = self._halt_kept()
                if why is not None:             # every teardown while EP0 is silent; every stop (§4.6 stop budget)
                    (self.log.i if why == "stopping" else self.log.w)("usb", "clear_halt skipped", why=why)
                else:
                    _, e = _soft(self.dev.clear_halt, self.ep)
                    if e is not None:           # ETIMEDOUT: EP0 stopped answering after Block C (the kernel's 5 s)
                        self.log.w("usb", "clear_halt failed errno=%s" % errno_name(e))
                for i in (1, 0):
                    _, e = _soft(self.dev.release, i)
                    if e is not None:
                        self.log.d("usb", "teardown step failed", errno=errno_name(e))
        except DeviceGone:
            path, self._gone = "lost", True
        finally:
            self.urbs.stop_reaper()
            self._close_quietly()
            self.teardown_path = path
            self._in_teardown = False

    def _close_quietly(self):
        if self.dev.fd is not None and not self._in_teardown and not self.gone:
            for i in sorted(self.dev.claimed, reverse=True):     # an OPENING failure: give the claims back
                _soft(self.dev.release, i)
        _soft(self.dev.close)
        self.urbs.forget()

    def reset_port(self) -> None:
        """USBDEVFS_RESET on a fresh fd (the session's fd must be closed; never behind a hub)."""
        if self.dev.fd is not None:
            raise RuntimeError("reset_port needs the session's fd closed (teardown first)")
        if self.node.behind_hub:
            raise RuntimeError("never reset a device behind a hub (%s)" % self.node.sysname)
        self.dev.open()
        try:
            self.dev.reset()
            self.log.w("usb", "reset port sysname=%s" % self.node.sysname)
        finally:
            _soft(self.dev.close)
