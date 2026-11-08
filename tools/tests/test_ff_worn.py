# SPDX-License-Identifier: MIT
"""WornMonitor (spec §11.3 test_ff_worn, task T4): the eye-server object as temp files, driven by a
SimClock (no sleeps): missing and short files, unknown ABIs and an uninitialised producer are
unknown (None); a known, initialised object whose counter never moves reads False worn_timeout_s
after the first poll (not worn at service start); a moving counter is True; a counter that stops
reads False after worn_timeout_s; a replaced file (new inode) is None on that poll, then the rules
restart with a new t0."""
import ffutil  # first: installs the port guard

import os
import shutil
import struct
import tempfile
import unittest

ffutil.setup_path()
from frameface.sim import SimClock  # noqa: E402
from frameface.worn import HEADER_BYTES, INIT_OFF, NO_WORN, SEQ_OFF, VERSION_OFF, WornMonitor  # noqa: E402


class Worn(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ffworn-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "eye-server.mmap")
        self.clock = SimClock()
        self.mon = WornMonitor(self.path, self.clock, worn_timeout_s=3.0)

    def write(self, version=5, initialized=1, seq=0, size=0x1000, path=None):
        data = bytearray(size)
        if size >= HEADER_BYTES:
            struct.pack_into("<II", data, VERSION_OFF, version, initialized)
            struct.pack_into("<I", data, SEQ_OFF, seq)
        with open(path or self.path, "wb") as f:
            f.write(data)

    def bump(self, seq):
        """The producer writes the counter in place (same inode)."""
        with open(self.path, "r+b") as f:
            f.seek(SEQ_OFF)
            f.write(struct.pack("<I", seq))

    def polls(self, seconds, step=0.5):
        out = []
        for _ in range(int(round(seconds / step))):
            self.clock.advance(step)
            out.append(self.mon.poll())
        return out

    def test_missing_short_unknown_abi_and_uninitialised_are_unknown(self):
        self.assertIsNone(self.mon.poll())                      # missing file
        self.write(size=HEADER_BYTES - 1)
        self.assertIsNone(self.mon.poll())                      # shorter than 0x40
        for version in (3, 6):
            self.write(version=version)
            self.assertEqual(self.polls(5.0), [None] * 10, version)
        self.write(version=4, initialized=0)
        self.assertEqual(self.polls(5.0), [None] * 10)
        self.assertIsNone(NO_WORN())

    def test_still_counter_reads_false_after_the_timeout(self):
        for version in (4, 5):
            self.mon = WornMonitor(self.path, self.clock, worn_timeout_s=3.0)
            self.write(version=version, seq=77)
            self.assertIsNone(self.mon.poll())                  # t0: the first known poll
            got = self.polls(4.0)                               # 0.5 .. 4.0 s after t0
            self.assertEqual(got, [None] * 5 + [False] * 3, version)    # False from 3.0 s on

    def test_moving_counter_is_true_then_false_once_it_stops(self):
        self.write(seq=1)
        self.assertIsNone(self.mon.poll())
        for seq in range(2, 12):
            self.clock.advance(0.5)
            self.bump(seq)
            self.assertIs(self.mon.poll(), True, seq)
        got = self.polls(4.0)                                    # the headset comes off: counter still
        self.assertEqual(got, [True] * 5 + [False] * 3)
        self.clock.advance(0.5)
        self.bump(99)
        self.assertIs(self.mon.poll(), True)

    def test_replaced_file_restarts_the_rules_with_a_new_t0(self):
        self.write(seq=5)
        self.assertIsNone(self.mon.poll())
        self.assertEqual(self.polls(3.0)[-1], False)
        fresh = os.path.join(self.dir, "new.mmap")
        self.write(seq=5, path=fresh)
        os.replace(fresh, self.path)                            # the eye server re-created its object
        self.clock.advance(0.5)
        self.assertIsNone(self.mon.poll())                      # the inode change itself
        self.assertEqual(self.polls(3.5), [None] * 6 + [False])  # a new t0 at the first poll after it
        self.clock.advance(0.5)
        self.bump(6)
        self.assertIs(self.mon.poll(), True)

    def test_file_removed_then_back(self):
        self.write(seq=1)
        self.mon.poll()
        os.remove(self.path)
        self.clock.advance(0.5)
        self.assertIsNone(self.mon.poll())
        self.write(seq=1)
        self.assertEqual(self.polls(3.5), [None] * 6 + [False])


if __name__ == "__main__":
    unittest.main()
