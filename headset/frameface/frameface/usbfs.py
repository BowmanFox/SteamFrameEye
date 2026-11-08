# SPDX-License-Identifier: MIT
"""Raw usbfs through ctypes (spec §4.3, §4.9): the ABI structs, the ioctl numbers, the `Sys` seam,
`UsbDevice` (the only code that issues usbfs ioctls) and `UrbQueue` (the bulk URB pump).

Every ioctl goes through `Sys.ioctl(fd, request, address)`. `LinuxSys` calls libc's ioctl through
ctypes with the address of a ctypes object that stays referenced, because the kernel keeps the
address of a submitted URB and writes the completion into the caller's memory at reap time. The
fcntl module's ioctl copies small buffers into a temporary, so it is never used here. Tests use
`frameface.sim.FakeKernel`, which implements `Sys` on the same ctypes memory.

ioctl numbers are derived from the struct sizes with the generic _IOC layout (aarch64 and x86-64):
dir << 30 | size << 16 | 'U' << 8 | nr, dir NONE 0, WRITE 1, READ 2.
"""
import collections
import ctypes as C
from dataclasses import dataclass
import errno
import os
import sys as _sys
import threading
from typing import Protocol

__all__ = [
    "Ctrl", "SetIf", "Urb", "DisconnectClaim", "GetDriver", "IOCTLS", "CONTROL", "SETINTERFACE",
    "SETCONFIGURATION", "GETDRIVER", "SUBMITURB", "DISCARDURB", "REAPURB", "REAPURBNDELAY", "CLAIMINTERFACE",
    "RELEASEINTERFACE", "RESET", "CLEAR_HALT", "GET_CAPABILITIES", "DISCONNECT_CLAIM", "GET_SPEED", "FIONREAD",
    "URB_TYPE_BULK", "URB_STATUS", "URB_STATUS_NAME", "DISCONNECT_CLAIM_IF_DRIVER", "DISCONNECT_CLAIM_EXCEPT_DRIVER",
    "POLLOUT", "POLLERR", "POLLHUP", "POLLNVAL", "OPEN_FLAGS", "SPEEDS", "CAPS", "FLUSH_GRACE_S", "Sys", "LinuxSys",
    "DeviceGone", "abi_table", "UrbSlot", "UsbDevice", "UrbQueue", "QUEUE_STATS", "errno_name"]


class Ctrl(C.Structure):                # struct usbdevfs_ctrltransfer, sizeof 24
    _fields_ = [("bRequestType", C.c_uint8), ("bRequest", C.c_uint8), ("wValue", C.c_uint16),
                ("wIndex", C.c_uint16), ("wLength", C.c_uint16), ("timeout", C.c_uint32),     # ms
                ("data", C.c_void_p)]


class SetIf(C.Structure):               # struct usbdevfs_setinterface, sizeof 8
    _fields_ = [("interface", C.c_uint32), ("altsetting", C.c_uint32)]


class Urb(C.Structure):                 # struct usbdevfs_urb without iso descriptors, sizeof 56
    _fields_ = [("type", C.c_uint8), ("endpoint", C.c_uint8), ("status", C.c_int32), ("flags", C.c_uint32),
                ("buffer", C.c_void_p), ("buffer_length", C.c_int32), ("actual_length", C.c_int32),
                ("start_frame", C.c_int32), ("number_of_packets", C.c_int32),   # union with stream_id
                ("error_count", C.c_int32), ("signr", C.c_uint32), ("usercontext", C.c_void_p)]


class DisconnectClaim(C.Structure):     # struct usbdevfs_disconnect_claim, sizeof 264
    _fields_ = [("interface", C.c_uint32), ("flags", C.c_uint32), ("driver", C.c_char * 256)]


class GetDriver(C.Structure):           # struct usbdevfs_getdriver, sizeof 260
    _fields_ = [("interface", C.c_uint32), ("driver", C.c_char * 256)]


_NONE, _WRITE, _READ = 0, 1, 2
_UINT = C.sizeof(C.c_uint)
_PTR = C.sizeof(C.c_void_p)


