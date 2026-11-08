# SPDX-License-Identifier: MIT
"""Persistent state below the state dir (spec §4.2, §4.19): runtime.json (dirty marker, settle
times, the last FAULT /status cannot report) and profile.json (the known-good (strategy, xu_len)
pair, IR level, byte plane, payload stride), keyed by the descriptor SHA-1.

Every write goes to a temporary file in the same directory, is fsynced and os.replace()d over the
target; a write error propagates as OSError. A corrupt or invalid file loads as defaults (profile:
None) with one entry in Store.warnings. The boottime fields belong to `boot_id`: when the stored
boot id is not the current one, the mark_* methods start from a fresh Runtime (keeping `fault`).
"""
from dataclasses import dataclass, asdict, fields
import datetime
import json
import math

from .clock import boot_id as read_boot_id
from .config import atomic_write, ensure_dir
from .policy import IR_LEVELS, STRATEGIES

__all__ = ["Runtime", "Profile", "Store"]


@dataclass
class Runtime:
    v: int = 1
    boot_id: str = ""
    dirty: bool = False
    dirty_since_bt: "float | None" = None
    last_teardown_bt: "float | None" = None
    last_reset_bt: "float | None" = None
    last_loss_bt: "float | None" = None
    fault: "dict | None" = None         # e.g. {"kind": "port", "port": 9023, "errno": ..., "owner": ..., "since": ...}


@dataclass
class Profile:
    v: int = 1
    desc_sha1: str = ""
    speed: int = 0
    get_len: int = 0
    strategy: "str | None" = None
    xu_len: "int | None" = None         # known-good pair (strategy, xu_len)
    source: str = ""                    # "service" | "probe"
    y_phase: "str | None" = None
    plane: "dict | None" = None
    payload_stride: "int | None" = None
    fps_pts: "float | None" = None
    ir_level: str = "L0"
    ir_reason: str = ""
    updated: str = ""


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _opt(check):
    return lambda v: v is None or check(v)


def _number(v):                          # finite only: an Infinity boottime would never settle
    return _is_int(v) or (isinstance(v, float) and math.isfinite(v))


_RUNTIME_CHECKS = {
    "v": lambda v: v == 1 and _is_int(v), "boot_id": lambda v: isinstance(v, str),
    "dirty": lambda v: isinstance(v, bool), "dirty_since_bt": _opt(_number), "last_teardown_bt": _opt(_number),
    "last_reset_bt": _opt(_number), "last_loss_bt": _opt(_number), "fault": _opt(lambda v: isinstance(v, dict)),
}
_PROFILE_CHECKS = {
    "v": lambda v: v == 1 and _is_int(v), "desc_sha1": lambda v: isinstance(v, str),
    "speed": _is_int, "get_len": _is_int, "strategy": _opt(lambda v: isinstance(v, str) and v in STRATEGIES),
    "xu_len": _opt(lambda v: _is_int(v) and 16 <= v <= 1024), "source": lambda v: isinstance(v, str),
    "y_phase": _opt(lambda v: v in ("odd", "even")), "plane": _opt(lambda v: isinstance(v, dict)),
    "payload_stride": _opt(lambda v: _is_int(v) and v > 0), "fps_pts": _opt(_number),
    "ir_level": lambda v: isinstance(v, str) and v in IR_LEVELS, "ir_reason": lambda v: isinstance(v, str),
    "updated": lambda v: isinstance(v, str),
}


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    """runtime.json and profile.json of `paths.state_dir`. `boot_id` defaults to the kernel's
    (clock.boot_id()); tests inject one."""

    def __init__(self, paths, clock, boot_id: "str | None" = None):
        self.paths = paths
        self.clock = clock
        self.boot_id = read_boot_id() if boot_id is None else boot_id
        self.warnings = []

    # -- generic load / save

    def _load(self, path, cls, checks):
        """cls instance from `path`; None if it is missing or unusable (a warning unless missing)."""
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            return None
        except OSError as e:
            self.warnings.append("state: %s unreadable (%s); using defaults" % (path, e))
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError) as e:         # RecursionError: absurdly deep nesting
            self.warnings.append("state: %s is corrupt (%s); using defaults" % (path, e))
            return None
        if not isinstance(data, dict) or not checks["v"](data.get("v")):
            self.warnings.append("state: %s has no version 1 object; using defaults" % path)
            return None
        values, bad = {}, []
        for f in fields(cls):
            if f.name in data:
                if checks[f.name](data[f.name]):
                    values[f.name] = data[f.name]
                else:
                    bad.append(f.name)
        if bad:
            self.warnings.append("state: %s has invalid %s; using defaults for them" % (path, ", ".join(bad)))
        return cls(**values)

    def _save(self, path, obj):
        ensure_dir(self.paths.state_dir)
        atomic_write(path, (json.dumps(asdict(obj), indent=1) + "\n").encode("utf-8"), 0o600)

    # -- runtime.json

    def load_runtime(self) -> Runtime:
        r = self._load(self.paths.runtime, Runtime, _RUNTIME_CHECKS)
        return Runtime() if r is None else r

    def save_runtime(self, r: Runtime) -> None:
        self._save(self.paths.runtime, r)

    def same_boot(self, r: Runtime) -> bool:
        """True if `r` was written during this boot (its boottime fields are comparable)."""
        return bool(r.boot_id) and r.boot_id == self.boot_id

    def dirty_start(self) -> bool:
        """runtime.json says this boot left the device dirty (decision 10)."""
        r = self.load_runtime()
        return self.same_boot(r) and r.dirty

    def _current(self):
        r = self.load_runtime()
        if not self.same_boot(r):
            r = Runtime(boot_id=self.boot_id, fault=r.fault)
        return r

    def mark_dirty(self) -> None:
        r = self._current()
        r.dirty, r.dirty_since_bt = True, self.clock.boottime()
        self.save_runtime(r)

    def mark_clean_teardown(self) -> None:
        r = self._current()
        r.dirty, r.dirty_since_bt, r.last_teardown_bt = False, None, self.clock.boottime()
        self.save_runtime(r)

    def mark_reset(self) -> None:
        r = self._current()
        r.last_reset_bt = self.clock.boottime()
        self.save_runtime(r)

    def mark_lost(self) -> None:
        r = self._current()
        r.dirty, r.dirty_since_bt, r.last_loss_bt = False, None, self.clock.boottime()
        self.save_runtime(r)

    def set_fault(self, fault: "dict | None") -> None:
        """Record (or clear with None) the FAULT that /status cannot report, e.g. kind "port"."""
        r = self._current()
        r.fault = fault
        self.save_runtime(r)

    # -- profile.json

    def load_profile(self, desc_sha1: str) -> "Profile | None":
        """The profile of these descriptors, or None (missing, corrupt or another descriptor SHA-1)."""
        p = self._load(self.paths.profile, Profile, _PROFILE_CHECKS)
        return p if p is not None and p.desc_sha1 == desc_sha1 else None

    def save_profile(self, p: Profile) -> None:
        """Stamp p.updated (UTC ISO time) and write profile.json."""
        p.updated = _now_iso()
        self._save(self.paths.profile, p)
