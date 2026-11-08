# SPDX-License-Identifier: MIT
"""`Sup`, the compact supervisor the driver matrix runs on, and `RigCase`, the base of the rig tests (spec §11.3, task T2).
They lived in test_ff_replay until it reached the 600-line limit; test_ff_driver_matrix, test_ff_replay, test_ff_service
(SvcCase, hence test_ff_service_matrix and test_ff_sup_parity) and test_ff_usbfs import them from here.

Sup is §4.6/§4.11/§4.13 on top of the real driver, fake kernel and the pure policy (on_failure, ladder, xu_len_for,
settle_deadline, backoff, brown-out and IR ladder), i.e. the service's state machine (supervisor.UsbSupervisor) without
HTTP, clients, worn or pixels. It keeps the service's recovery rules: the settle from the end of the last session (a clean
teardown or a LOST), every decision's settle_mult, the backoff after a LOST soon after an activation (B1), the end of the
ladder pass after 60 s of healthy streaming with its proven rung, the 600 s gap between USB resets and the brown-out
display (test_ff_sup_parity compares the two). It always wants to stream, steps the clock in 10 ms pumps while streaming
and ticks every 100 ms."""
import ffutil  # first: installs the port guard

import collections
import math
import shutil
import tempfile
import unittest

ffutil.setup_path()
from frameface.assembler import BulkReassembler, FrameAssembler, FramePool  # noqa: E402
from frameface.config import Config, temp_paths  # noqa: E402
from frameface.descriptors import sha1_hex  # noqa: E402
from frameface.policy import (IR_LEVELS, LADDER, RESET_GAP_S, STRATEGIES, AbsenceTracker, BrownoutTracker,  # noqa: E402
                              Failure as F, FailureContext, backoff_s, display, ir_next, ladder, on_failure,
                              settle_deadline, xu_len_for)
from frameface.quality import HealthTracker, LayoutSchedule, analyze_layout, choose_phase  # noqa: E402
from frameface.sim import VftModel, check_trace, make_rig  # noqa: E402
from frameface.state import Profile, Store  # noqa: E402
from frameface.sysfs import find_owners  # noqa: E402
from frameface.usbfs import DeviceGone, UsbDevice  # noqa: E402
from frameface.vft import Aborted, ActivationError, DriverConfig, ProbeReject, VftDriver, XuError, XuTimeout  # noqa: E402

__all__ = ["ListLog", "Sup", "RigCase", "SOFT"]
SOFT = (F.STALL, F.FROZEN, F.EPIPE_STORM, F.XFER_STORM)


class ListLog:
    def __init__(self):
        self.lines = []

    def _add(self, level, component, event, kv):
        self.lines.append("%s %s: %s %s" % (level, component, event, " ".join("%s=%s" % i for i in kv.items())))

    def d(self, c, e, **kv):
        self._add("D", c, e, kv)

    def i(self, c, e, **kv):
        self._add("I", c, e, kv)

    def w(self, c, e, **kv):
        self._add("W", c, e, kv)

    def e(self, c, e, **kv):
        self._add("E", c, e, kv)

    def has(self, text):
        return any(text in line for line in self.lines)


def _count(times, now, window):
    return sum(1 for t in times if now - t <= window)


def _plane_ok(buf, phase):
    s = buf[phase::78]
    mean = sum(s) / len(s)
    return not (mean < 3.0 and max(s) < 16) and not (len(set(s)) < 16 and mean > 8)