def _ioc(direction, nr, size=0):
    return direction << 30 | size << 16 | ord("U") << 8 | nr


CONTROL = _ioc(_READ | _WRITE, 0, C.sizeof(Ctrl))
SETINTERFACE = _ioc(_READ, 4, C.sizeof(SetIf))
SETCONFIGURATION = _ioc(_READ, 5, _UINT)
GETDRIVER = _ioc(_WRITE, 8, C.sizeof(GetDriver))
SUBMITURB = _ioc(_READ, 10, C.sizeof(Urb))
DISCARDURB = _ioc(_NONE, 11)
REAPURB = _ioc(_WRITE, 12, _PTR)                 # blocking reap: never used
REAPURBNDELAY = _ioc(_WRITE, 13, _PTR)
CLAIMINTERFACE = _ioc(_READ, 15, _UINT)
RELEASEINTERFACE = _ioc(_READ, 16, _UINT)
RESET = _ioc(_NONE, 20)
CLEAR_HALT = _ioc(_READ, 21, _UINT)
GET_CAPABILITIES = _ioc(_READ, 26, C.sizeof(C.c_uint32))
DISCONNECT_CLAIM = _ioc(_READ, 27, C.sizeof(DisconnectClaim))
GET_SPEED = _ioc(_NONE, 31)
IOCTLS = {name: globals()[name] for name in (
    "CONTROL SETINTERFACE SETCONFIGURATION GETDRIVER SUBMITURB DISCARDURB REAPURB REAPURBNDELAY CLAIMINTERFACE "
    "RELEASEINTERFACE RESET CLEAR_HALT GET_CAPABILITIES DISCONNECT_CLAIM GET_SPEED").split()}

FIONREAD = 0x541B                       # asm-generic (aarch64, x86-64); only the FIONREAD self-test uses it
URB_TYPE_BULK = 3
# urb.status is a negative *Linux* errno whatever the host (Windows' errno module differs, e.g. EPROTO)
URB_STATUS = {"EPIPE": -32, "EPROTO": -71, "EILSEQ": -84, "ETIME": -62, "EOVERFLOW": -75, "ENOENT": -2,
              "ECONNRESET": -104, "ENODEV": -19, "ESHUTDOWN": -108}
URB_STATUS_NAME = {v: k for k, v in URB_STATUS.items()}
DISCONNECT_CLAIM_IF_DRIVER, DISCONNECT_CLAIM_EXCEPT_DRIVER = 0x01, 0x02
POLLOUT, POLLERR, POLLHUP, POLLNVAL = 0x004, 0x008, 0x010, 0x020
OPEN_FLAGS = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
SPEEDS = {1: 1, 2: 12, 3: 480, 4: 480, 5: 5000, 6: 10000}      # USBDEVFS_GET_SPEED -> Mbit/s (4 = wireless)
CAPS = {"ZERO_PACKET": 0x01, "BULK_CONTINUATION": 0x02, "NO_PACKET_SIZE_LIM": 0x04, "BULK_SCATTER_GATHER": 0x08,
        "REAP_AFTER_DISCONNECT": 0x10, "MMAP": 0x20, "DROP_PRIVILEGES": 0x40, "CONNINFO_EX": 0x80, "SUSPEND": 0x100}
FLUSH_GRACE_S = 0.5                     # after an ESHUTDOWN flush: time for the hub driver to process a disconnect


def errno_name(e) -> str:
    """'EPIPE' for 32 (or for an OSError carrying it); 'E<n>' for unknown numbers."""
    if isinstance(e, BaseException):
        e = getattr(e, "errno", None)
    return errno.errorcode.get(e, "E%s" % e) if e is not None else "E?"


class Sys(Protocol):
    def open(self, path: str, flags: int) -> int: ...
    def close(self, fd: int) -> None: ...
    def ioctl(self, fd: int, request: int, arg: int) -> int: ...    # arg: address or 0; >= 0 or OSError
    def wait(self, fd: int, timeout_s: float) -> int: ...           # poll mask (POLLOUT|POLLERR|POLLHUP), 0 on timeout


