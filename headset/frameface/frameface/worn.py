# SPDX-License-Identifier: MIT
"""Is the headset worn? (spec §4.14 "Worn signal")

The eye-tracking server's shared object /dev/shm/eye-server.mmap holds a u32 ABI version at 0x00,
a u32 `initialized` flag at 0x04 and a u32 sample counter at 0x38 (the same offsets in ABI 4 and 5,
headset/frameeye_osc.py VERSION_OFF/INIT_OFF/SEQ_OFF). The counter moves only while the headset is
worn and out of standby. WornMonitor.poll() (2 Hz):

1. missing file, size < 0x40 or an inode change -> None (fail-open; the next poll starts over)
2. ABI version not 4 or 5, or initialized != 1 -> None (unknown layout or producer not ready)
3. known and initialized: the first such poll records (seq, t0); True while the counter changed
   within the last worn_timeout_s; False once it has not changed for >= worn_timeout_s, measured
   from the last change or from t0 if it never changed (not worn at service start reads False
   after worn_timeout_s); None before that.

Each poll maps the first 0x40 bytes read-only and closes the file again, so the producer may
replace the object at any time (the inode check notices it).
"""
import mmap
import os
import struct
import time

__all__ = ["WornMonitor", "NO_WORN", "EYE_SERVER_MMAP", "KNOWN_ABIS", "HEADER_BYTES", "VERSION_OFF", "INIT_OFF",
           "SEQ_OFF"]

EYE_SERVER_MMAP = "/dev/shm/eye-server.mmap"
VERSION_OFF, INIT_OFF, SEQ_OFF = 0x00, 0x04, 0x38
HEADER_BYTES = 0x40
KNOWN_ABIS = (4, 5)


def NO_WORN() -> None:
    """The "worn unknown" source of the selftest, `sim` and tests (never the real eye-server object)."""
    return None


class _Monotonic:
    @staticmethod
    def monotonic():
        return time.monotonic()


class WornMonitor:
    def __init__(self, path: str = EYE_SERVER_MMAP, clock=None, worn_timeout_s: float = 3.0):
        self.path = path
        self.clock = clock if clock is not None else _Monotonic()
        self.worn_timeout_s = float(worn_timeout_s)
        self._ino = None
        self._t0 = self._seq = self._changed_t = None
        self.last = None

    def _restart(self):
        self._t0 = self._seq = self._changed_t = None

    def _header(self):
        """(bytes of the first 0x40, (dev, ino)) or None if missing, short or unreadable."""
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0))
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if st.st_size < HEADER_BYTES:
                return None
            with mmap.mmap(fd, HEADER_BYTES, access=mmap.ACCESS_READ) as mm:
                return bytes(mm[:HEADER_BYTES]), (st.st_dev, st.st_ino)
        except (OSError, ValueError):
            return None
        finally:
            os.close(fd)

    def poll(self) -> "bool | None":
        self.last = self._poll()
        return self.last

    def _poll(self):
        got = self._header()
        if got is None:
            self._ino = None
            self._restart()
            return None
        header, ino = got
        if self._ino is not None and ino != self._ino:
            self._ino = ino                     # replaced: unknown on this poll, rules restart
            self._restart()
            return None
        self._ino = ino
        version, initialized = struct.unpack_from("<II", header, VERSION_OFF)
        if version not in KNOWN_ABIS or initialized != 1:
            self._restart()
            return None
        seq = struct.unpack_from("<I", header, SEQ_OFF)[0]
        now = self.clock.monotonic()
        if self._t0 is None:
            self._t0, self._seq = now, seq
            return None
        if seq != self._seq:
            self._seq, self._changed_t = seq, now
            return True
        since = self._changed_t if self._changed_t is not None else self._t0
        if now - since >= self.worn_timeout_s:
            return False
        return True if self._changed_t is not None else None
