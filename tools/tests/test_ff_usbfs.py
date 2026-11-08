# SPDX-License-Identifier: MIT
"""The usbfs ABI and UsbDevice (spec §4.3; §11.3 test_ff_usbfs, task T2): struct sizes and every field
offset, the 15 ioctl numbers, abi_table() in the Appendix A.6 format, FIONREAD through LinuxSys (Linux),
abi_dump == abi_table() (Linux with gcc or FRAMEFACE_ABI_DUMP) and the fcntl source scan. Flush: an
ESHUTDOWN (status or errno; a system suspend flushes every URB) is not a loss unless GET_SPEED's ENODEV
or poll's HUP/ERR confirm one (usb-2)."""
import ffutil  # first: installs the port guard

import collections
import ctypes as C
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ffutil.setup_path()
from frameface import usbfs as U  # noqa: E402
from frameface import uvc  # noqa: E402
from frameface.sim import check_trace  # noqa: E402
from frameface.vft import XuError  # noqa: E402
from test_ff_driver import XU_SETS, Session  # noqa: E402
from ffsup import RigCase  # noqa: E402

A6 = json.loads("""
{"sizes":{"ctrltransfer":24,"setinterface":8,"urb":56,"disconnect_claim":264,"getdriver":260},
 "offsets":{"ctrltransfer.bRequestType":0,"ctrltransfer.bRequest":1,"ctrltransfer.wValue":2,"ctrltransfer.wIndex":4,
            "ctrltransfer.wLength":6,"ctrltransfer.timeout":8,"ctrltransfer.data":16,
            "setinterface.interface":0,"setinterface.altsetting":4,
            "urb.type":0,"urb.endpoint":1,"urb.status":4,"urb.flags":8,"urb.buffer":16,"urb.buffer_length":24,
            "urb.actual_length":28,"urb.start_frame":32,"urb.number_of_packets":36,"urb.error_count":40,"urb.signr":44,
            "urb.usercontext":48,"disconnect_claim.interface":0,"disconnect_claim.flags":4,"disconnect_claim.driver":8,
            "getdriver.interface":0,"getdriver.driver":4},
 "ioctl":{"CONTROL":3222820096,"SETINTERFACE":2148029700,"SETCONFIGURATION":2147767557,"GETDRIVER":1090802952,
          "SUBMITURB":2151175434,"DISCARDURB":21771,"REAPURB":1074287884,"REAPURBNDELAY":1074287885,
          "CLAIMINTERFACE":2147767567,"RELEASEINTERFACE":2147767568,"RESET":21780,"CLEAR_HALT":2147767573,
          "GET_CAPABILITIES":2147767578,"DISCONNECT_CLAIM":2164806939,"GET_SPEED":21791},
 "consts":{"URB_TYPE_BULK":3,"DISCONNECT_CLAIM_IF_DRIVER":1,"DISCONNECT_CLAIM_EXCEPT_DRIVER":2,"FIONREAD":21531}}
""")
IOCTL_HEX = {"CONTROL": 0xC0185500, "SETINTERFACE": 0x80085504, "SETCONFIGURATION": 0x80045505,
             "GETDRIVER": 0x41045508, "SUBMITURB": 0x8038550A, "DISCARDURB": 0x0000550B, "REAPURB": 0x4008550C,
             "REAPURBNDELAY": 0x4008550D, "CLAIMINTERFACE": 0x8004550F, "RELEASEINTERFACE": 0x80045510,
             "RESET": 0x00005514, "CLEAR_HALT": 0x80045515, "GET_CAPABILITIES": 0x8004551A,
             "DISCONNECT_CLAIM": 0x8108551B, "GET_SPEED": 0x0000551F}
FIELDS = {U.Ctrl: [("bRequestType", 0), ("bRequest", 1), ("wValue", 2), ("wIndex", 4), ("wLength", 6), ("timeout", 8),
                   ("data", 16)],
          U.SetIf: [("interface", 0), ("altsetting", 4)],
          U.Urb: [("type", 0), ("endpoint", 1), ("status", 4), ("flags", 8), ("buffer", 16), ("buffer_length", 24),
                  ("actual_length", 28), ("start_frame", 32), ("number_of_packets", 36), ("error_count", 40),
                  ("signr", 44), ("usercontext", 48)],
          U.DisconnectClaim: [("interface", 0), ("flags", 4), ("driver", 8)],
          U.GetDriver: [("interface", 0), ("driver", 4)]}