class LinuxSys:
    """The real kernel: os.open/os.close, libc ioctl through ctypes (argtypes c_int, c_ulong,
    c_void_p; errno captured) and select.poll for completions. Linux only."""

    def __init__(self):
        if not _sys.platform.startswith("linux"):
            raise OSError(errno.ENOSYS, "LinuxSys needs Linux usbfs")
        import select
        self._poll = select.poll
        self._pollout = select.POLLOUT
        libc = C.CDLL(None, use_errno=True)
        self._ioctl = libc.ioctl
        self._ioctl.argtypes = (C.c_int, C.c_ulong, C.c_void_p)
        self._ioctl.restype = C.c_int

    def open(self, path, flags):
        return os.open(path, flags)

    def close(self, fd):
        os.close(fd)

    def ioctl(self, fd, request, arg):
        r = self._ioctl(fd, request, arg or None)
        if r < 0:
            e = C.get_errno()
            raise OSError(e, os.strerror(e))
        return r

    def wait(self, fd, timeout_s):
        p = self._poll()
        p.register(fd, self._pollout)       # POLLERR and POLLHUP are always reported
        return next((ev for _, ev in p.poll(max(0, int(timeout_s * 1000 + 0.5)))), 0)     # one fd: one entry


class DeviceGone(OSError):
    """The device was unplugged or reset (ENODEV, POLLHUP/POLLERR): take the LOST path. ESHUTDOWN alone
    is not this: it only says the URBs were flushed (system suspend, stopped host controller)."""


def abi_table() -> dict:
    """The usbfs ABI as seen by these ctypes structs, in the JSON format of Appendix A.6 (the
    selftest compares it key by key with the output of csrc/abi_dump.c)."""
    structs = (("ctrltransfer", Ctrl), ("setinterface", SetIf), ("urb", Urb),
               ("disconnect_claim", DisconnectClaim), ("getdriver", GetDriver))
    offsets = {"%s.%s" % (name, field): getattr(cls, field).offset
               for name, cls in structs for field, _ in cls._fields_}
    return {"sizes": {name: C.sizeof(cls) for name, cls in structs},
            "offsets": offsets,
            "ioctl": dict(IOCTLS),
            "consts": {"URB_TYPE_BULK": URB_TYPE_BULK, "DISCONNECT_CLAIM_IF_DRIVER": DISCONNECT_CLAIM_IF_DRIVER,
                       "DISCONNECT_CLAIM_EXCEPT_DRIVER": DISCONNECT_CLAIM_EXCEPT_DRIVER, "FIONREAD": FIONREAD}}


@dataclass
class UrbSlot:
    index: int
    size: int
    buf: "C.Array"
    urb: Urb
    inflight: bool = False
    interface: int = 1                  # the interface of the endpoint (RELEASE/SETINTERFACE destroy its URBs)


