# SPDX-License-Identifier: MIT
"""The USB half of the service's state machine (spec §4.6-§4.13), a mixin of service.Service.

OPENING .. STREAMING on the real driver: the strategy ladder and the XU length of each activation,
the stream watchdogs, the §4.13 failure decisions and their flows (TEARDOWN -> BACKOFF, RESETTING,
LOST, FAULT), the IR ladder (dark trials, brown-outs) and profile.json. Every control transfer and
every state change runs in the supervisor (main) thread; the pixel stage (pixels.PixelStage, its own
thread or inline) only records what it saw and asks the supervisor to act through `Act.request`.

Service owns the inputs (presence, worn, battery, the disabled flag), the idle policy, HTTP and
/status; the attributes used here are initialised in Service.__init__.
"""
import math
import os
import threading

from . import uvc
from .assembler import BulkReassembler, FrameAssembler, FramePool
from .descriptors import sha1_hex
from .policy import (IR_LEVELS, LADDER, RESET_GAP_S, STRATEGIES, Failure as F, FailureContext, backoff_s, ir_next,
                     ladder, on_failure, settle_deadline, xu_len_for)
from .pixels import Act
from .quality import HealthTracker
from .state import Profile
from .sysfs import find_owners, node_access, read_power
from .usbfs import DeviceGone, UsbDevice
from .vft import Aborted, ActivationError, DriverConfig, ProbeReject, VftDriver, XuError, XuTimeout

__all__ = ["UsbSupervisor"]

SOFT = (F.STALL, F.FROZEN, F.EPIPE_STORM, F.XFER_STORM)
TESTBAR_RUN, DARK_S, SUPPRESS_S = 30, 2.0, 600.0
SAVE_AFTER_S, SAVE_AFTER_FRAMES, HEALTHY_RESET_S = 10.0, 300, 60.0
B1_WINDOW_S, PERMISSION_WINDOW_S, PERMISSION_RETRY_S, RESET_WAIT_S = 30.0, 5.0, 0.5, 2.0
NO_DATA_S, NO_FRAMES_S, LEARN_GRACE_S, HEARTBEAT_S = 2.0, 3.0, 2.0, 2.0


def _count(times, now, window):
    return sum(1 for t in list(times) if now - t <= window)


def _append(lst, item, keep=2000):
    """Append to one of the service's history lists, keeping the newest `keep` items (weeks of uptime)."""
    lst.append(item)
    if len(lst) > keep:
        del lst[:-keep]


