# SPDX-License-Identifier: MIT
"""SimClock (spec §7.1): a deterministic Clock for tests and the fake hardware.

monotonic() and boottime() share one base; suspend(s) adds to boottime only (a suspend jump).
sleep(s) and advance(dt) move time forward and run every call_at() callback that falls due, in
time order, with the clock set to the callback's time. Single-threaded by design: the fake kernel
schedules one URB completion per callback, so a whole stream runs without a thread. RealScheduler
gives the same call_at() on the real clock: one thread sleeps until the next callback is due.
"""
import heapq
import itertools
import threading

__all__ = ["SimClock", "RealScheduler"]


class _Handle:
    __slots__ = ("t", "fn", "cancelled")

    def __init__(self, t, fn):
        self.t, self.fn, self.cancelled = t, fn, False

    def cancel(self):
        self.cancelled = True


class SimClock:
    def __init__(self, start: float = 1000.0):
        self._t = float(start)
        self.suspend_offset = 0.0
        self._heap = []
        self._seq = itertools.count()
        self.callbacks_run = 0

    def monotonic(self) -> float:
        return self._t

    def boottime(self) -> float:
        return self._t + self.suspend_offset

    def sleep(self, s: float) -> None:
        self.advance(s)

    def advance(self, dt: float) -> None:
        """Move time forward by dt (>= 0), running due callbacks in order on the way."""
        end = self._t + max(0.0, float(dt))
        heap = self._heap
        while heap and heap[0][0] <= end:
            t, _, h = heapq.heappop(heap)
            if h.cancelled:
                continue
            if t > self._t:
                self._t = t
            self.callbacks_run += 1
            h.fn()
        if end > self._t:
            self._t = end

    def advance_to(self, t: float) -> None:
        self.advance(t - self._t)

    def call_at(self, t: float, fn) -> _Handle:
        """Run fn() when the clock reaches monotonic time t (at once on the next advance if t is past)."""
        h = _Handle(float(t), fn)
        heapq.heappush(self._heap, (h.t, next(self._seq), h))
        return h

    def call_later(self, dt: float, fn) -> _Handle:
        return self.call_at(self._t + max(0.0, float(dt)), fn)

    def next_event(self) -> "float | None":
        """The time of the earliest pending callback, or None."""
        heap = self._heap
        while heap and heap[0][2].cancelled:
            heapq.heappop(heap)
        return heap[0][0] if heap else None

    def suspend(self, s: float) -> None:
        """A suspend of s seconds: boottime jumps, monotonic does not (CLOCK_MONOTONIC stops)."""
        self.suspend_offset += float(s)


class RealScheduler:
    """call_at() on the real clock: one thread runs each callback when due, holding `cond`'s lock."""

    def __init__(self, clock, cond):
        self.clock, self.cond = clock, cond
        self.heap, self.seq, self.running = [], itertools.count(), True
        self.thread = threading.Thread(target=self._run, name="fakevft", daemon=True)
        self.thread.start()

    def call_at(self, t, fn):
        h = _Handle(t, fn)
        with self.cond:
            heapq.heappush(self.heap, (t, next(self.seq), h))
            self.cond.notify_all()
        return h

    def _run(self):
        with self.cond:
            while self.running:
                while self.heap and self.heap[0][2].cancelled:
                    heapq.heappop(self.heap)
                now = self.clock.monotonic()
                if self.heap and self.heap[0][0] <= now:
                    heapq.heappop(self.heap)[2].fn()
                    continue
                self.cond.wait(min(self.heap[0][0] - now, 0.5) if self.heap else 0.5)

    def stop(self):
        with self.cond:
            self.running = False
            self.cond.notify_all()
        if self.thread is not threading.current_thread():
            self.thread.join(1.0)
