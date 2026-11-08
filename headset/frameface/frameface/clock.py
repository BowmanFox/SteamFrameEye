# SPDX-License-Identifier: MIT
"""Clocks of the FrameFace service (spec §4.2).

Every timed decision takes a Clock, so tests can drive the service with a simulated clock
(`frameface.sim.simclock.SimClock`). `monotonic()` measures intervals inside one process;
`boottime()` is CLOCK_BOOTTIME on Linux (it keeps counting through a suspend, so settle times
persisted in runtime.json stay meaningful for the whole boot) and falls back to monotonic
elsewhere. `boot_id()` names the boot those boottime values belong to.
"""
import os
import time
from typing import Protocol

__all__ = ["Clock", "RealClock", "boot_id"]


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def boottime(self) -> float: ...

    def sleep(self, s: float) -> None: ...


_BOOTTIME = getattr(time, "CLOCK_BOOTTIME", None)


class RealClock:
    """The process clock: time.monotonic, CLOCK_BOOTTIME (Linux) and time.sleep."""

    def monotonic(self) -> float:
        return time.monotonic()

    def boottime(self) -> float:
        if _BOOTTIME is not None:
            try:
                return time.clock_gettime(_BOOTTIME)
            except OSError:
                pass
        return time.monotonic()

    def sleep(self, s: float) -> None:
        if s > 0:
            time.sleep(s)


def boot_id(proc_root: str = "/proc") -> str:
    """The kernel's boot id (/proc/sys/kernel/random/boot_id, stripped), or "unknown"."""
    try:
        with open(os.path.join(proc_root, "sys", "kernel", "random", "boot_id"), encoding="ascii") as f:
            value = f.read().strip()
    except (OSError, ValueError):
        return "unknown"
    return value or "unknown"