class UsbSupervisor:
    # -- helpers

    def _note(self, **kv):
        if self.trace_note is not None:
            self.trace_note(**kv)

    def _profile(self):
        desc = self.desc
        if desc is None:
            return None
        p = self.store.load_profile(sha1_hex(desc))
        self._last_profile = p
        return p

    def _save_profile(self, **kv):
        if self.desc is None:
            return
        sha = sha1_hex(self.desc)
        p = self.store.load_profile(sha) or Profile(desc_sha1=sha)
        for k, v in kv.items():
            setattr(p, k, v)
        try:
            self.store.save_profile(p)
        except OSError as e:
            self.log.w("state", "profile write failed", error=str(e))
        self._last_profile = p

    def _mark(self, what):
        """store.mark_<what>() on runtime.json; a write error (full or read-only disk) is logged and the
        state machine goes on (an exception here would leave it in a state no tick ever leaves)."""
        try:
            getattr(self.store, "mark_" + what)()
        except OSError as e:
            self.log.w("state", "runtime write failed", mark=what, error=str(e))

    def _queue_profile(self, **kv):
        with self._profile_lock:
            self._profile_updates.update(kv)

    def _apply_profile_updates(self):
        with self._profile_lock:
            kv, self._profile_updates = self._profile_updates, {}
        if kv:
            self._save_profile(**kv)

    def _level(self):
        if self.cfg.ir_level != "auto":
            return self.cfg.ir_level
        p = self._profile() if self.desc is not None else self._last_profile
        return self.level or (p.ir_level if p else "L0")

    def _deadline(self, bt):
        """settle_deadline() from the end of the last session: a clean teardown or a LOST, whichever came
        later (a LOST while the tracker stays attached needs the settle too; a real absence skips it)."""
        rt = self.store.load_runtime()
        ends = [t for t in (rt.last_teardown_bt, rt.last_loss_bt) if t is not None]
        end = max(ends) if ends else None
        return settle_deadline(bt, same_boot=self.store.same_boot(rt), enum_bt=self.enum_bt, start_bt=self.start_bt,
                               dirty_start=self.dirty_start, last_teardown_bt=end, last_reset_bt=rt.last_reset_bt,
                               absent_since_teardown=self.absence.absent_since(end, rt.last_reset_bt),
                               settle_s=self.cfg.settle_s, enum_settle_s=self.cfg.enum_settle_s,
                               settle_mult=self.settle_mult, backoff_until_bt=self.backoff_until)

    def _reset_age(self, bt):
        """Seconds since our last USB port reset of this boot (runtime.json), inf if none."""
        rt = self.store.load_runtime()
        return bt - rt.last_reset_bt if rt.last_reset_bt is not None and self.store.same_boot(rt) else math.inf

    def _end_act(self):
        a = self.act
        if a is not None and a.asm is not None and not a.counted:
            a.counted = True
            self.frames_done += a.asm.stats["frames_ok"]

    # -- OPENING .. STARTING (§4.6-§4.9)

    def _open_activate(self):
        node, now, bt = self.node, self.clock.monotonic(), self.clock.boottime()
        prof = self._profile()
        known = prof.strategy if prof else None
        pinned = self.cfg.strategy if self.cfg.strategy != "auto" else None
        if self.pass_ladder is None:
            self.pass_ladder = ladder(known, pinned)
            self.rung = self.pass_ladder[0]
        self.rung_source = "config" if pinned else ("profile" if known == self.rung else "ladder")
        if self.phase is None:
            y = self.cfg.y_phase if self.cfg.y_phase != "auto" else (prof.y_phase if prof else None)
            self.phase = 0 if y == "even" else 1
        s = STRATEGIES[self.rung]
        self._set_state("OPENING", "start")
        self.pm = read_power(self.sys_root, node.sysname)
        self.log.i("usb", "opening", sysname=node.sysname, pm_control=self.pm["control"],
                   runtime_status=self.pm["runtime_status"])
        drv = VftDriver(UsbDevice(self.usb_sys, node.devpath), node, self.clock, self.log,
                        DriverConfig(n_urbs=self.n_urbs, urb_bytes=self.cfg.urb_bytes,
                                     teardown_magic3=self.cfg.teardown_magic3), inline=not self.threaded)
        if self.threaded:
            drv.urbs.on_reaper_start = lambda: self._priority("reaper", self.cfg.cpu_io, self.cfg.nice_io)
        self._opening = drv
        if self._stop_req.is_set():             # a stop that came before this driver: OPENING sees it (vft._ctrl)
            drv.abort.set()
        try:
            ident = drv.open(set_config=s.set_config)
        except ActivationError as e:
            return self._open_failed(e, node, bt)
        except DeviceGone:
            self.node = None
            return self._set_state("NO_DEVICE", "gone")
        except OSError as e:
            self.log.w("usb", "open failed", errno=e.errno, error=str(e))
            return self._fail(F.XU_ERROR, detail="open: %s" % e)
        except Exception as e:                  # a bug: a FAULT with its retry, never a state that never moves
            return self._internal(e)
        finally:
            self._opening = None
        self.driver, self.ident = drv, ident
        if self._stop_req.is_set():
            drv.abort.set()
        lay = ident.layout
        if (lay.format_index, lay.frame_index) != (1, 1):
            return self._unsupported("descriptor format %d frame %d (FrameFace requests format 1 frame 1)" % (
                lay.format_index, lay.frame_index))
        n = xu_len_for(self.rung, self.cfg.xu_len, known, prof.xu_len if prof else None, ident.get_len)
        if n < uvc.REG_CMD_LEN:
            return self._unsupported("XU length %d cannot carry a %d-byte register write" % (n, uvc.REG_CMD_LEN))
        self.tried.add(self.rung)
        level = self._level()
        current = self.cfg.ir_current if self.cfg.ir_current is not None else IR_LEVELS[level]
        self._note(strategy=self.rung, n=n, set_config=s.set_config)
        self._end_act()
        a = self.act = Act(self.rung, n, level, now, self.trial, self.inherit_seen_ok)
        self.inherit_seen_ok = False
        _append(self.activations, {"strategy": self.rung, "n": n, "level": level, "t": now, "trial": self.trial})
        self.counts["activations"] += 1
        self._set_state("ACTIVATING", self.rung)
        self._mark("dirty")
        try:
            drv.activate(s, self.cfg.ir_pwm, current, n)
            self._set_state("NEGOTIATING", "")
            params = drv.negotiate(s, self.cfg.ir_pwm, current, n)
            self._report(drv, ident)
            self._set_state("STARTING", "")
            fb = lay.width * lay.height * 2
            a.pool = FramePool(fb)
            a.asm = FrameAssembler(fb, a.pool, self.clock)
            a.asm.on_event = self._asm_event
            a.reasm = BulkReassembler(a.asm, params.max_payload, lay.max_packet)
            a.params, a.health, a.start_t = params, HealthTracker(self.phase), self.clock.monotonic()
            drv.start(params, a.reasm)
            self.cxu = 0                        # Block A was acknowledged: XU failures count afresh
            self.settle_mult = 1.0              # the doubled settle of row 7 applies once (as Sup does)
        except (XuTimeout, XuError) as e:
            return self._fail(F.XU_TIMEOUT if isinstance(e, XuTimeout) else F.XU_ERROR, detail=str(e))
        except ProbeReject as e:
            return self._fail(F.PROBE_REJECT, detail=str(e))
        except DeviceGone:
            return self._lost("gone during activation")
        except Aborted:
            self._note(abort=True)
            self.log.i("usb", "activation abandoned", reason="stop")
            return self._teardown("stop")
        except (OSError, ValueError, RuntimeError) as e:
            self.log.e("usb", "activation failed", error=repr(e))
            return self._fail(F.UNSUPPORTED, detail=repr(e))
        except Exception as e:                  # a bug: Block C and a FAULT with its retry, never a stuck state
            return self._internal(e)
        if self.threaded:
            a.thread = threading.Thread(target=self._pixel_main, args=(a,), name="pixels", daemon=True)
            a.thread.start()

    def _open_failed(self, e, node, bt):
        f = e.failure
        if f is F.PERMISSION:
            if self.enum_bt is not None and bt - self.enum_bt < PERMISSION_WINDOW_S:
                self.counts["eacces"] += 1      # udev may still be setting the mode (§4.22)
                self.backoff_until = bt + PERMISSION_RETRY_S
                return self._set_state("BACKOFF", "eacces")
            acc = node_access(node.devpath)
            self.log.e("usb", "permission denied", node=node.devpath, mode=acc["mode"], group=acc["group"])
        elif f is F.BUSY:
            own = [(p, c) for p, c in find_owners(node.devpath, self.proc_root) if p != os.getpid()]
            self.owner = ", ".join("%s pid %d" % (c, p) for p, c in own) or None
            self.log.w("usb", "busy", interface=e.info.get("interface", 1), owner=self.owner or "unknown")
        self.counts["open_error"] += 1
        self._fail(f, detail=str(e))

    def _unsupported(self, why):
        self.fault_detail = why
        self.log.e("usb", "unsupported", reason=why)
        self._fail(F.UNSUPPORTED, detail=why)

    def _internal(self, e):
        """An unexpected error in OPENING .. STARTING: recorded in `errors`, then Block C (if a session
        is open) and FAULT(unsupported) with its 60 s retry instead of a state no tick ever leaves."""
        _append(self.errors, e, 100)
        self.log.e("usb", "internal error", error=repr(e))
        return self._fail(F.UNSUPPORTED, detail=repr(e))

    def _report(self, drv, ident):
        """The activation report of §4.7 (S3: Block A ran inside negotiate(), read last_activation)."""
        act = drv.last_activation
        if act is None:
            return
        self.log.event("activation", strategy=act.strategy, ir_level=act.ir_level, get_len=ident.get_len,
                       xu_len=act.xu_len, set_config=ident.set_config, cmds=act.cmds,
                       stream_off_ack=act.stream_off_ack, stream_on_ack=act.stream_on_ack, total_ms=act.total_ms)

    def _asm_event(self, name, kv):
        if name == "lenient_headers":
            self.log.i("stream", "lenient headers on")
        elif name == "stride_learned":
            self.log.i("stream", "stride learned", stride=kv.get("stride"))
            self._queue_profile(payload_stride=kv.get("stride"))
        self.log.event(name, **kv)

    # -- STARTING / STREAMING: watchdogs (§4.13)

    def _watchdogs(self, now):
        drv, a = self.driver, self.act
        if drv.gone or isinstance(drv.urbs.reaper_error, DeviceGone):
            return self._lost("gone")
        if self.threaded:
            r, beat = drv.urbs.reaper, drv.urbs.reaper_beat
            if drv.urbs.reaper_error is not None or (r is not None and not r.is_alive()) or (
                    beat is not None and now - beat > HEARTBEAT_S):
                return self._fail(F.STALL, detail="reaper thread")
            if a.thread is not None and (not a.thread.is_alive() or (a.beat is not None and now - a.beat > HEARTBEAT_S)):
                return self._fail(F.STALL, detail="pixels thread")
        if a.trial_ok and self.trial:
            self.trial = False
            self._save_profile(ir_level=self.level, ir_reason="dark_recovered")
            _append(self.events, "ir_persisted")
            self.log.w("usb", "ir level=%s reason=dark_recovered" % self.level)
            self.log.event("ir_change", level=self.level, reason="dark_recovered")
        if a.request is not None:
            f, kw = a.request
            a.request = None
            if kw.pop("end_trial", False):
                self.trial, self.dark_suppressed_until = False, now + SUPPRESS_S
            return self._fail(f, **kw)
        asm = a.asm
        if self.state == "STARTING":
            if asm.stats["frames_ok"]:
                a.stream_t = asm.last_ok_t if asm.last_ok_t is not None else now
                self.log.i("stream", "first frame", ms=int((a.stream_t - a.start_t) * 1000), fps_pts=asm.fps.fps_pts)
                self._set_state("STREAMING", "first frame")
            elif drv.urbs.first_data_t is None and now - a.start_t >= NO_DATA_S:
                return self._fail(F.NO_DATA)
            elif drv.urbs.first_data_t is not None and \
                    now - a.start_t >= NO_FRAMES_S + (LEARN_GRACE_S if a.reasm.learning else 0.0):
                return self._fail(F.NO_FRAMES)
        if self.state == "STREAMING":
            prof = self._last_profile
            fps = (prof.fps_pts if prof is not None and prof.fps_pts else None) or 60.0
            gap = now - (asm.last_ok_t if asm.last_ok_t is not None else a.stream_t)
            if gap >= max(3.0, 90.0 / fps):
                return self._fail(F.STALL, detail="no frame for %.1f s" % gap)
            if gap >= max(1.0, 30.0 / fps):
                if not a.stall_warned:
                    a.stall_warned = True
                    self.log.w("stream", "stall", seconds=round(gap, 1))
            else:
                a.stall_warned = False
        q = drv.urbs
        if _count(q.epipe_times, now, 10.0) > 5:
            return self._fail(F.EPIPE_STORM)
        if _count(q.xfer_err_times, now, 1.0) > 50:
            self._suspect("xfer_storm")
            return self._fail(F.XFER_STORM)
        if not self.grown and _count(q.starved_times, now, 60.0) > 1:
            self.grown, self.n_urbs = True, min(32, self.n_urbs + 16)
            drv.add_urbs(16)
            _append(self.events, "urbs_grown")
            self.log.event("urbs_grown", urbs=drv.stats["urbs"])
        if a.first_ok_t is not None and now - a.first_ok_t >= HEALTHY_RESET_S:
            self.fails_in_row = self.soft_in_row = 0
            if not a.proven:
                self._end_pass(a, now)
        if not a.saved and a.stream_t is not None and now - a.stream_t >= SAVE_AFTER_S and \
                a.healthy >= SAVE_AFTER_FRAMES:
            a.saved = self.saved_pair = True
            self._save_profile(strategy=a.strategy, xu_len=a.n, source="service", get_len=self.ident.get_len,
                               speed=self.ident.speed, fps_pts=asm.fps.fps_pts)
            self.log.event("profile", strategy=a.strategy, xu_len=a.n, source="service")

    def _end_pass(self, a, now):
        """60 s of healthy streaming end the ladder pass (decision 7): a new pass starts with this proven rung
        first, whatever profile.json says (its known-good pair may not be saved yet, or the write failed: review
        finding A-2), and the first failure of the proven rung that would move the ladder retries it once (row 11
        with the proven seconds) instead of abandoning it for the next rung."""
        a.proven = True
        pinned = self.cfg.strategy if self.cfg.strategy != "auto" else None
        self.tried, self.ladder_restarts, self.cxu, self.settle_mult = set(), 0, 0, 1.0
        self.pass_ladder, self.rung = ladder(a.strategy, pinned), a.strategy
        self.proven = (a.strategy, now - a.first_ok_t)

    # -- failures and their flows (§4.13)

    def _fail(self, f, ir_trial_possible=None, detail=""):
        now, bt = self.clock.monotonic(), self.clock.boottime()
        a, node = self.act, self.node
        _append(self.failures, f)
        self.cxu = self.cxu + 1 if f in (F.XU_TIMEOUT, F.XU_ERROR) else 0
        level = self._level()
        if ir_trial_possible is None:
            ir_trial_possible = self.cfg.ir_level == "auto" and ir_next(level) is not None and not self.trial and \
                now >= self.dark_suppressed_until
        good = now - a.first_ok_t if self.state == "STREAMING" and a is not None and a.first_ok_t is not None else 0.0
        if self.proven is not None and self.proven[0] != self.rung:
            self.proven = None                  # the ladder moved on from the proven rung
        proven_s = self.proven[1] if self.proven is not None else 0.0
        seen_ok = bool(a is not None and a.seen_ok and self.state in ("STARTING", "STREAMING"))
        ctx = FailureContext(self.rung or LADDER[0], set(self.tried), self.cxu, max(good, proven_s), self.soft_in_row,
                             node is not None and not node.behind_hub, self.cfg.allow_port_reset, self._reset_age(bt),
                             self.reset_allowance, self.ladder_restarts, seen_ok=seen_ok,
                             brownout_pending=self.brown.pending(now), ir_trial_possible=ir_trial_possible,
                             ladder=tuple(self.pass_ladder or LADDER))
        d = on_failure(f, ctx)
        if d.row == 11:
            self.proven = None                  # a proven rung is retried once; failing again moves the ladder
        _append(self.decisions, d)
        dark_start = f is F.DARK and d.row not in (4, 5)
        self.last_failure = "dark_start" if dark_start else f.value
        self.next_rung = d.strategy or None
        self._log_failure(f, d, a, level, dark_start, detail)
        if d.action == "suppress":
            self.dark_suppressed_until = now + SUPPRESS_S
            return
        self.settle_mult = d.settle_mult        # 2.0 only for row 7, and only for its retry (once)
        if d.action == "wait_device":
            return self._lost(f.value)
        self.soft_in_row = self.soft_in_row + 1 if d.row == 10 else (self.soft_in_row if f in SOFT else 0)
        if d.row == 10:
            self.counts["soft_reopens"] += 1
        self._teardown(f.value)
        if d.action == "fault":
            return self._enter_fault(d.fault, d.delay_s, hint=d.hint, detail=detail or f.value)
        if d.action == "reset_port":
            if d.row == 6:
                self.reset_allowance -= 1
            else:
                self.ladder_restarts, self.tried, self.rung = 1, set(), d.strategy     # rung 0 of this pass
            return self._reset(f.value)
        if d.action == "next_strategy":
            self.rung = d.strategy
        elif d.action == "ir_trial":
            self.inherit_seen_ok = seen_ok
            self.level, self.trial, self.rung = ir_next(level), True, d.strategy
        self.fails_in_row += 1
        self.backoff_until = bt + backoff_s(self.fails_in_row)
        self._set_state("BACKOFF", f.value)

    def _log_failure(self, f, d, a, level, dark_start, detail):
        if f is F.TEST_BARS:
            self.log.w("stream", "test bars", count=TESTBAR_RUN, strategy=self.rung, next=d.strategy or d.action)
        elif dark_start and d.action != "ir_trial":
            self.log.w("stream", "dark from start", strategy=self.rung, xu_len=a.n if a else None,
                       next=d.strategy or d.action)
        elif f is F.DARK:
            self.log.w("stream", "dark", seconds=DARK_S, ir=level, trial=ir_next(level) if d.action == "ir_trial"
                       else "no")
        self.log.w("usb", "failure %s" % f.value, action=d.action, row=d.row, next=d.strategy or "-")
        self.log.event("failure", kind=f.value, action=d.action, row=d.row, strategy=self.rung,
                       next=d.strategy or None, settle_mult=d.settle_mult, detail=detail or None)

    def _teardown(self, why=""):
        """Block C (§4.12) of the current session; runtime.json follows the path it took. A stop that comes during it
        reaches its driver (`_closing`, request_stop: the §4.6 stop rules of Block C and step 11)."""
        drv, a = self.driver, self.act
        self._closing, self.driver = drv, None
        if drv is None:
            return
        self._set_publish(False, why or "teardown")
        self._set_state("TEARDOWN", why)
        if a is not None:
            a.stop.set()
        try:
            drv.teardown(clean=True)
        except Exception as e:                  # Block C must never take the service down
            self.log.e("usb", "teardown failed", error=repr(e))
        self._closing = None
        if a is not None and a.thread is not None and a.thread is not threading.current_thread():
            a.thread.join(1.0)
        self._end_act()
        self._retire(drv)
        self._mark("lost" if drv.teardown_path == "lost" else "clean_teardown")

    def _retire(self, drv):
        """Keep the last 32 torn-down drivers (stats, teardown_path); only the newest keeps its stream
        sink, so older ones hold no frame buffers (3 x 320000 B each)."""
        self.drivers.append(drv)
        del self.drivers[:-32]
        for old in self.drivers[:-1]:
            old.sink = old.urbs.sink = None

    def _lost(self, why="gone"):
        """The LOST path: no control request, discard, close. Soon after an activation it is brown-out
        suspect B1 and a consecutive failure: the backoff applies before the next activation."""
        drv, a = self.driver, self.act
        self.driver = None
        self._set_publish(False, "no_device")
        self._set_state("LOST", why)
        if drv is not None:
            if a is not None:
                a.stop.set()
            try:
                drv.teardown(clean=False)
            except Exception as e:
                self.log.e("usb", "teardown failed", error=repr(e))
            if a is not None and a.thread is not None and a.thread is not threading.current_thread():
                a.thread.join(1.0)
            self._end_act()
            self._retire(drv)
        self._mark("lost")
        self.present = False
        self.counts["unplugs"] += 1
        self.log.event("lost", why=why)
        self.settle_mult = 1.0                  # row 1 (wait_device): the plain settle after the loss
        if a is not None and self.clock.monotonic() - a.t < B1_WINDOW_S:
            self._suspect("lost_after_activation")
            self.fails_in_row += 1              # a tracker dropping off after every start backs off (1 .. 60 s)
            self.backoff_until = self.clock.boottime() + backoff_s(self.fails_in_row)
        self.node = None
        self._set_state("NO_DEVICE", "lost")

    def _reset(self, reason):
        """RESETTING (§4.12): USBDEVFS_RESET on a fresh fd, never within RESET_GAP_S of the last reset of
        this boot (whoever asks; then only the wait and the rescan) and never once a stop is pending (it
        would delay the stop, §4.6), then 2 s and a rescan."""
        node = self.node
        self._set_state("RESETTING", reason)
        age = self._reset_age(self.clock.boottime())
        if self._stop_req.is_set():
            self.log.w("usb", "reset skipped", reason=reason, why="stopping")
        elif age < RESET_GAP_S:
            self.log.w("usb", "reset skipped", reason=reason, last_reset_s=round(age, 1))
        else:
            self.log.w("usb", "reset port", sysname=node.sysname, reason=reason)
            try:
                VftDriver(UsbDevice(self.usb_sys, node.devpath), node, self.clock, self.log, DriverConfig(),
                          inline=True).reset_port()
            except (OSError, RuntimeError) as e:
                self.log.w("usb", "reset failed", error=str(e))
            self._mark("reset")
            self.counts["resets"] += 1
            self.log.event("reset", reason=reason)
        self.reset_until = self.clock.monotonic() + RESET_WAIT_S
        self.node = None

    def _suspect(self, reason):
        now = self.clock.monotonic()
        lowered = self.brown.suspect(now, reason)
        self.log.w("usb", "brownout suspect", reason=reason, count=self.brown.total)
        self.log.event("brownout_suspect", reason=reason, count=self.brown.total)
        if not lowered:
            return
        nxt = ir_next(self._level())
        if nxt is None:                         # §4.13 "At L3: stay, hint": the brownout display for 60 s
            self.log.w("usb", "brownout at the lowest IR level", level=self._level())
            self.overlay, self.overlay_until = "brownout", now + 60.0
            return
        self.level = None
        self._save_profile(ir_level=nxt, ir_reason="brownout")
        _append(self.events, "ir_change")
        self.log.w("usb", "ir level=%s reason=brownout" % nxt)
        self.log.event("ir_change", level=nxt, reason="brownout")
        self.overlay, self.overlay_until = "brownout", now + 60.0
