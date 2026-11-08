# SPDX-License-Identifier: MIT
"""Health gate and plane analysis of complete frames (spec §4.11, §4.15.6). Pure.

classify(): sample s = frame[phase::78] (4103 bytes of the chosen byte plane)
  dark    mean(s) < 3.0 and max(s) < 16
  testbar not dark, fewer than 16 distinct values and mean(s) > 8 (the bridge chip's test bars)
  frozen  crc32(frame[::7]) equals the previous frame's
  ok      otherwise
analyze_layout(): rows 0, 25, ..., 375 of the 800-byte rows; even/odd byte planes and the four
byte phases; choose_phase() keeps Baballonia's odd bytes unless 3 of the last 5 layouts show a
flat odd plane.
"""
from dataclasses import dataclass
import zlib

__all__ = ["Health", "HealthTracker", "Layout", "analyze_layout", "LayoutSchedule", "choose_phase", "KINDS",
           "SAMPLE_STEP", "ROW_BYTES", "LAYOUT_ROWS"]

KINDS = ("ok", "dark", "testbar", "frozen")
SAMPLE_STEP = 78
DARK_MEAN, DARK_MAX = 3.0, 16
TESTBAR_DISTINCT, TESTBAR_MEAN = 16, 8
FROZEN_STEP = 7
ROW_BYTES = 800                          # 400 YUYV pixels
LAYOUT_ROWS = tuple(range(0, 400, 25))   # rows 0, 25, ..., 375
PHASE_WINDOW = 5


@dataclass(frozen=True)
class Health:
    kind: str                            # "ok" | "dark" | "testbar" | "frozen"
    mean: float
    max: int
    distinct: int


def _contiguous(data):
    return data.tobytes() if isinstance(data, memoryview) else data


class HealthTracker:
    """Classifies each complete frame; consecutive[kind] counts the current run of that kind
    (the other kinds read 0)."""

    def __init__(self, phase: int):
        if phase not in (0, 1):
            raise ValueError("phase must be 0 (even bytes) or 1 (odd bytes)")
        self.phase = phase
        self.consecutive = dict.fromkeys(KINDS, 0)
        self._crc = None

    def classify(self, frame) -> Health:
        s = _contiguous(frame[self.phase::SAMPLE_STEP])
        mean = sum(s) / len(s) if len(s) else 0.0
        top = max(s) if len(s) else 0
        distinct = len(set(s))
        crc = zlib.crc32(_contiguous(frame[::FROZEN_STEP]))
        frozen, self._crc = crc == self._crc, crc
        if mean < DARK_MEAN and top < DARK_MAX:
            kind = "dark"
        elif distinct < TESTBAR_DISTINCT and mean > TESTBAR_MEAN:
            kind = "testbar"
        elif frozen:
            kind = "frozen"
        else:
            kind = "ok"
        for k in KINDS:
            self.consecutive[k] = self.consecutive[k] + 1 if k == kind else 0
        return Health(kind, mean, top, distinct)


@dataclass
class Layout:
    std_even: float
    std_odd: float
    mad_even_odd: float
    phase_means: tuple
    phase_stds: tuple
    verdict: str                         # identical | odd_flat | even_flat | distinct
    h2_hint: bool


def _mean_std(values):
    n = len(values)
    if not n:
        return 0.0, 0.0
    s1 = sum(values)
    s2 = sum(v * v for v in values)
    var = (n * s2 - s1 * s1) / (n * n)                  # exact integer numerator: population variance
    return s1 / n, max(var, 0.0) ** 0.5


def analyze_layout(frame) -> Layout:
    """Plane statistics of one frame (spec §4.15.6)."""
    rows = [_contiguous(frame[r * ROW_BYTES:(r + 1) * ROW_BYTES]) for r in LAYOUT_ROWS]
    even = b"".join(row[0::2] for row in rows)
    odd = b"".join(row[1::2] for row in rows)
    _, std_even = _mean_std(even)
    _, std_odd = _mean_std(odd)
    mad = sum(abs(a - b) for a, b in zip(even, odd)) / len(even) if even else 0.0
    phases = [_mean_std(b"".join(row[k::4] for row in rows)) for k in range(4)]
    if mad < 0.5:
        verdict = "identical"
    elif std_odd < 3 and std_even > 10:
        verdict = "odd_flat"
    elif std_even < 3 and std_odd > 10:
        verdict = "even_flat"
    else:
        verdict = "distinct"
    h2 = verdict == "distinct" and std_even > 10 and std_odd > 10 and mad >= 2
    return Layout(std_even, std_odd, mad, tuple(m for m, _ in phases), tuple(s for _, s in phases), verdict, h2)


class LayoutSchedule:
    """The single schedule on which ok frames feed analyze_layout (§4.15.6, referenced by §4.11):
    the 30th ok frame of a stream, then the first ok frame after each further 10 s during the
    first 60 s of the stream (counted from its first ok frame), then every 60th ok frame.
    Call reset() when a stream starts; ok_frame(t) for each ok frame says whether to analyse it."""

    FIRST, STEP_S, EARLY_S, EVERY = 30, 10.0, 60.0, 60

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.n = 0
        self._t0 = self._last = None

    def ok_frame(self, t: float) -> bool:
        self.n += 1
        if self._t0 is None:
            self._t0 = t
        if self.n < self.FIRST:
            return False
        if self.n == self.FIRST or (t - self._t0 < self.EARLY_S and t - self._last >= self.STEP_S):
            self._last = t
            return True
        return t - self._t0 >= self.EARLY_S and self.n % self.EVERY == 0


def choose_phase(layouts: list, configured: str) -> tuple:
    """(phase, reason): a configured "odd"/"even" wins; "auto" takes the even bytes (0, "odd_flat")
    only if at least 3 of the last 5 layouts are odd_flat, else Baballonia's odd bytes (1, "default")."""
    if configured == "odd":
        return 1, "configured"
    if configured == "even":
        return 0, "configured"
    recent = list(layouts)[-PHASE_WINDOW:]
    if sum(1 for lay in recent if lay.verdict == "odd_flat") >= 3:
        return 0, "odd_flat"
    return 1, "default"