class UsbDevice:
    """One usbfs file descriptor of the tracker. Every buffer handed to the kernel is a ctypes
    object owned here: URB slots stay referenced until the fd is closed."""

    def __init__(self, sys: Sys, path: str):
        self.sys, self.path, self.fd, self.claimed = sys, path, None, set()
        self.slots = []                 # every slot of this fd (kept until close)
        self._by_addr, self._next_index = {}, 0

    # -- file descriptor

    def open(self) -> None:
        if self.fd is not None:
            raise OSError(errno.EBUSY, "usbfs node already open")
        self.fd = self.sys.open(self.path, OPEN_FLAGS)

    def close(self) -> None:
        """Close the fd; the kernel destroys URBs that were not reaped, so the slots are released."""
        fd, self.fd = self.fd, None
        self.claimed.clear()
        for slot in self.slots:
            slot.inflight = False
        self.slots, self._by_addr = [], {}
        if fd is not None:
            self.sys.close(fd)

    def _ioctl(self, request, arg=0):
        """ENODEV raises DeviceGone; ESHUTDOWN stays a plain OSError (flushed, not proof of a loss)."""
        if self.fd is None:
            raise OSError(errno.EBADF, "usbfs node not open")
        try:
            return self.sys.ioctl(self.fd, request, arg)
        except DeviceGone:
            raise
        except OSError as e:
            if e.errno == errno.ENODEV:
                raise DeviceGone(e.errno, "device gone (%s)" % errno_name(e.errno)) from None
            raise

    def attached(self) -> bool:
        """False once a real loss shows: GET_SPEED (no reap) answers ENODEV or poll reports HUP/ERR (usbfs
        does both as soon as the device is NOTATTACHED); True otherwise, also while the fd is closed."""
        try:
            self.speed()
        except DeviceGone:
            return False
        except OSError:
            pass
        return self.fd is None or not self.wait(0) & (POLLHUP | POLLERR)

    @staticmethod
    def _uint_arg(value):
        v = C.c_uint(value)
        return v, C.addressof(v)

    # -- interfaces and configuration

    def claim(self, i: int) -> None:
        v, addr = self._uint_arg(i)
        self._ioctl(CLAIMINTERFACE, addr)
        self.claimed.add(i)

    def release(self, i: int) -> None:
        v, addr = self._uint_arg(i)
        self._ioctl(RELEASEINTERFACE, addr)
        self.claimed.discard(i)
        self._forget(i)

    def set_configuration(self, value: int) -> None:
        """USBDEVFS_SETCONFIGURATION; the kernel answers EBUSY while any interface is claimed."""
        v, addr = self._uint_arg(value)
        self._ioctl(SETCONFIGURATION, addr)

    def get_driver(self, i: int) -> "str | None":
        gd = GetDriver(interface=i)
        try:
            self._ioctl(GETDRIVER, C.addressof(gd))
        except OSError as e:
            if e.errno == errno.ENODATA:
                return None
            raise
        return gd.driver.decode("ascii", "replace")

    def disconnect_claim(self, i: int) -> None:
        """Take interface i from a kernel driver, never from another usbfs owner: always flags
        EXCEPT_DRIVER (0x02) with driver "usbfs" (EBUSY if usbfs holds it)."""
        dc = DisconnectClaim(interface=i, flags=DISCONNECT_CLAIM_EXCEPT_DRIVER, driver=b"usbfs")
        self._ioctl(DISCONNECT_CLAIM, C.addressof(dc))
        self.claimed.add(i)

    def set_interface(self, i: int, alt: int) -> None:
        s = SetIf(interface=i, altsetting=alt)
        self._ioctl(SETINTERFACE, C.addressof(s))
        self._forget(i)

    def clear_halt(self, ep: int) -> None:
        v, addr = self._uint_arg(ep)
        self._ioctl(CLEAR_HALT, addr)

    def reset(self) -> None:
        self._ioctl(RESET, 0)

    def speed(self) -> int:
        return self._ioctl(GET_SPEED, 0)

    def capabilities(self) -> int:
        v = C.c_uint32(0)
        self._ioctl(GET_CAPABILITIES, C.addressof(v))
        return v.value

    # -- control transfers

    def control_in(self, s, timeout_ms: int = 1000) -> bytes:
        buf = (C.c_ubyte * max(1, s.length))()
        ctrl = Ctrl(s.request_type, s.request, s.value, s.index, s.length, timeout_ms,
                    C.addressof(buf) if s.length else None)
        n = self._ioctl(CONTROL, C.addressof(ctrl))
        return bytes(buf[:max(0, min(n, s.length))])

    def control_out(self, s, data: bytes, timeout_ms: int = 1000) -> int:
        data = bytes(data)
        if len(data) != s.length:
            raise ValueError("control OUT data is %d bytes, wLength %d" % (len(data), s.length))
        buf = (C.c_ubyte * max(1, len(data))).from_buffer_copy(data or b"\0")
        ctrl = Ctrl(s.request_type, s.request, s.value, s.index, s.length, timeout_ms,
                    C.addressof(buf) if s.length else None)
        return self._ioctl(CONTROL, C.addressof(ctrl))

    # -- URBs

    def alloc_slots(self, n: int, size: int, ep: int, interface: int = 1) -> list:
        """n bulk URB slots of `size` bytes for endpoint `ep`; usercontext = the slot index."""
        out = []
        for _ in range(n):
            index = self._next_index
            self._next_index += 1
            buf = (C.c_ubyte * size)()
            urb = Urb(type=URB_TYPE_BULK, endpoint=ep, flags=0, buffer=C.addressof(buf), buffer_length=size,
                      usercontext=index)
            slot = UrbSlot(index, size, buf, urb, False, interface)
            self.slots.append(slot)
            self._by_addr[C.addressof(urb)] = slot
            out.append(slot)
        return out

    def submit(self, slot: UrbSlot) -> None:
        u = slot.urb
        u.status = u.actual_length = u.error_count = 0
        self._ioctl(SUBMITURB, C.addressof(u))
        slot.inflight = True

    def discard(self, slot: UrbSlot) -> None:
        """DISCARDURB; EINVAL (the URB already completed) is swallowed."""
        try:
            self._ioctl(DISCARDURB, C.addressof(slot.urb))
        except OSError as e:
            if e.errno != errno.EINVAL:
                raise

    def reap(self) -> "UrbSlot | None":
        """REAPURBNDELAY: the completed slot, None on EAGAIN; ENODEV raises DeviceGone."""
        p = C.c_void_p()
        try:
            self._ioctl(REAPURBNDELAY, C.addressof(p))
        except OSError as e:
            if e.errno == errno.EAGAIN:
                return None
            raise
        slot = self._by_addr.get(p.value)
        if slot is None:
            raise OSError(errno.EFAULT, "reaped an unknown URB address 0x%x" % (p.value or 0))
        slot.inflight = False
        return slot

    def wait(self, timeout_s: float) -> int:
        if self.fd is None:
            return POLLNVAL
        return self.sys.wait(self.fd, timeout_s)

    def _forget(self, interface):
        """RELEASEINTERFACE and SETINTERFACE destroy this fd's URBs on that interface."""
        for slot in self.slots:
            if slot.interface == interface:
                slot.inflight = False