class Sup:
    def __init__(self, rig, store, cfg, lag=0.0, sys=None):
        self.rig, self.k, self.clock, self.store, self.cfg, self.lag = rig, rig.kernel, rig.clock, store, cfg, lag
        self.sys = sys if sys is not None else rig.kernel      # TraceSys / ReplayKernel in test_ff_replay
        self.log, self.states, self.state = ListLog(), [], None
        self.set_state("NO_DEVICE")
        rt = store.load_runtime()
        self.dirty_start = store.same_boot(rt) and rt.dirty
        known = store.load_profile(sha1_hex(rig.vft.descriptors))
        self.preloaded = known.strategy if known else None      # the known-good rung this start found
        self.reset_allowance = 1 if self.dirty_start else 0
        self.start_bt = self.clock.boottime()
        self.absence, self.brown = AbsenceTracker(), BrownoutTracker()
        self.tried, self.ladder_restarts, self.cxu, self.soft_in_row, self.fails_in_row = set(), 0, 0, 0, 0
        self.settle_mult, self.backoff_until, self.rung, self.pass_ladder, self.proven = 1.0, -math.inf, None, None, None
        self.level, self.trial, self.dark_suppressed_until, self.overlay = None, False, -math.inf, ""
        self.brownout_until = -math.inf                         # the brown-out display (60 s, as the service's overlay)
        self.seen_ok = self.inherit_seen_ok = self.saved_pair = self.grown = self.stopped = self.paused = False
        self.decisions, self.failures, self.activations, self.events, self.drivers = [], [], [], [], []
        self.counts = collections.Counter()
        self.fault = self.node = self.driver = self.enum_bt = self.owner = None
        self.n_urbs, self.phase, self.layouts, self.hiccups, self.frame_sizes = cfg.n_urbs, 1, [], [], []
        now = self.clock.monotonic()
        self.next_tick = self.next_scan = now
        self.last_d = self.clock.boottime() - now

    # -- loop

    def set_state(self, s):
        if s != self.state:
            self.state = s
            self.states.append((s, self.clock.monotonic()))

    def run(self, seconds, until=None):
        end = self.clock.monotonic() + seconds
        while self.clock.monotonic() < end and self.state not in ("FAULT", "STOPPED"):
            if until is not None and until(self):
                return True
            self.step()
        return bool(until(self)) if until is not None else None

    def step(self):
        if self.driver is not None and self.state in ("STARTING", "STREAMING"):
            try:
                self.driver.pump(0.01)
                if self.lag:
                    self.clock.sleep(self.lag)
                while self.hiccups and self.clock.monotonic() >= self.hiccups[0][0]:
                    self.clock.sleep(self.hiccups.pop(0)[1])
                self.frames()
            except DeviceGone:
                self.lost()
                return
        else:
            self.clock.sleep(0.05)
        if self.clock.monotonic() >= self.next_tick:
            self.next_tick = self.clock.monotonic() + 0.1
            self.tick()

    def tick(self):
        now, bt = self.clock.monotonic(), self.clock.boottime()
        if bt - now - self.last_d > 2.0:
            self.last_d = bt - now
            self.events.append("suspend")
            if self.driver is not None:
                self.teardown()
                self.set_state("SETTLING")
            return
        self.last_d = bt - now
        if now >= self.next_scan:
            self.next_scan = now + 1.0
            self.scan(bt)
        if self.state == "BACKOFF" and bt >= self.backoff_until:
            self.set_state("SETTLING")
        if self.state == "SETTLING" and self.node is not None and bt >= self.deadline(bt):
            self.open_activate()
        elif self.state in ("STARTING", "STREAMING") and self.driver is not None:
            self.watchdogs(now)

    def scan(self, bt):
        node = self.rig.node()
        self.absence.scan(bt, node is not None)
        if self.paused:
            return
        if node is None:
            if self.state in ("SETTLING", "BACKOFF"):
                self.set_state("NO_DEVICE")
            if self.driver is None:
                self.node = None
        elif self.state == "NO_DEVICE":
            self.node, self.enum_bt = node, bt
            self.set_state("SETTLING")
        elif self.driver is None:
            self.node = node

    def deadline(self, bt):
        """settle_deadline() from the end of the last session, a clean teardown or a LOST (UsbSupervisor._deadline)."""
        rt = self.store.load_runtime()
        end = max((t for t in (rt.last_teardown_bt, rt.last_loss_bt) if t is not None), default=None)
        return settle_deadline(bt, same_boot=self.store.same_boot(rt), enum_bt=self.enum_bt, start_bt=self.start_bt,
                               dirty_start=self.dirty_start, last_teardown_bt=end, last_reset_bt=rt.last_reset_bt,
                               absent_since_teardown=self.absence.absent_since(end, rt.last_reset_bt),
                               settle_s=self.cfg.settle_s, enum_settle_s=self.cfg.enum_settle_s,
                               settle_mult=self.settle_mult, backoff_until_bt=self.backoff_until)

    def reset_age(self, bt):
        rt = self.store.load_runtime()
        return bt - rt.last_reset_bt if rt.last_reset_bt is not None and self.store.same_boot(rt) else math.inf

    # -- profile and IR level

    def profile(self):
        return self.store.load_profile(sha1_hex(self.node.descriptors)) if self.node else None

    def save(self, **kv):
        p = self.profile() or Profile(desc_sha1=sha1_hex(self.node.descriptors))
        for k, v in kv.items():
            setattr(p, k, v)
        try:
            self.store.save_profile(p)
        except OSError as e:                    # a full or read-only disk: logged, the state machine goes on
            self.log.w("state", "profile write failed", error=str(e))

    def current_level(self):
        if self.cfg.ir_level != "auto":
            return self.cfg.ir_level
        p = self.profile()
        return self.level or (p.ir_level if p else "L0")

    # -- OPENING .. STARTING

    def open_activate(self):
        node, now = self.node, self.clock.monotonic()
        prof = self.profile()
        known = prof.strategy if prof else None
        if self.pass_ladder is None:
            self.pass_ladder = ladder(known, self.cfg.strategy if self.cfg.strategy != "auto" else None)
            self.rung = self.pass_ladder[0]
        s = STRATEGIES[self.rung]
        self.set_state("OPENING")
        drv = VftDriver(UsbDevice(self.sys, node.devpath), node, self.clock, self.log,
                        DriverConfig(n_urbs=self.n_urbs, urb_bytes=self.cfg.urb_bytes,
                                     teardown_magic3=self.cfg.teardown_magic3), inline=True)
        try:
            ident = drv.open(set_config=s.set_config)
        except ActivationError as e:
            if e.failure is F.PERMISSION and self.clock.boottime() - self.enum_bt < 5.0:
                self.counts["eacces"] += 1
                self.backoff_until = self.clock.boottime() + 0.5
                self.set_state("BACKOFF")
                return
            if e.failure is F.BUSY:
                self.owner = ["%s pid %d" % (c, p) for p, c in find_owners(node.devpath, self.rig.proc_root)]
            self.counts["open_error"] += 1
            self.fail(e.failure)
            return
        except DeviceGone:
            self.node = None
            self.set_state("NO_DEVICE")
            return
        self.driver, self.ident = drv, ident
        self.tried.add(self.rung)
        n = xu_len_for(self.rung, self.cfg.xu_len, known, prof.xu_len if prof else None, ident.get_len)
        level = self.current_level()
        current = self.cfg.ir_current if self.cfg.ir_current is not None else IR_LEVELS[level]
        self.k.note(strategy=self.rung, n=n, set_config=s.set_config)
        self.activations.append({"strategy": self.rung, "n": n, "level": level, "t": now, "trial": self.trial})
        self.set_state("ACTIVATING")
        self.store.mark_dirty()
        self.seen_ok, self.inherit_seen_ok, self.act_t, self.n = self.inherit_seen_ok, False, now, n
        try:
            drv.activate(s, self.cfg.ir_pwm, current, n)
            self.set_state("NEGOTIATING")
            params = drv.negotiate(s, self.cfg.ir_pwm, current, n)
            self.set_state("STARTING")
            fb = ident.layout.width * ident.layout.height * 2
            self.pool = FramePool(fb)
            self.asm = FrameAssembler(fb, self.pool, self.clock)
            self.reasm = BulkReassembler(self.asm, params.max_payload, ident.layout.max_packet)
            drv.start(params, self.reasm)
        except (XuTimeout, XuError) as e:
            self.fail(F.XU_TIMEOUT if isinstance(e, XuTimeout) else F.XU_ERROR)
            return
        except ProbeReject:
            self.fail(F.PROBE_REJECT)
            return
        except DeviceGone:
            self.lost()
            return
        except Aborted:
            self.k.note(abort=True)
            self.teardown()
            self.set_state("STOPPED")
            return
        self.start_t = self.last_frame_t = self.clock.monotonic()
        self.settle_mult, self.cxu, self.first_ok_t, self.healthy, self.testbar_run = 1.0, 0, None, 0, 0   # cxu: Block A acked
        self.dark_since, self.dark_n, self.nondark_since, self.stream_t, self.act_proven = None, 0, None, None, False
        self.health, self.sched = HealthTracker(self.phase), LayoutSchedule()

    # -- frames, watchdogs

    def frames(self):
        got = self.pool.take(0)
        if not got:
            return
        buf, info = got
        now = info.t_mono
        self.frame_sizes.append(info.size)
        if self.state == "STARTING":
            self.set_state("STREAMING")
            self.stream_t = now
        self.last_frame_t = now
        kind = self.health.classify(buf).kind
        if kind == "testbar" and self.cfg.y_phase == "auto" and _plane_ok(buf, 1 - self.phase):
            kind = "plane"                  # the chosen plane is flat, the other has content
        self.counts[kind] += 1
        self.testbar_run = self.testbar_run + 1 if kind == "testbar" else 0
        if kind in ("ok", "plane") and self.sched.ok_frame(now):
            self.plane(buf)
        if kind == "ok":
            self.seen_ok, self.healthy = True, self.healthy + 1
            self.first_ok_t = self.first_ok_t or now
        if kind == "dark":
            if self.dark_since is None:
                self.dark_since, self.dark_n = now, 0
            self.dark_n, self.nondark_since = self.dark_n + 1, None
        else:
            self.dark_since, self.dark_n = None, 0
            self.nondark_since = self.nondark_since or now
        if self.trial and self.nondark_since is not None and now - self.nondark_since >= 2.0:
            self.trial = False
            self.save(ir_level=self.level, ir_reason="dark_recovered")
            self.events.append("ir_persisted")
        if self.dark_since is not None and self.dark_n >= 30 and now - self.dark_since >= 2.0 and \
                now >= self.dark_suppressed_until:
            if self.trial:
                self.trial, self.dark_suppressed_until = False, now + 600
                return self.fail(F.DARK, ir_trial_possible=False)
            return self.fail(F.DARK)
        if self.testbar_run >= 30:
            return self.fail(F.TEST_BARS)
        if self.health.consecutive["frozen"] >= 60:
            return self.fail(F.FROZEN)
        if not self.saved_pair and now - self.stream_t >= 10 and self.healthy >= 300:
            self.saved_pair = True
            self.save(strategy=self.rung, xu_len=self.n, source="service", get_len=self.ident.get_len,
                      speed=self.ident.speed, fps_pts=self.asm.fps.fps_pts)

    def plane(self, buf):
        lay = analyze_layout(buf)
        self.layouts.append(lay)
        phase, reason = choose_phase(self.layouts[-5:], self.cfg.y_phase)
        if lay.h2_hint and "h2_hint" not in self.events:
            self.events.append("h2_hint")
        if phase != self.phase:
            self.phase, self.health = phase, HealthTracker(phase)
            self.log.w("pixels", "odd plane flat; using even bytes (not Baballonia parity)")
            self.save(y_phase="even" if phase == 0 else "odd",
                      plane={"verdict": lay.verdict, "h2_hint": lay.h2_hint, "reason": reason})

    def watchdogs(self, now):
        drv = self.driver
        if self.state == "STARTING":
            if drv.urbs.first_data_t is None and now - self.start_t >= 2.0:
                return self.fail(F.NO_DATA)
            if drv.urbs.first_data_t is not None and now - self.start_t >= 3.0 + (2.0 if self.reasm.learning else 0.0):
                return self.fail(F.NO_FRAMES)
        elif now - self.last_frame_t >= max(3.0, 90.0 / ((self.profile() or Profile()).fps_pts or 60)):
            return self.fail(F.STALL)
        if _count(drv.urbs.epipe_times, now, 10.0) > 5:
            return self.fail(F.EPIPE_STORM)
        if _count(drv.urbs.xfer_err_times, now, 1.0) > 50:
            self.suspect("xfer_storm")
            return self.fail(F.XFER_STORM)
        if not self.grown and _count(drv.urbs.starved_times, now, 60.0) > 1:
            self.grown, self.n_urbs = True, self.n_urbs + 16
            drv.add_urbs(16)
            self.events.append("urbs_grown")
        if self.first_ok_t is not None and now - self.first_ok_t >= 60:
            self.fails_in_row = self.soft_in_row = 0
            if not self.act_proven:             # once per activation: the ladder pass ends, the rung is proven and
                self.act_proven = True          # first in the next pass, whatever profile.json says (A-2)
                self.tried, self.ladder_restarts, self.cxu, self.settle_mult = set(), 0, 0, 1.0
                self.pass_ladder = ladder(self.rung, self.cfg.strategy if self.cfg.strategy != "auto" else None)
                self.proven = (self.rung, now - self.first_ok_t)

    # -- failures and recovery

    def fail(self, f, ir_trial_possible=None):
        now, bt = self.clock.monotonic(), self.clock.boottime()
        self.failures.append(f)
        self.cxu = self.cxu + 1 if f in (F.XU_TIMEOUT, F.XU_ERROR) else 0
        level = self.current_level()
        if ir_trial_possible is None:
            ir_trial_possible = self.cfg.ir_level == "auto" and ir_next(level) is not None and not self.trial and \
                now >= self.dark_suppressed_until
        good = now - self.first_ok_t if self.state == "STREAMING" and self.first_ok_t is not None else 0.0
        if self.proven is not None and self.proven[0] != self.rung:
            self.proven = None                  # the ladder moved on from the proven rung
        ctx = FailureContext(self.rung, set(self.tried), self.cxu, max(good, self.proven[1] if self.proven else 0.0),
                             self.soft_in_row, not self.node.behind_hub, self.cfg.allow_port_reset, self.reset_age(bt),
                             self.reset_allowance, self.ladder_restarts, seen_ok=self.seen_ok,
                             brownout_pending=self.brown.pending(now), ir_trial_possible=ir_trial_possible,
                             ladder=tuple(self.pass_ladder or LADDER))
        d = on_failure(f, ctx)
        self.decisions.append(d)
        if d.row == 11:
            self.proven = None                  # a proven rung is retried once
        if d.action == "suppress":
            self.dark_suppressed_until, self.overlay = now + 600, "dark"
            return
        self.settle_mult = d.settle_mult        # every decision's (2.0 only for row 7's retry)
        if d.action == "wait_device":
            return self.lost()
        self.soft_in_row = self.soft_in_row + 1 if d.row == 10 else (self.soft_in_row if f in SOFT else 0)
        self.teardown()
        if d.action == "fault":
            self.fault = d
            self.set_state("FAULT")
            return
        if d.action == "reset_port":
            if d.row == 6:
                self.reset_allowance -= 1
            else:
                self.ladder_restarts, self.tried, self.rung = 1, set(), d.strategy
            return self.reset()
        if d.action == "next_strategy":
            self.rung = d.strategy
        elif d.action == "ir_trial":
            self.inherit_seen_ok, self.level, self.trial, self.rung = self.seen_ok, ir_next(level), True, d.strategy
        self.fails_in_row += 1
        self.backoff_until = bt + backoff_s(self.fails_in_row)
        self.set_state("BACKOFF")

    def suspect(self, reason):
        now = self.clock.monotonic()
        if self.brown.suspect(now, reason):
            nxt = ir_next(self.current_level())
            self.brownout_until = now + 60.0    # lowered, or at L3 (§4.13 "stay, hint"): the brown-out display
            if nxt is not None:
                self.level = None
                self.save(ir_level=nxt, ir_reason="brownout")
                self.events.append("ir_change")

    def teardown(self):
        drv, self.driver = self.driver, None
        if drv is None:
            return
        self.set_state("TEARDOWN")
        drv.teardown(clean=True)
        self.drivers.append(drv)
        if drv.teardown_path == "lost":
            self.store.mark_lost()
        else:
            self.store.mark_clean_teardown()

    def lost(self):
        drv, self.driver = self.driver, None
        self.set_state("LOST")
        if drv is not None:
            drv.teardown(clean=False)
            self.drivers.append(drv)
        self.store.mark_lost()
        self.counts["unplugs"] += 1
        self.settle_mult = 1.0                  # row 1: the plain settle after the loss
        if self.clock.monotonic() - self.act_t < 30:
            self.suspect("lost_after_activation")
            self.fails_in_row += 1              # B1 is a consecutive failure too: the backoff applies
            self.backoff_until = self.clock.boottime() + backoff_s(self.fails_in_row)
        self.node = None
        self.set_state("NO_DEVICE")

    def reset(self):
        self.set_state("RESETTING")
        age = self.reset_age(self.clock.boottime())
        if age < RESET_GAP_S:                   # never within 600 s of the last reset of this boot
            self.log.w("usb", "reset skipped", last_reset_s=round(age, 1))
            self.counts["resets_skipped"] += 1
        else:
            VftDriver(UsbDevice(self.sys, self.node.devpath), self.node, self.clock, self.log, DriverConfig(),
                      inline=True).reset_port()
            self.store.mark_reset()
            self.counts["resets"] += 1
        self.clock.sleep(2.0)
        self.node = None
        self.set_state("NO_DEVICE")

    def stop(self):
        self.teardown()
        self.set_state("STOPPED")

    def idle(self):
        self.teardown()
        self.paused = True
        self.set_state("IDLE")

    def resume(self):
        self.paused, self.node = False, None
        self.set_state("NO_DEVICE")

    def display(self):
        brownout = self.clock.monotonic() < self.brownout_until
        ctx = {"fault": self.fault.fault if self.fault else None, "overlay": "brownout" if brownout else self.overlay,
               "owner": ", ".join(self.owner or []) or None, "ir_level": self.current_level()}
        return display(self.state, ctx)