SOURCE = os.path.join(str(ffutil.FRAMEFACE_DIR), "csrc", "abi_dump.c")


class Abi(unittest.TestCase):
    def test_struct_sizes(self):
        self.assertEqual([C.sizeof(c) for c in (U.Ctrl, U.SetIf, U.Urb, U.DisconnectClaim, U.GetDriver)],
                         [24, 8, 56, 264, 260])

    def test_every_field_offset(self):
        for cls, fields in FIELDS.items():
            self.assertEqual([f for f, _ in cls._fields_], [f for f, _ in fields], cls.__name__)
            for name, offset in fields:
                self.assertEqual(getattr(cls, name).offset, offset, "%s.%s" % (cls.__name__, name))

    def test_the_15_ioctl_numbers(self):
        self.assertEqual(len(IOCTL_HEX), 15)
        self.assertEqual(U.IOCTLS, IOCTL_HEX)
        for name, value in IOCTL_HEX.items():
            self.assertEqual(getattr(U, name), value, name)
        self.assertEqual(U.SETCONFIGURATION, 0x80045505)

    def test_abi_table_is_appendix_a6(self):
        table = U.abi_table()
        self.assertEqual(table, A6)
        self.assertEqual(list(table), ["sizes", "offsets", "ioctl", "consts"])
        self.assertEqual(json.loads(json.dumps(table)), A6)
        self.assertTrue(all(isinstance(v, int) for sec in table.values() for v in sec.values()))

    def test_constants(self):
        self.assertEqual((U.FIONREAD, U.URB_TYPE_BULK, U.DISCONNECT_CLAIM_IF_DRIVER, U.DISCONNECT_CLAIM_EXCEPT_DRIVER),
                         (0x541B, 3, 1, 2))
        self.assertEqual((U.POLLOUT, U.POLLERR, U.POLLHUP), (0x004, 0x008, 0x010))
        self.assertEqual(U.OPEN_FLAGS, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
        self.assertEqual(U.URB_STATUS["EPROTO"], -71)                     # Linux numbers, whatever the host
        self.assertEqual(U.SPEEDS[3], 480)
        self.assertEqual(U.CAPS["BULK_CONTINUATION"], 0x02)


class Native(unittest.TestCase):
    def test_fionread_through_linuxsys(self):
        ffutil.require_native("linux-syscalls", reason="FIONREAD through libc ioctl")
        sysi = U.LinuxSys()
        r, w = os.pipe()
        try:
            os.write(w, b"12345")
            v = C.c_int(-1)
            self.assertEqual((sysi.ioctl(r, U.FIONREAD, C.addressof(v)), v.value), (0, 5))
            with self.assertRaises(OSError) as cm:                         # errno comes from ctypes, not -1
                sysi.ioctl(r, U.GET_SPEED, 0)
            self.assertIn(cm.exception.errno, (errno.ENOTTY, errno.EINVAL))
            self.assertTrue(sysi.wait(w, 0.0) & U.POLLOUT)
        finally:
            os.close(r)
            os.close(w)

    def test_abi_dump_matches_abi_table(self):
        exe = os.environ.get("FRAMEFACE_ABI_DUMP", "").strip()
        if exe:
            ffutil.require_native(reason="run FRAMEFACE_ABI_DUMP")
        else:
            gcc = ffutil.require_native("gcc", reason="build csrc/abi_dump.c")["gcc"]
            tmp = tempfile.mkdtemp(prefix="ffabi-")
            self.addCleanup(shutil.rmtree, tmp, True)
            exe = os.path.join(tmp, "abi_dump")
            subprocess.run([gcc, "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror", "-o", exe, SOURCE], check=True,
                           capture_output=True, timeout=120)
        out = json.loads(subprocess.run([exe], check=True, capture_output=True, timeout=60).stdout)
        table = U.abi_table()
        self.assertEqual(list(out), list(table))
        for section in table:
            for key, value in table[section].items():
                self.assertEqual(out[section].get(key), value, "%s.%s" % (section, key))
        self.assertEqual(out, table)

    def test_linuxsys_exists_only_on_linux(self):
        if sys.platform.startswith("linux"):
            self.assertIsNotNone(U.LinuxSys())
        else:
            with self.assertRaises(OSError):
                U.LinuxSys()


class SourceScan(unittest.TestCase):
    def test_no_fcntl_ioctl_under_headset_frameface(self):
        bad = re.compile(r"fcntl\s*\.\s*ioctl|from\s+fcntl\s+import[^\n]*\bioctl\b")
        scanned = []
        for dirpath, _, names in os.walk(str(ffutil.FRAMEFACE_DIR)):
            for name in names:
                if os.path.splitext(name)[1] in (".py", ".c", ".h", ".sh", ".service", ".rules", ".ps1"):
                    path = os.path.join(dirpath, name)
                    with open(path, encoding="utf-8", errors="replace") as f:
                        self.assertIsNone(bad.search(f.read()), path)
                    scanned.append(name)
        self.assertIn("usbfs.py", scanned)
        self.assertIn("abi_dump.c", scanned)
        with open(U.__file__, encoding="utf-8") as f:
            self.assertIn("CDLL(None, use_errno=True)", f.read())


class RecSys:
    """A Sys that records each ioctl with the struct it points at, and answers by script."""

    def __init__(self):
        self.calls, self.errors, self.reply, self.reap_addr, self.closed = [], {}, b"", None, []

    def open(self, path, flags):
        self.calls.append(("open", path, flags))
        return 7

    def close(self, fd):
        self.closed.append(fd)

    def wait(self, fd, timeout_s):
        self.calls.append(("wait", fd, timeout_s))
        return U.POLLOUT

    def ioctl(self, fd, request, arg):
        if request in self.errors:
            raise OSError(self.errors[request], os.strerror(self.errors[request]))
        if request == U.CONTROL:
            c = U.Ctrl.from_address(arg)
            self.calls.append(("ctrl", c.bRequestType, c.bRequest, c.wValue, c.wIndex, c.wLength, c.timeout,
                               C.string_at(c.data, c.wLength) if c.data and not c.bRequestType & 0x80 else None))
            if c.bRequestType & 0x80:
                C.memmove(c.data, self.reply, len(self.reply))
                return len(self.reply)
            return c.wLength
        if request == U.DISCONNECT_CLAIM:
            dc = U.DisconnectClaim.from_address(arg)
            self.calls.append(("dc", dc.interface, dc.flags, dc.driver))
        elif request == U.GETDRIVER:
            U.GetDriver.from_address(arg).driver = b"uvcvideo"
        elif request == U.REAPURBNDELAY:
            C.c_void_p.from_address(arg).value = self.reap_addr
        elif request == U.SETINTERFACE:
            s = U.SetIf.from_address(arg)
            self.calls.append(("setif", s.interface, s.altsetting))
        elif request in (U.CLAIMINTERFACE, U.RELEASEINTERFACE, U.CLEAR_HALT, U.SETCONFIGURATION):
            self.calls.append((request, C.c_uint.from_address(arg).value))
        elif request == U.GET_CAPABILITIES:
            C.c_uint32.from_address(arg).value = 0x1F
        else:
            self.calls.append((request, arg))
        return 3 if request == U.GET_SPEED else 0


class Device(unittest.TestCase):
    def setUp(self):
        self.sys = RecSys()
        self.dev = U.UsbDevice(self.sys, "/dev/bus/usb/001/003")
        self.dev.open()

    def test_open_flags_and_control_transfers(self):
        self.assertEqual(self.sys.calls[0], ("open", "/dev/bus/usb/001/003", U.OPEN_FLAGS))
        self.sys.reply = bytes([0x40, 0x00])
        self.assertEqual(self.dev.control_in(uvc.xu_setup(uvc.GET_LEN, 2), 1000), b"\x40\x00")
        self.assertEqual(self.sys.calls[-1], ("ctrl", 0xA1, 0x85, 0x0200, 0x0400, 2, 1000, None))
        self.assertEqual(self.dev.control_out(uvc.xu_setup(uvc.SET_CUR, 64), uvc.magic(64), 500), 64)
        self.assertEqual(self.sys.calls[-1], ("ctrl", 0x21, 0x01, 0x0200, 0x0400, 64, 500, uvc.magic(64)))
        self.assertEqual(self.dev.control_out(uvc.set_interface_setup(), b""), 0)
        with self.assertRaises(ValueError):
            self.dev.control_out(uvc.xu_setup(uvc.SET_CUR, 64), b"\x51\x52")

    def test_interface_calls(self):
        self.dev.claim(0)
        self.dev.claim(1)
        self.dev.set_interface(1, 0)
        self.dev.clear_halt(0x81)
        self.dev.release(1)
        self.dev.set_configuration(1)
        self.assertEqual(self.sys.calls[1:], [(U.CLAIMINTERFACE, 0), (U.CLAIMINTERFACE, 1), ("setif", 1, 0),
                                              (U.CLEAR_HALT, 0x81), (U.RELEASEINTERFACE, 1), (U.SETCONFIGURATION, 1)])
        self.assertEqual(self.dev.claimed, {0})
        self.assertEqual((self.dev.speed(), self.dev.capabilities(), self.dev.get_driver(0)), (3, 0x1F, "uvcvideo"))
        self.sys.errors[U.GETDRIVER] = errno.ENODATA
        self.assertIsNone(self.dev.get_driver(1))

    def test_disconnect_claim_is_always_except_usbfs(self):
        self.dev.disconnect_claim(1)
        self.assertEqual(self.sys.calls[-1], ("dc", 1, 0x02, b"usbfs"))
        self.assertIn(1, self.dev.claimed)

    def test_urb_slots_submit_reap_discard(self):
        slots = self.dev.alloc_slots(3, 32768, 0x81) + self.dev.alloc_slots(1, 4096, 0x81)
        self.assertEqual([s.index for s in slots], [0, 1, 2, 3])
        u = slots[1].urb
        self.assertEqual((u.type, u.endpoint, u.flags, u.buffer, u.buffer_length, u.usercontext),
                         (3, 0x81, 0, C.addressof(slots[1].buf), 32768, 1))
        u.status, u.actual_length = -71, 99
        self.dev.submit(slots[1])
        self.assertEqual((u.status, u.actual_length, slots[1].inflight), (0, 0, True))
        self.sys.reap_addr = C.addressof(u)
        self.assertIs(self.dev.reap(), slots[1])
        self.assertFalse(slots[1].inflight)
        self.sys.reap_addr = 12345
        with self.assertRaises(OSError):
            self.dev.reap()
        self.sys.errors[U.REAPURBNDELAY] = errno.EAGAIN
        self.assertIsNone(self.dev.reap())
        self.sys.errors[U.DISCARDURB] = errno.EINVAL
        self.dev.discard(slots[0])                                         # already completed: swallowed
        self.sys.errors[U.DISCARDURB] = errno.EIO
        with self.assertRaises(OSError):
            self.dev.discard(slots[0])

    def test_enodev_is_device_gone_eshutdown_is_not(self):
        self.sys.errors = {U.CLAIMINTERFACE: errno.ENODEV, U.REAPURBNDELAY: errno.ENODEV}
        for call in (lambda: self.dev.claim(0), self.dev.reap):
            with self.assertRaises(U.DeviceGone) as cm:
                call()
            self.assertIsInstance(cm.exception, OSError)
        slot = self.dev.alloc_slots(1, 4096, 0x81)[0]
        for request, call in ((U.SUBMITURB, lambda: self.dev.submit(slot)),
                              (U.CLAIMINTERFACE, lambda: self.dev.claim(0)),
                              (U.CONTROL, lambda: self.dev.control_out(uvc.xu_setup(uvc.SET_CUR, 64), uvc.magic(64)))):
            self.sys.errors = {request: errno.ESHUTDOWN}       # flushed (suspend, stopped HC): no proof of a loss
            with self.assertRaises(OSError) as cm:
                call()
            self.assertEqual((cm.exception.errno, isinstance(cm.exception, U.DeviceGone)), (errno.ESHUTDOWN, False))
        self.sys.errors = {U.SETINTERFACE: errno.EBUSY}
        with self.assertRaises(OSError) as cm:
            self.dev.set_interface(1, 0)
        self.assertNotIsInstance(cm.exception, U.DeviceGone)

    def test_attached_asks_get_speed_and_poll_never_a_reap(self):
        self.assertTrue(self.dev.attached())
        self.assertEqual(self.sys.calls[-2:], [(U.GET_SPEED, 0), ("wait", 7, 0)])
        self.sys.errors = {U.GET_SPEED: errno.ENODEV}           # usbfs: ENODEV once the device is NOTATTACHED
        self.assertFalse(self.dev.attached())
        self.sys.errors = {U.GET_SPEED: errno.ESHUTDOWN}
        self.assertTrue(self.dev.attached())
        self.sys.errors, self.sys.wait = {}, lambda fd, t: U.POLLOUT | U.POLLHUP
        self.assertFalse(self.dev.attached())
        self.dev.close()
        self.assertTrue(self.dev.attached())                    # closed: nothing left to confirm

    def test_close_forgets_slots_and_refuses_ioctls(self):
        slots = self.dev.alloc_slots(2, 4096, 0x81)
        self.dev.claim(1)
        self.dev.submit(slots[0])
        self.assertEqual(self.dev.wait(0.1), U.POLLOUT)
        self.dev.close()
        self.assertEqual((self.sys.closed, slots[0].inflight, self.dev.claimed, self.dev.slots), ([7], False, set(), []))
        n = len(self.sys.calls)
        with self.assertRaises(OSError) as cm:
            self.dev.claim(0)
        self.assertEqual((cm.exception.errno, len(self.sys.calls)), (errno.EBADF, n))
        self.assertEqual(self.dev.wait(0.1), U.POLLNVAL)


class _Refuse:
    """A Sys around the fake kernel whose next ioctls of a request fail with ESHUTDOWN (a stopped host
    controller); every other call goes through."""

    def __init__(self, inner):
        self.inner, self.refuse = inner, collections.Counter()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def ioctl(self, fd, request, arg):
        if self.refuse[request]:
            self.refuse[request] -= 1
            raise OSError(errno.ESHUTDOWN, "refused: host controller stopped")
        return self.inner.ioctl(fd, request, arg)


class Flush(RigCase):
    """usb-2 on FakeKernel: FakeKernel.system_suspend() gives every pending URB back with -ESHUTDOWN while the
    tracker stays attached (Linux usb_suspend_both); an unplug also flushes, but answers ENODEV and HUP."""

    def streaming(self, **knobs):
        se = Session(self, **knobs)
        se.stream(0.5)
        return se

    def test_system_suspend_flush_is_not_a_loss_and_block_c_runs_clean(self):
        se = self.streaming()
        drv, k, t0 = se.drv, se.k, se.clock.monotonic()
        submits = k.submits
        k.system_suspend(30.0)
        se.run(1.0)                                             # reaped, flushed, still attached: no DeviceGone
        self.assertEqual((drv.urbs.flushed, drv.gone, drv.inflight, drv.stats["flushed"]), (True, False, 0, 16))
        self.assertEqual((k.submits, se.clock.boottime() - se.clock.monotonic() >= 30.0), (submits, True))
        speed = [o["t"] for o in k.ops if o["kind"] == "speed" and o["t"] >= t0]
        self.assertAlmostEqual(speed[-1] - speed[0], U.FLUSH_GRACE_S, delta=0.002)    # GET_SPEED during the grace
        self.assertLessEqual(U.FLUSH_GRACE_S, 0.5)
        m = se.mark()
        drv.teardown()
        names = [o["name"] for o in k.ops[m:] if o.get("name") in XU_SETS]
        self.assertEqual((drv.teardown_path, names), ("clean", ["MAGIC", "STREAM_OFF", "MAGIC"] + ["REG_WRITE"] * 11))
        self.assertEqual([o["kind"] + str(o.get("iface", "")) for o in k.ops[m:] if o["kind"] in ("clear_halt",
                                                                                               "release", "close")],
                         ["clear_halt", "release1", "release0", "close"])
        self.assertEqual((k.violations, check_trace(k.ops)), ([], []))

    def test_a_real_unplug_is_still_lost_at_once(self):
        se = self.streaming()
        se.k.unplug()
        t0 = se.clock.monotonic()
        with self.assertRaises(U.DeviceGone):
            se.run(1.0)
        self.assertLess(se.clock.monotonic() - t0, 0.05)       # GET_SPEED answered ENODEV: no grace spent
        self.assertTrue(se.drv.gone)
        se.drv.teardown()
        self.assertEqual((se.drv.teardown_path, se.k.violations, check_trace(se.k.ops)), ("lost", [], []))

    def test_block_c_on_a_flushed_tracker_that_turns_out_gone_ends_at_the_first_enodev(self):
        se = self.streaming()
        se.k.system_suspend()
        se.run(1.0)
        se.rig.vft.on_xu = lambda kind, phase: se.k.unplug()   # it vanishes under Block C's first MAGIC
        m, t0 = se.mark(), se.clock.monotonic()
        se.drv.teardown()
        self.assertEqual(se.drv.teardown_path, "lost")
        self.assertEqual([o["name"] for o in se.k.ops[m:] if o.get("name") in XU_SETS], ["MAGIC"])
        self.assertLess(se.clock.monotonic() - t0, 0.1)        # ENODEV ends it: no ack timeouts at all
        self.assertEqual((se.k.violations, check_trace(se.k.ops)), ([], []))

    def test_eshutdown_errno_is_a_flush_until_enodev_confirms_a_loss(self):
        se = Session(self)
        sys_ = se.drv.dev.sys = _Refuse(se.k)
        sys_.refuse[U.CONTROL] = 1                              # MAGIC #1's SET_CUR: the device is still there
        with self.assertRaises(XuError):
            se.activate()
        se.drv.teardown()
        self.assertEqual((se.drv.teardown_path, se.drv.gone), ("clean", False))
        se = Session(self)
        sys_ = se.drv.dev.sys = _Refuse(se.k)
        se.stream(0.3)
        sys_.refuse[U.SUBMITURB] = 1                            # one resubmission refused: flushed, not lost
        se.run(1.0)
        self.assertEqual((se.drv.urbs.flushed, se.drv.gone, se.drv.inflight), (True, False, 0))
        se.k.unplug()
        sys_.refuse[U.CONTROL] = 1
        with self.assertRaises(U.DeviceGone):                   # ESHUTDOWN, then GET_SPEED's ENODEV
            se.drv.xu(uvc.magic(64), 200)
        se.drv.teardown()
        self.assertEqual((se.drv.teardown_path, se.k.violations), ("lost", []))

    def test_suspend_with_the_tracker_attached_takes_the_clean_path_then_settles(self):
        for tick_first in (False, True):                        # the reaper (pump) sees the flush first, or the tick
            with self.subTest(tick_first=tick_first):
                rig, s = self.make()
                self.assertTrue(s.run(30, lambda s: s.state == "STREAMING" and s.healthy >= 60))
                fd, t_susp = s.driver.dev.fd, rig.clock.monotonic()
                rig.kernel.system_suspend(30.0)                 # every URB back with -ESHUTDOWN, boottime +30 s
                if tick_first:
                    s.tick()
                self.assertTrue(s.run(60, lambda s: len(s.activations) == 2 and s.state == "STREAMING"))
                self.assertEqual((s.events.count("suspend"), s.failures, s.counts["unplugs"]), (1, [], 0))
                self.assertEqual((s.drivers[0].teardown_path, s.store.load_runtime().last_loss_bt), ("clean", None))
                names = [o["name"] for o in rig.kernel.ops if o.get("fd") == fd and o["t"] >= t_susp and
                         o["kind"] == "ctrl" and o["name"] in XU_SETS]
                self.assertEqual(names, ["MAGIC", "STREAM_OFF", "MAGIC"] + ["REG_WRITE"] * 11)   # Block C's XU part
                teardown_t = next(t for st, t in s.states if st == "TEARDOWN")
                self.assertGreaterEqual(s.activations[1]["t"] - teardown_t, 8.0)            # the settle applies
                s.stop()
                self.check(s)


if __name__ == "__main__":
    unittest.main()
