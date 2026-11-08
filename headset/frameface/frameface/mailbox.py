# SPDX-License-Identifier: MIT
"""Single-slot mailboxes between the service's threads (spec §4.2, §4.17).

`Latest` keeps only the newest item and numbers every put(); a reader waits for an item newer
than the one it already has (wait_newer) or peeks at the newest one with its put time (latest).
`Jpeg` is the item the pixel stage publishes and the HTTP stream threads send: only frames
published while `publish` is true ever enter the mailbox ("live" JPEGs).
"""
from dataclasses import dataclass
import threading
import time
from typing import Generic, TypeVar

__all__ = ["Latest", "Jpeg"]

T = TypeVar("T")


@dataclass
class Jpeg:
    seq: int                    # published-frame counter of the service (X-Frame-Seq)
    t_mono: float               # monotonic time the frame was complete
    t_us: int                   # the same in microseconds (X-Timestamp-Us)
    data: bytes
    placeholder: bool = False


class Latest(Generic[T]):
    """The newest item of a producer. `clock` (monotonic()) stamps each put; default time.monotonic.
    Waiting uses the real time of threading.Condition."""

    def __init__(self, clock=None):
        self._now = clock.monotonic if clock is not None else time.monotonic
        self._cond = threading.Condition()
        self._seq = 0
        self._item = None
        self._t = None

    @property
    def seq(self) -> int:
        """The number of the newest item (0 before the first put)."""
        return self._seq

    def put(self, item: T) -> int:
        with self._cond:
            self._seq += 1
            self._item, self._t = item, self._now()
            self._cond.notify_all()
            return self._seq

    def wait_newer(self, seq: int, timeout: float):
        """(seq, item) of the newest item if its number is above `seq`, waiting up to `timeout`
        seconds for one; None on timeout."""
        with self._cond:
            if self._seq <= seq and timeout > 0:
                self._cond.wait_for(lambda: self._seq > seq, timeout)
            if self._seq > seq:
                return self._seq, self._item
            return None

    def latest(self):
        """(seq, item, put time) of the newest item, or None if nothing was put yet."""
        with self._cond:
            if not self._seq:
                return None
            return self._seq, self._item, self._t
