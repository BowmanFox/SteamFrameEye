# SPDX-License-Identifier: MIT
"""The pixel stage of the service (spec §4.11, §4.15, §4.16), a mixin of service.Service.

Every complete frame runs the health gate on the chosen byte plane: dark -> test bars -> frozen ->
ok. A frame that shows test bars only in the chosen plane while the other plane has content is a
plane question ("plane"), never a ladder failure: it feeds the plane analysis (§4.15.6), which may
switch to the even bytes with a WARN. The stage never decides recovery itself: 30 test-bar frames,
2 s and 30 frames of dark (worn != False; during an IR trial: the trial failed), 60 frozen frames
become `Act.request` for the supervisor; 2 s of non-dark frames during a trial set `Act.trial_ok`.
Ok frames are published while `publish` is on: preprocess (SWAR capped at 30 fps) -> JPEG ->
validate -> the Latest mailbox; unhealthy frames never are.
"""
import math
import threading
import time

from .jpeg import JpegError, validate_jpeg
from .mailbox import Jpeg
from .policy import Failure as F
from .quality import HealthTracker, LayoutSchedule, analyze_layout, choose_phase

__all__ = ["Act", "PixelStage", "plane_ok"]

TESTBAR_RUN, FROZEN_RUN = 30, 60
DARK_S, DARK_FRAMES, RECOVER_S = 2.0, 30, 2.0


def plane_ok(buf, phase) -> bool:
    """The §4.11 sample of the other byte plane is neither dark nor test bars: a frame that is test
    bars only in the chosen plane is a plane question (h1_chroma80: constant odd bytes), not a
    reason to climb the ladder."""
    s = buf[phase::78]
    mean = sum(s) / len(s)
    return not (mean < 3.0 and max(s) < 16) and not (len(set(s)) < 16 and mean > 8)


def _ewma(old, new, alpha=0.1):
    return new if old is None else old + alpha * (new - old)


class Act:
    """One activation: the stream objects and the health-gate state of its frames."""

    def __init__(self, strategy, n, level, t, trial, seen_ok):
        self.strategy, self.n, self.level, self.t, self.trial = strategy, n, level, t, trial
        self.seen_ok = seen_ok                  # an ok frame here (or in the activation an IR trial replaced)
        self.pool = self.asm = self.reasm = self.params = self.health = None
        self.start_t = self.stream_t = self.first_ok_t = None
        self.healthy = self.frames = self.testbar_run = self.dark_n = 0
        self.dark_since = self.nondark_since = None
        self.sched = LayoutSchedule()
        self.request = None                     # (Failure, kwargs) for the supervisor
        self.trial_ok = self.saved = self.stall_warned = self.counted = False
        self.proven = False                     # 60 s healthy: ended the ladder pass (supervisor._end_pass)
        self.thread = self.beat = self.error = None
        self.stop = threading.Event()
        self.pre_due = -math.inf                # SWAR rate cap schedule


