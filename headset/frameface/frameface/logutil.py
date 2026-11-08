# SPDX-License-Identifier: MIT
"""Logging of the service (spec §4.20): journal lines on stderr and events.jsonl.

Line format `<L> <component>: <event> k=v ...` with L in D/I/W/E; without JOURNAL_STREAM (not run
by systemd) an ISO timestamp prefix is added. A (component, event) pair is printed at most once per
10 s: repeats inside that window are dropped and the next printed line of that pair carries
`suppressed=<n>`. So callers put what distinguishes lines (a transition, a strategy) into the event
text and the details into k=v.

events.jsonl holds one JSON object per line, {"t": "<iso>", "bt": <boottime>, "ev": "<name>", ...};
it rotates to events.jsonl.1 when a line would take it past 256 KiB (one rotation kept). recent(n)
gives the last events as text for /status. Callers never pass pixels, the token or request paths.
"""
import collections
import datetime
import json
import math
import os
import sys
import threading
import time

from .config import ensure_dir

__all__ = ["Log", "REPEAT_S", "ROTATE_BYTES", "format_kv", "iso_now"]

REPEAT_S = 10.0
ROTATE_BYTES = 256 * 1024
_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
_MAX_KEYS = 512


def iso_now() -> str:
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S")


def _value(v):
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        if not math.isfinite(v):
            return "none" if math.isnan(v) else ("inf" if v > 0 else "-inf")
        text = ("%.3f" % v).rstrip("0")
        return text + "0" if text.endswith(".") else text          # 2.0 stays "2.0", 1.25 "1.25"
    if v is None:
        return "none"
    text = str(v)
    if not text or any(c in text for c in ' "=\t\n'):
        return json.dumps(text)
    return text


def format_kv(kv: dict) -> str:
    return " ".join("%s=%s" % (k, _value(v)) for k, v in kv.items())


def _jsonable(v):
    """JSON-safe values: inf/nan become null (json.dumps would write Infinity), others str()."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [_jsonable(x) for x in (sorted(v) if isinstance(v, (set, frozenset)) else v)]
    return str(v)


class _RealClock:
    @staticmethod
    def monotonic():
        return time.monotonic()

    @staticmethod
    def boottime():
        clk = getattr(time, "CLOCK_BOOTTIME", None)
        if clk is not None:
            try:
                return time.clock_gettime(clk)
            except OSError:
                pass
        return time.monotonic()


class Log:
    """Journal lines and events of one process. `clock` (monotonic, boottime) drives the repeat
    window and the "bt" field; default the real clocks. Thread-safe."""

    def __init__(self, level: str, events_path: "str | None", journal: "bool | None" = None, stream=sys.stderr,
                 clock=None, keep: int = 20):
        self.level = _LEVELS.get(level, 20)
        self.events_path = events_path
        self.journal = bool(os.environ.get("JOURNAL_STREAM")) if journal is None else bool(journal)
        self.stream = stream
        self.clock = clock if clock is not None else _RealClock()
        self._lock = threading.Lock()
        self._seen = {}                         # (component, event) -> [last printed t, suppressed]
        self._recent = collections.deque(maxlen=max(20, keep))
        self.lines = collections.deque(maxlen=400)      # the last printed lines (tests, diagnostics)

    # -- journal lines

    def d(self, component: str, event: str, **kv) -> None:
        self._line("D", 10, component, event, kv)

    def i(self, component: str, event: str, **kv) -> None:
        self._line("I", 20, component, event, kv)

    def w(self, component: str, event: str, **kv) -> None:
        self._line("W", 30, component, event, kv)

    def e(self, component: str, event: str, **kv) -> None:
        self._line("E", 40, component, event, kv)

    def _line(self, letter, level, component, event, kv):
        if level < self.level:
            return
        now = self.clock.monotonic()
        key = (component, event)
        with self._lock:
            seen = self._seen.get(key)
            if seen is not None and now - seen[0] < REPEAT_S:
                seen[1] += 1
                return
            suppressed = seen[1] if seen is not None else 0
            if len(self._seen) >= _MAX_KEYS:
                for k in [k for k, s in self._seen.items() if now - s[0] >= REPEAT_S]:
                    del self._seen[k]
            self._seen[key] = [now, 0]
            text = "%s %s: %s" % (letter, component, event)
            if kv:
                text += " " + format_kv(kv)
            if suppressed:
                text += " suppressed=%d" % suppressed
            if not self.journal:
                text = iso_now() + " " + text
            self.lines.append(text)
            stream = self.stream
            if stream is not None:
                try:
                    stream.write(text + "\n")
                    stream.flush()
                except (OSError, ValueError):     # a closed or hung-up stderr (ssh -t drop) is not fatal
                    pass

    def has(self, text: str) -> bool:
        """True if a printed line contains `text` (tests and diagnostics)."""
        with self._lock:
            return any(text in line for line in self.lines)

    # -- events.jsonl

    def event(self, name: str, **kv) -> None:
        stamp = iso_now()
        record = {"t": stamp, "bt": round(self.clock.boottime(), 3), "ev": name}
        record.update({k: _jsonable(v) for k, v in kv.items()})
        line = json.dumps(record, separators=(",", ":"), sort_keys=False) + "\n"
        brief = "%s %s" % (stamp, name) + (" " + format_kv({k: v for k, v in kv.items()
                                                             if not isinstance(v, (dict, list))}) if kv else "")
        with self._lock:
            self._recent.append(brief.rstrip())
            if self.events_path:
                self._append(line.encode("utf-8"))

    def _append(self, data):
        path = self.events_path
        try:
            folder = os.path.dirname(os.path.abspath(path))
            if not os.path.isdir(folder):
                ensure_dir(folder)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            if size and size + len(data) > ROTATE_BYTES:
                os.replace(path, path + ".1")
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        except OSError:
            pass                                # a full or read-only disk must not stop the service

    def recent(self, n: int = 20) -> list:
        with self._lock:
            items = list(self._recent)
        return items[-n:] if n > 0 else []
