# SPDX-License-Identifier: MIT
"""The XU half of the VIVE Facial Tracker driver (spec §4.5, §4.7, §4.12), a mixin of vft.VftDriver: the XU
protocol (SET_CUR, then GET_CUR polls until the 0x56 ack), Block A's XU sequence and Block C's XU part.

Cadence (§7.2 model): the device counts MAGICs since the last STREAM_ON; REG_WRITEs after MAGIC #2 are buffered
and latch at MAGIC #3. Block C must start from a reset count, so Block A records how far it got (`_phase`, set as
each MAGIC's SET_CUR goes out) and, when it ends early, leaves the device where Block C expects it:
- "magic1" (MAGIC #1 may be at the device, MAGIC #2 is not): STREAM_ON goes out first (tolerated);
- "magic2" (MAGIC #2 out, MAGIC #3 not): the ON writes stay buffered for Block C's recovery prefix (step 2b);
- "magic3" (MAGIC #3 out, STREAM_ON not): STREAM_ON goes out first.
A stop (`abort`, set by request_stop()) abandons Block A at the next control-transfer boundary (Aborted), except in
the cadence window ("magic2", "magic3"): a stop there waits for STREAM_ON's SET_CUR, so a normal tracker runs the
whole of Block A and then the normal Block C. That wait is bounded (§4.6 stop budget): once the stop is seen, a
window command waits at most teardown_ack_ms for its ack, and no window command starts later than STOP_WINDOW_S
after the stop; either limit ends Block A as above. A stop always ends Block A with Aborted, never as a device
failure. Block C itself is never abandoned, but with a stop pending its XU part ends at the first command that finds
EP0 silent (an ETIMEDOUT), also at the tolerated STREAM_OFF and at S4's GET_LEN after a SET_CUR; S4's GET_LEN before
a command is not sent to a silent EP0 (_c_ends): every further request would wait out its own timeout. There is no
automatic per-command retry: a retried MAGIC could change the cadence.
"""
from . import uvc
from .usbfs import DeviceGone, errno_name

__all__ = ["XuTimeout", "XuError", "Aborted", "XuBlocks", "STOP_WINDOW_S"]

STOP_WINDOW_S = 0.5                     # the rest of the cadence window once a stop is seen (§4.6 stop budget)
_WINDOW = ("magic2", "magic3")          # Block A from MAGIC #2's SET_CUR until STREAM_ON's: a stop waits, bounded
_WAITS = _WINDOW + ("stream_on",)       # ... and the STREAM_ON that ends it or restarts the cadence: always sent


class XuTimeout(Exception):
    """An XU command was not acknowledged within its ack time (`cmd` names it)."""