_XFER = {URB_STATUS["EPROTO"]: "eproto", URB_STATUS["EILSEQ"]: "eilseq", URB_STATUS["ETIME"]: "etime",
         URB_STATUS["EOVERFLOW"]: "eoverflow"}
QUEUE_STATS = ("urbs", "urb_bytes", "inflight_min", "reap_batch_max", "urbs_ok", "bytes", "zlp", "epipe", "eproto",
               "eilseq", "etime", "eoverflow", "other", "discarded", "flushed", "submit_errors", "wakes",
               "starved_wakes")


class UrbQueue:
    """The bulk URB queue of one stream (spec §4.9). start() submits n URBs at once (EINVAL/ENOMEM:
    discard and reap what was submitted, halve the size down to 4096, retry); pump() waits, drains
    REAPURBNDELAY, applies the status table and resubmits unless `stopping` or `flushed`; threaded mode
    runs pump(0.1) in a reaper thread. `stats`, `inflight` and the deques epipe_times / xfer_err_times /
    starved_times (monotonic times) feed the service's watchdogs; `first_data_t` is the first URB with bytes.

    status 0: sink.feed(data, n, urb_len) (ZLP included); -EPIPE: recover(ep) (the owner's CLEAR_HALT,
    vft._recover), resubmit unless it kept the halt; -EPROTO, -EILSEQ, -ETIME, -EOVERFLOW: sink.asm.error(),
    resubmit; -ENOENT, -ECONNRESET: discarded, no resubmit; -ENODEV: DeviceGone; -ESHUTDOWN or a SUBMITURB
    ESHUTDOWN: `flushed` (system suspend, stopped host controller, unplug in progress): no resubmission from
    then on, confirm_gone() decides; any other: sink.asm.error(), resubmit."""

    def __init__(self, dev, clock, log, recover, inline=False):
        self.dev, self.clock, self.log, self.recover, self.inline = dev, clock, log, recover, inline
        self.slots, self.sink, self.size, self.ep, self.interface = [], None, 0, 0x81, 1
        self.inflight, self.stopping, self.gone, self.flushed, self._flush_checked = 0, False, False, False, False
        self.stats = dict.fromkeys(QUEUE_STATS, 0)
        self.stats["inflight_min"] = None
        self.epipe_times, self.xfer_err_times, self.starved_times = (collections.deque(maxlen=m) for m in (64, 512, 64))
        self.first_data_t = self.reaper = self.reaper_error = self.reaper_beat = self.on_reaper_start = None
        self.reaper_stop = threading.Event()
        self._lock = threading.Lock()

    def _add(self, n):
        with self._lock:
            self.inflight += n

    def start(self, n, size, ep, interface, sink):
        self.sink, self.ep, self.interface, self.stopping = sink, ep, interface, False
        while True:
            slots = self.dev.alloc_slots(n, size, ep, interface)
            self.slots.extend(slots)                    # kept referenced until the fd closes
            failed = False
            for slot in slots:
                try:
                    self.dev.submit(slot)
                except OSError as e:
                    if e.errno == errno.ESHUTDOWN and not isinstance(e, DeviceGone):
                        self.flushed = True             # host controller not running: the next pump() decides
                        break
                    if isinstance(e, DeviceGone) or e.errno not in (errno.EINVAL, errno.ENOMEM) or size <= 4096:
                        raise
                    failed = True
                    break
                self._add(1)
            if not failed:
                break
            self.stopping = True
            self.discard_all()
            self.drain(1.0)
            self.stopping = False
            size = max(4096, (size // 2) // 512 * 512)
            self.log.w("usb", "urb size reduced to=%d" % size)
        self.size = size
        self.stats.update(urbs=n, urb_bytes=size, inflight_min=self.inflight)
        if not self.inline:
            self.reaper_stop.clear()
            self.reaper = threading.Thread(target=self._reaper_main, name="reaper", daemon=True)
            self.reaper.start()

    def add(self, n):
        """n more URBs of the current size (starvation growth); returns how many were submitted."""
        if self.dev.fd is None or self.stopping or self.flushed or not self.size:
            return 0
        added = 0
        for slot in self.dev.alloc_slots(n, self.size, self.ep, self.interface):
            self.slots.append(slot)
            try:
                self.dev.submit(slot)
            except DeviceGone:
                raise
            except OSError as e:
                self.flushed = self.flushed or e.errno == errno.ESHUTDOWN
                self.stats["submit_errors"] += 1
                self.log.w("usb", "add urbs submit failed errno=%s" % errno_name(e))
                break
            self._add(1)
            added += 1
        self.stats["urbs"] += added
        return added

    def pump(self, timeout_s):
        if self.dev.fd is None or not self.slots:
            return 0
        mask = self.dev.wait(timeout_s)
        before, batch = self.inflight, 0
        try:
            while True:
                slot = self.dev.reap()
                if slot is None:
                    break
                batch += 1
                self._add(-1)
                self._complete(slot)
            if self.flushed and not self._flush_checked and not self.stopping:
                self._flush_checked = True
                if self.confirm_gone():
                    raise DeviceGone(errno.ENODEV, "URBs flushed (ESHUTDOWN) and the device is gone")
        except DeviceGone:
            self.gone = True
            raise
        st = self.stats
        if batch:
            left = before - batch
            st["wakes"] += 1
            st["inflight_min"] = left if st["inflight_min"] is None else min(st["inflight_min"], left)
            st["reap_batch_max"] = max(st["reap_batch_max"], batch)
            if not self.stopping and not self.flushed and (left <= 2 or batch >= 12):
                st["starved_wakes"] += 1
                self.starved_times.append(self.clock.monotonic())
        elif mask & (POLLHUP | POLLERR):
            self.gone = True
            raise DeviceGone(errno.ENODEV, "usbfs node hung up")
        return batch

    def _complete(self, slot):
        st, n, stats = slot.urb.status, slot.urb.actual_length, self.stats
        if st == 0:
            stats["urbs_ok"] += 1
            stats["bytes"] += n
            if n == 0:
                stats["zlp"] += 1
            elif self.first_data_t is None:
                self.first_data_t = self.clock.monotonic()
            if self.sink is not None:
                self.sink.feed(memoryview(slot.buf)[:n], n, slot.size)
        elif st == URB_STATUS["EPIPE"]:
            stats["epipe"] += 1
            self.epipe_times.append(self.clock.monotonic())
            if not self.stopping and not self.recover(self.ep):
                return                                  # the halt is kept: a resubmitted URB would only stall again
        elif st in _XFER or st not in URB_STATUS_NAME:
            stats[_XFER.get(st, "other")] += 1
            if st in _XFER:
                self.xfer_err_times.append(self.clock.monotonic())
            asm = getattr(self.sink, "asm", None)
            if asm is not None:
                asm.error()
        elif st in (URB_STATUS["ENOENT"], URB_STATUS["ECONNRESET"]):
            stats["discarded"] += 1
            return
        elif st == URB_STATUS["ESHUTDOWN"]:             # flushed: the device may well still be there
            stats["flushed"] += 1
            self.flushed = True
            return
        else:                                           # ENODEV
            if self.stopping:
                return
            self.gone = True
            raise DeviceGone(errno.ENODEV, "URB status %d" % st)
        if not self.stopping and not self.flushed:
            try:
                self.dev.submit(slot)
            except DeviceGone:
                raise
            except OSError as e:
                self.flushed = self.flushed or e.errno == errno.ESHUTDOWN
                stats["submit_errors"] += 1
                self.log.w("usb", "resubmit failed errno=%s" % errno_name(e))
                return
            self._add(1)

    def confirm_gone(self, grace_s=FLUSH_GRACE_S) -> bool:
        """After a flush (ESHUTDOWN): True (and `gone`) only if a real loss shows within grace_s (attached());
        False if the device stays attached or a teardown begins (`stopping`, reaper stop) meanwhile."""
        end = self.clock.monotonic() + grace_s
        while self.dev.attached():
            rest = end - self.clock.monotonic()
            if rest <= 0 or self.stopping or self.reaper_stop.is_set() or self.dev.fd is None:
                self.log.w("usb", "urbs flushed (ESHUTDOWN); device still attached")
                return False
            self.clock.sleep(min(0.1, rest))
        self.gone = True
        return True

    def _reaper_main(self):
        if self.on_reaper_start is not None:
            try:
                self.on_reaper_start()
            except Exception as e:                      # affinity and nice are best effort
                self.log.w("usb", "reaper start hook failed", error=repr(e))
        while not self.reaper_stop.is_set():
            self.reaper_beat = self.clock.monotonic()
            try:
                self.pump(0.1)
            except Exception as e:
                if not self.reaper_stop.is_set() or isinstance(e, DeviceGone):
                    self.reaper_error = e
                return

    def discard_all(self):
        for slot in list(self.slots):
            if slot.inflight:
                try:
                    self.dev.discard(slot)
                except OSError:
                    pass

    def drain(self, limit_s):
        """Reap until nothing is in flight, at most limit_s (waits on the reaper thread while it runs)."""
        end = self.clock.monotonic() + limit_s
        r = self.reaper
        while r is not None and r.is_alive() and self.inflight > 0 and self.clock.monotonic() < end:
            self.clock.sleep(0.005)
        while self.inflight > 0 and self.clock.monotonic() < end:
            try:
                self.pump(min(0.05, max(0.0, end - self.clock.monotonic())))
            except OSError:
                break
        if self.inflight > 0:
            self.log.w("usb", "teardown left urbs in flight", inflight=self.inflight)

    def stop_reaper(self):
        r, self.reaper = self.reaper, None
        if r is not None:
            self.reaper_stop.set()
            if r is not threading.current_thread():
                r.join(0.5)

    def forget(self):
        """The fd closed: the kernel destroyed whatever was left."""
        with self._lock:
            self.inflight = 0
        self.slots = []