class PixelStage:
    """_process() per complete frame: the pixel thread runs _pixel_main(), inline mode calls it per step."""

    def _pixel_main(self, a):
        self._priority("pixels", self.cfg.cpu_work, 0)
        try:
            while not a.stop.is_set():
                a.beat = self.clock.monotonic()
                got = a.pool.take(0.1)
                if got is not None:
                    self._process(a, *got)
        except Exception as e:
            a.error = e
            self.log.e("pixels", "thread failed", error=repr(e))

    def _process(self, a, buf, info):
        """The health gate of one complete frame; ok frames are published while `publish` is on."""
        now = info.t_mono
        a.frames += 1
        if a.request is not None:
            return
        kind = a.health.classify(buf).kind
        if kind == "testbar" and self.cfg.y_phase == "auto" and plane_ok(buf, 1 - a.health.phase):
            kind = "plane"
        self.counts[kind] += 1
        a.testbar_run = a.testbar_run + 1 if kind == "testbar" else 0
        if kind in ("ok", "plane") and a.sched.ok_frame(now):
            self._plane(a, buf)
        if kind == "ok":
            a.seen_ok = True
            a.healthy += 1
            if a.first_ok_t is None:
                a.first_ok_t = now
                self.last_failure = self.next_rung = None
        if kind == "dark":
            if a.dark_since is None:
                a.dark_since, a.dark_n = now, 0
            a.dark_n += 1
            a.nondark_since = None
        else:
            a.dark_since, a.dark_n = None, 0
            if a.nondark_since is None:
                a.nondark_since = now
        if self.trial and a.nondark_since is not None and now - a.nondark_since >= RECOVER_S:
            a.trial_ok = True
        if a.dark_since is not None and a.dark_n >= DARK_FRAMES and now - a.dark_since >= DARK_S and \
                now >= self.dark_suppressed_until and not (self.trial and a.trial_ok):    # success pending
            if self.trial:
                a.request = (F.DARK, {"ir_trial_possible": False, "end_trial": True})
                return
            if self.worn is not False:
                a.request = (F.DARK, {})
                return
        if a.testbar_run >= TESTBAR_RUN:
            a.request = (F.TEST_BARS, {})
            return
        if a.health.consecutive["frozen"] >= FROZEN_RUN:
            a.request = (F.FROZEN, {})
            return
        if kind == "ok" and self.publish:
            self._publish(a, buf, info)

    def _plane(self, a, buf):
        lay = analyze_layout(buf)
        self.layouts.append(lay)
        del self.layouts[:-16]
        self.plane_info = {"verdict": lay.verdict, "mad": round(lay.mad_even_odd, 2), "std_odd": round(lay.std_odd, 2),
                           "std_even": round(lay.std_even, 2), "h2_hint": lay.h2_hint}
        if lay.h2_hint and not self.h2_hinted:
            self.h2_hinted = True
            self.events.append("h2_hint")             # once per process
            self.log.w("pixels", "two images side by side?", verdict=lay.verdict, mad=round(lay.mad_even_odd, 1))
        window = self.layouts[-5:]
        phase, reason = choose_phase(window, self.cfg.y_phase)
        if phase != self.phase and (self.cfg.y_phase != "auto" or len(window) >= 3):
            self.phase = phase
            a.health = HealthTracker(phase)
            if phase == 0:
                self.log.w("pixels", "odd plane flat; using even bytes (not Baballonia parity)")
            else:
                self.log.i("pixels", "using odd bytes", reason=reason)
            self._queue_profile(y_phase="even" if phase == 0 else "odd",
                                plane={"verdict": lay.verdict, "h2_hint": lay.h2_hint, "reason": reason})

    def _publish(self, a, buf, info):
        pre, now = self.pre, info.t_mono
        if pre.max_fps:                         # SWAR: at most 30 fps on average, one frame of jitter credit
            if now < a.pre_due - 0.001:
                return
            a.pre_due = max(a.pre_due + 1.0 / pre.max_fps, now - 1.0 / pre.max_fps)
        t0 = time.perf_counter()
        try:
            px = pre(buf, self.phase, self.col0)
            t1 = time.perf_counter()
            data = self.encoder.encode_gray(px)
        except (JpegError, ValueError) as e:
            self.jpeg_bad += 1
            self.log.w("pixels", "frame dropped", error=str(e))
            return
        t2 = time.perf_counter()
        if not validate_jpeg(data):
            self.jpeg_bad += 1
            return
        self.pre_ms = _ewma(self.pre_ms, (t1 - t0) * 1000.0)
        self.jpeg_ms = _ewma(self.jpeg_ms, (t2 - t1) * 1000.0)
        self.jpeg_kb = _ewma(self.jpeg_kb, len(data) / 1024.0)
        self.published += 1
        self.jpegs.put(Jpeg(self.published, info.t_mono, int(info.t_mono * 1e6), data))