class XuError(Exception):
    """An XU command failed: a STALL (code = REQUEST_ERROR_CODE) or an error ack byte (code)."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class Aborted(Exception):
    """A stop request abandoned an activation or negotiation at a control-transfer boundary."""


def _soft(fn, *args):
    """(fn(*args), None), or (None, OSError) for an error other than DeviceGone (which propagates)."""
    try:
        return fn(*args), None
    except DeviceGone:
        raise
    except OSError as e:
        return None, e


class XuBlocks:
    """Uses the driver's dev, cfg, log, n, identity, unit, strategy, abort, ep0_silent, _rhythm, _current, _in_teardown,
    _phase, _stop_t and its _now, _sleep, _ctrl_in, _ctrl_out and _error_code."""

    # -- stops

    def _stop_pending(self):
        """True once a stop was requested; notes when the driver first saw it (the start of the window's limit)."""
        if not self.abort.is_set():
            return False
        if self._stop_t is None:
            self._stop_t = self._now()
        return True

    def _check_abort(self):
        if not self._in_teardown and self._phase not in _WAITS and self._stop_pending():
            raise Aborted("stop requested")

    def _window_spent(self):
        """In the cadence window with a stop pending for STOP_WINDOW_S: no further window command (§4.6)."""
        return self._phase in _WINDOW and not self._in_teardown and self._stop_pending() and \
            self._now() >= self._stop_t + STOP_WINDOW_S

    def _ack_limit(self, deadline, t_ack):
        """A command's ack deadline; in the cadence window with a stop pending, at most teardown_ack_ms after its
        SET_CUR and STOP_WINDOW_S after the stop."""
        if self._phase in _WINDOW and not self._in_teardown and self._stop_pending():
            return min(deadline, t_ack + self.cfg.teardown_ack_ms / 1000.0, self._stop_t + STOP_WINDOW_S)
        return deadline

    def _c_ends(self):
        """Block C with a stop pending and EP0 silent (a transfer ended with ETIMEDOUT, none answered since): its XU
        part ends, whichever transfer it was (§4.6 stop budget). Without a stop, Block C goes on trying (IR off)."""
        return self._in_teardown and self.ep0_silent and self.abort.is_set()

    # -- XU protocol (§4.5)

    def _rhythm_len(self, after):
        """S4's GET_LEN before and after a SET_CUR (SRanipal rhythm), its error ignored. Where Block C ends (_c_ends)
        the one before is not sent (the SET_CUR still is), and an ETIMEDOUT of the one after ends the command."""
        if not after and self._c_ends():
            return
        _, e = _soft(self._ctrl_in, uvc.xu_setup(uvc.GET_LEN, 2, self.unit))
        if e is not None and after and self._c_ends():
            raise XuError("XU GET_LEN: %s" % errno_name(e))

    def _xu(self, cmd, ack_ms):
        """SET_CUR(cmd padded to n), then poll GET_CUR: (ack, reply, busy polls, echo16)."""
        n = self.n or (self.identity.get_len if self.identity else len(cmd))
        padded = bytes(cmd) if len(cmd) == n else uvc.pad(cmd, n)
        if self._rhythm:
            self._rhythm_len(False)                     # SRanipal rhythm: GET_LEN before ...
        _, e = _soft(self._ctrl_out, uvc.xu_setup(uvc.SET_CUR, n, self.unit), padded)
        if e is not None:
            raise XuError("XU SET_CUR %s: %s" % (padded[:2].hex(), errno_name(e)), self._error_code(e))
        if self._rhythm:
            self._rhythm_len(True)                      # ... and after the SET_CUR
        if ack_ms <= 0:
            return "not_polled", b"", 0, None
        t_ack = self._now()
        deadline, busy = t_ack + ack_ms / 1000.0, 0
        while True:
            reply, e = _soft(self._ctrl_in, uvc.xu_setup(uvc.GET_CUR, n, self.unit))
            if e is not None:
                raise XuError("XU GET_CUR %s: %s" % (padded[:2].hex(), errno_name(e)), self._error_code(e))
            ack, _, echo16 = uvc.parse_ack(padded, reply)
            if ack is uvc.Ack.DONE:
                return "done", reply, busy, echo16
            if ack is not uvc.Ack.BUSY:
                code = reply[0] if reply else None
                raise XuError("XU %s answered %s" % (padded[:2].hex(), "0x%02x" % code if reply else "nothing"),
                              code)
            busy += 1
            limit = self._ack_limit(deadline, t_ack)
            if self._now() >= limit:
                raise XuTimeout("XU %s not acknowledged within %d ms%s" % (
                    padded[:2].hex(), round((limit - t_ack) * 1000), " (stop pending)" if limit < deadline else ""))
            self._check_abort()
            self._sleep(0.001, abortable=False)

    def xu(self, cmd: bytes, ack_ms: int) -> tuple:
        """One XU command at the session's length (the probe's REG_READ): ("done", reply) or
        ("not_polled", b""); XuTimeout / XuError / DeviceGone otherwise."""
        ack, reply, _, _ = self._xu(cmd, ack_ms)
        return ack, reply

    def _cmd(self, cmds, name, cmd, ack_ms, phase=None):
        """One command of Block A or C with its report entry; `phase` (Block A's MAGICs) starts at its SET_CUR."""
        self._check_abort()
        if self._window_spent():
            raise XuTimeout("stop pending for %.1f s: %s not sent" % (STOP_WINDOW_S, name))
        if phase is not None:
            self._phase = phase
        t = self._now()
        try:
            ack, _, busy, echo16 = self._xu(cmd, ack_ms)
        except (XuTimeout, XuError) as e:
            cmds.append({"name": name, "ms": round((self._now() - t) * 1000, 1), "busy": None,
                         "ack": "timeout" if isinstance(e, XuTimeout) else "error", "echo16": None})
            e.cmd = name
            raise
        cmds.append({"name": name, "ms": round((self._now() - t) * 1000, 1), "busy": busy, "ack": ack,
                     "echo16": echo16})

    def _writes(self, cmds, n, pwm, current, ack):
        for a, v in uvc.ir_regs(pwm, current):          # the 11 REG_WRITEs (ON or OFF)
            self._cmd(cmds, "REG %02x=%02x" % (a, v), uvc.reg_write(a, v, n), ack)

    def _stream(self, cmds, on, s):
        """STREAM_OFF/ON: polled <= stream_poll_ms, or sent without polling (S5); tolerated either way, except an
        ETIMEDOUT in Block C with a stop pending (_c_ends)."""
        name, cmd = ("STREAM_ON" if on else "STREAM_OFF"), uvc.stream_cmd(on, self.n)
        polled = s is None or s.stream_ack_poll
        try:
            self._cmd(cmds, name, cmd, self.cfg.stream_poll_ms if polled else 0)
        except XuTimeout:
            return "timeout"
        except XuError as e:
            if self._c_ends():
                raise
            return "error:%02x" % (e.code if e.code is not None else 0xFF)
        return "done" if polled else "not_polled"

    def _restart_cadence(self, cmds, s):
        """STREAM_ON (tolerated; a pending stop neither skips nor cuts it): the device's MAGIC count starts again.
        It ends the cadence window, and goes out first when Block A ends with MAGIC #1 or #3 at the device."""
        self._phase = "stream_on"
        try:
            return self._stream(cmds, True, s)
        finally:
            self._phase = None

    # -- Block A (§4.7)

    def _block_a(self, s, pwm, current, n):
        """Block A's XU sequence: MAGIC, STREAM_OFF, 250 ms, MAGIC, 11 REG_WRITE, 100 ms, MAGIC, STREAM_ON, 500 ms;
        (cmds, stream_off_ack, stream_on_ack). Ending early it leaves the cadence as Block C expects it (module
        docstring); a pending stop makes the end Aborted, also when the window's limit cut a command."""
        cmds, ack, off = [], self.cfg.ack_timeout_ms, None
        try:
            self._cmd(cmds, "MAGIC#1", uvc.magic(n), ack, "magic1")
            off = self._stream(cmds, False, s)
            self._sleep(0.25)
            self._cmd(cmds, "MAGIC#2", uvc.magic(n), ack, "magic2")      # its stop check: the last stop point
            self._writes(cmds, n, pwm, current, ack)
            self._sleep(0.1, abortable=False)
            self._cmd(cmds, "MAGIC#3", uvc.magic(n), ack, "magic3")
        except (Aborted, XuTimeout, XuError) as e:
            if self._phase in ("magic1", "magic3"):     # MAGIC #1 or #3 may have counted: STREAM_ON resets the count
                self._restart_cadence(cmds, s)          # ("magic2": the buffered ON writes wait for Block C's prefix)
            if not isinstance(e, Aborted) and self._stop_pending():
                raise Aborted("stop requested (%s)" % e) from e
            raise
        on = self._restart_cadence(cmds, s)             # STREAM_ON's SET_CUR ends the cadence window
        self._sleep(0.5)                                # a stop that waited in the window takes effect here
        return cmds, off, on

    # -- Block C's XU part (§4.12)

    def _teardown_xu(self):
        n, s, ack = self.n, self.strategy, self.cfg.teardown_ack_ms
        if n is None:
            return False                                # never activated: nothing to switch off
        cmds, recover, self._phase = [], self._phase == "magic2", None
        try:
            if recover:                                 # Block A ended after MAGIC #2: ON writes are buffered;
                self._writes(cmds, n, 0x00, self._current, ack)     # OFF writes join them and one MAGIC
                self._sleep(0.1, abortable=False)                  # (the third) latches all in order
                self._cmd(cmds, "MAGIC", uvc.magic(n), ack)
            self._cmd(cmds, "MAGIC", uvc.magic(n), ack)
            self._stream(cmds, False, s)                # tolerated (S5: not polled), unless _c_ends
            self._sleep(0.1, abortable=False)
            self._cmd(cmds, "MAGIC", uvc.magic(n), ack)
            self._writes(cmds, n, 0x00, self._current, ack)
            self._sleep(0.2, abortable=False)
            if self.cfg.teardown_magic3:
                self._cmd(cmds, "MAGIC", uvc.magic(n), ack)
        except (XuTimeout, XuError) as e:
            self.log.w("usb", "teardown xu aborted", cmd=getattr(e, "cmd", "?"), error=str(e))
            return False
        return True