class RigCase(unittest.TestCase):
    """Rigs, stores and Sup instances in temp directories, removed after each test."""

    def tmpdir(self):
        d = tempfile.mkdtemp(prefix="fft2-")
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def store(self, rig):
        return Store(temp_paths(self.tmpdir()), rig.clock, boot_id="sim-boot")

    def make(self, cfg=None, profile=None, store=None, lag=0.0, sys=None, **knobs):
        rig = make_rig(VftModel(**knobs))
        self.addCleanup(rig.close)
        store = store if store is not None else self.store(rig)
        if profile:
            store.save_profile(Profile(desc_sha1=sha1_hex(rig.vft.descriptors), **profile))
        return rig, Sup(rig, store, cfg if cfg is not None else Config(token=""), lag=lag, sys=sys)

    def check(self, sup, service=True):
        """kernel.violations == [], the 13 trace invariants and the profile: a pair saved by this run is the
        last activation's (strategy, n) from the service; otherwise the stored rung is the one found at start."""
        self.assertEqual(sup.k.violations, [])
        self.assertEqual(check_trace(sup.k.ops, service), [])
        p = sup.store.load_profile(sha1_hex(sup.rig.vft.descriptors))
        if sup.saved_pair:
            last = sup.activations[-1]
            self.assertEqual((p.strategy, p.xu_len, p.source), (last["strategy"], last["n"], "service"))
        else:
            self.assertEqual(p.strategy if p else None, sup.preloaded)
