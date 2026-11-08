# SPDX-License-Identifier: MIT
"""FakeKernel (spec §7.3): the usbfs `Sys` on the driver's real ctypes memory (make_rig() in sim/__init__).

ioctl() decodes the request number and reads/writes the caller's ctypes structs by address: a
CONTROL reads the Ctrl and its data buffer and writes IN data back with memmove; SUBMITURB queues
the URB address; REAPURBNDELAY copies the data into urb.buffer, writes status/actual_length and
stores the URB address into the caller's void* (processcompl semantics, at reap time). Each URB
completes in exactly one event (SimClock call_at, or a scheduler thread on the real clock).

Every operation is appended to `ops` (dicts with t, kind, fd and decoded fields; an error by its Linux name, _ename);
rule breaks of the driver are appended to `violations` (strings "<kind>: <detail>"), which every test asserts to be
empty: unclaimed_interface_use, steal_from_usbfs_owner, setinterface_with_urbs_inflight, setconfig_after_claim,
reset_while_streaming, reset_behind_hub, ioctl_after_close, xu_after_enodev, ctrl_without_timeout. unplug() and
system_suspend() both give pending URBs back with -ESHUTDOWN; only an unplug makes ioctls answer ENODEV and polls
report HUP|ERR (a -ESHUTDOWN reap alone does not count as an ENODEV). silence_ep0() makes the tracker's control
endpoint stop answering (every EP0 request ends at its timeout).
"""
import collections
import ctypes as C
import errno
import itertools
import os
import threading

from .. import usbfs as U
from .fakevft import FakeVft
from .replay import ctrl_name
from .simclock import RealScheduler

__all__ = ["FakeKernel", "KERNEL_CTRL_TIMEOUT_S"]

KERNEL_CTRL_TIMEOUT_S = 5.0             # USB_CTRL_SET_TIMEOUT: the wait of CLEAR_HALT, SETINTERFACE, SETCONFIGURATION


def _ename(code):
    """An errno's name as check_trace reads it: the Linux one (Windows names 10060 WSAETIMEDOUT, 10058 WSAESHUTDOWN)."""
    return U.errno_name(code).replace("WSA", "", 1)


class _Rec:
    __slots__ = ("addr", "urb", "length", "index")

    def __init__(self, addr, urb, length, index):
        self.addr, self.urb, self.length, self.index = addr, urb, length, index


class _Fd:
    def __init__(self, fd, writable):
        self.fd, self.writable = fd, writable
        self.closed = self.gone = self.enodev_seen = self.any_claim = self.had_xu = self.submitted = False
        self.claimed = set()
        self.pending = collections.deque()
        self.completed = collections.deque()
        self.event = None
        self.mem = 0


class FakeKernel:
    """The kernel side of one fake tracker (and its sysfs tree, if `tree` is given)."""

    def __init__(self, vft: FakeVft, clock, tree=None):
        self.vft, self.clock, self.tree = vft, clock, tree
        self.sim = hasattr(clock, "call_at")
        self.ops, self.violations = [], []
        self.completion_events = self.submits = 0
        self.ctrl_cost_s = 0.0001 if self.sim else 0.0      # one control transfer: about 3 microframes
        self.present, self.busnum, self.devnum = True, 1, 3
        self.sysname = vft.m.sysname
        self.kernel_driver = {0: vft.m.kernel_driver, 1: vft.m.kernel_driver} if vft.m.kernel_driver else {}
        self.other_owner = vft.m.other_usbfs_owner              # holds interface 1
        self._fds, self._fd_seq = {}, itertools.count(100)
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        if not self.sim:
            vft.warm()                                  # no first render inside a real-time completion
        self._sched = None if self.sim else RealScheduler(clock, self._cond)
        self._gen, self._mem = vft.gen, 0
        self._forced = collections.deque()              # force_status(): statuses of the next completions
        self._mute_at = self._mute_left = None          # silence_ep0(): from when, and how many requests (None: all)
        self._handlers = {U.CONTROL: self._control, U.SETINTERFACE: self._setinterface,
                          U.SETCONFIGURATION: self._setconfig, U.GETDRIVER: self._getdriver,
                          U.SUBMITURB: self._submit, U.DISCARDURB: self._discard, U.REAPURB: self._reap_blocking,
                          U.REAPURBNDELAY: self._reap, U.CLAIMINTERFACE: self._claim,
                          U.RELEASEINTERFACE: self._release, U.RESET: self._reset, U.CLEAR_HALT: self._clear_halt,
                          U.GET_CAPABILITIES: self._caps, U.DISCONNECT_CLAIM: self._disconnect_claim,
                          U.GET_SPEED: self._speed}
        if tree is not None:
            tree.write_static(self)
            tree.write_device(self)
        m = vft.m
        if m.unplug_at_s is not None:
            self._call_at(vft.t0 + m.unplug_at_s, lambda: self.unplug(m.replug_after_s))

    @property
    def devpath(self):
        if self.tree is not None:
            return self.tree.devpath(self.busnum, self.devnum)
        return "/dev/bus/usb/%03d/%03d" % (self.busnum, self.devnum)

    # -- bookkeeping

    def _rec(self, kind, fd, **kv):
        op = {"t": self.clock.monotonic(), "kind": kind, "fd": fd}
        op.update(kv)
        self.ops.append(op)
        return op

    def _violation(self, kind, detail=""):
        self.violations.append(kind + (": " + detail if detail else ""))

    def note(self, **kv) -> None:
        """Append a test annotation to the op trace (e.g. the strategy and XU length of an activation)."""
        with self._lock:
            self._rec("note", None, **kv)

    def force_status(self, status: int, count: int = 1, halt: bool = False) -> None:
        """Test hook: the next `count` URB completions carry `status` (a negative Linux errno) and no data; `halt` (a
        STALL, -32) also halts the bulk endpoint, as hardware does: no data until CLEAR_HALT or SETINTERFACE."""
        with self._lock:
            self._forced.extend([(int(status), halt)] * count)

    def silence_ep0(self, at=None, count=None) -> None:
        """Test hook: the tracker's control endpoint (EP0) stops answering at monotonic time `at` (now if None): every
        CONTROL ioctl then waits for its Ctrl timeout (ms) and fails with ETIMEDOUT, CLEAR_HALT, SETINTERFACE and
        SETCONFIGURATION wait the kernel's KERNEL_CTRL_TIMEOUT_S and fail the same way (op trace: err="ETIMEDOUT");
        claim, release, submit, discard, reap and close are host-side and unaffected; a CONTROL with timeout 0 (usbfs:
        it waits for ever) is the violation ctrl_without_timeout. `count`: only that many EP0 requests go unanswered.
        SimClock only: the fake holds its one lock across an ioctl, so on the real clock a waiting CONTROL would also
        stall the reaper's reaps, which usbfs does not (it drops the device lock around a control transfer)."""
        if not self.sim:
            raise ValueError("silence_ep0 needs a SimClock")
        with self._lock:
            self._mute_at, self._mute_left = self.clock.monotonic() if at is None else float(at), count

    def _ep0_wait(self, st, op, seconds):
        """silence_ep0: an EP0 request nobody answers takes `seconds` (its timeout), then fails with ETIMEDOUT (ENODEV
        if the tracker went away meanwhile). Returns at once while EP0 answers."""
        if self._mute_at is None or self.clock.monotonic() < self._mute_at or self._mute_left == 0:
            return
        if seconds <= 0:
            self._violation("ctrl_without_timeout", "%s to a silent EP0 would wait for ever" % op.get("name"))
        self._mute_left = None if self._mute_left is None else self._mute_left - 1
        self.clock.advance(seconds)
        st.enodev_seen = st.enodev_seen or st.gone
        op["err"] = "ENODEV" if st.gone else "ETIMEDOUT"
        raise OSError(errno.ENODEV if st.gone else errno.ETIMEDOUT, "EP0 did not answer (silence_ep0)")

    def inflight(self) -> int:
        """URBs submitted and not yet reaped, over every open fd."""
        return sum(len(s.pending) + len(s.completed) for s in self._fds.values() if not s.closed)

    def _call_at(self, t, fn):
        return self.clock.call_at(t, fn) if self.sim else self._sched.call_at(t, fn)

    def shutdown(self) -> None:
        if self._sched is not None:
            self._sched.stop()

    # -- Sys

    def open(self, path, flags):
        with self._lock:
            if not self.present or os.path.normcase(os.path.normpath(path)) != \
                    os.path.normcase(os.path.normpath(self.devpath)):
                self._rec("open", None, path=path, err="ENOENT")
                raise FileNotFoundError(errno.ENOENT, "no such device node", path)
            write = bool(flags & (os.O_WRONLY | os.O_RDWR))
            mode = self.vft.m.node_mode
            denied = write and (mode == "ro" or (mode.startswith("ro_for:") and
                                                 self.clock.monotonic() - self.vft.enum_t < float(mode[7:])))
            if denied:
                self._rec("open", None, path=path, write=write, err="EACCES")
                raise PermissionError(errno.EACCES, "permission denied", path)
            fd = next(self._fd_seq)
            self._fds[fd] = _Fd(fd, write)
            self.vft.on_open()
            self._rec("open", fd, path=path, write=write, sysname=self.sysname)
            return fd

    def close(self, fd):
        with self._lock:
            st = self._fds.get(fd)
            if st is None or st.closed:
                self._violation("ioctl_after_close", "close of fd %s" % fd)
                raise OSError(errno.EBADF, "bad file descriptor")
            st.closed = True
            inflight, unreaped = len(st.pending), len(st.completed)
            self._cancel(st)
            self._mem -= st.mem
            st.pending.clear()
            st.completed.clear()
            st.claimed.clear()
            if not st.gone and (st.had_xu or st.submitted):
                self.vft.on_close(st.had_xu)
            self._rec("close", fd, inflight=inflight, unreaped=unreaped)
            self._resync()
            self._cond.notify_all()

    def ioctl(self, fd, request, arg):
        with self._lock:
            st = self._fds.get(fd)
            if st is None or st.closed:
                self._violation("ioctl_after_close", "fd %s request 0x%x" % (fd, request))
                raise OSError(errno.EBADF, "bad file descriptor")
            if not st.writable:
                self._rec("ioctl", fd, request=request, err="EPERM")
                raise PermissionError(errno.EPERM, "usbfs node opened read-only")
            handler = self._handlers.get(request)
            if handler is None:
                raise OSError(errno.ENOTTY, "unknown usbfs ioctl 0x%x" % request)
            if st.gone and request != U.REAPURBNDELAY:
                if request == U.CONTROL and st.enodev_seen and self._is_xu(arg):
                    self._violation("xu_after_enodev", "fd %d" % fd)
                st.enodev_seen = True
                self._rec("enodev", fd, request=request)
                raise OSError(errno.ENODEV, "no such device")
            try:
                return handler(st, arg)
            finally:
                self._resync()

    def wait(self, fd, timeout_s):
        st = self._fds.get(fd)
        if st is None or st.closed:
            return U.POLLNVAL
        if self.sim:
            self.clock.advance(0)
            deadline = self.clock.monotonic() + max(0.0, timeout_s)
            while True:
                mask = self._mask(st)
                now = self.clock.monotonic()
                if mask or now >= deadline:
                    return mask
                t_next = self.clock.next_event()
                self.clock.advance((deadline if t_next is None or t_next > deadline else t_next) - now)
        with self._cond:
            end = self.clock.monotonic() + max(0.0, timeout_s)
            while not self._mask(st):
                rest = end - self.clock.monotonic()
                if rest <= 0:
                    break
                self._cond.wait(rest)
            return self._mask(st)

    @staticmethod
    def _mask(st):
        return (U.POLLOUT if st.completed else 0) | (U.POLLHUP | U.POLLERR if st.gone else 0)

    # -- control transfers

    def _is_xu(self, arg):
        c = U.Ctrl.from_address(arg)
        return (c.wIndex & 0xFF) == 0 and (c.wIndex >> 8) != 0 and c.bRequestType & 0x60 == 0x20

    def _control(self, st, arg):
        c = U.Ctrl.from_address(arg)
        rt, rq, wv, wi, wl = c.bRequestType, c.bRequest, c.wValue, c.wIndex, c.wLength
        out = C.string_at(c.data, wl) if not rt & 0x80 and wl and c.data else None
        name = ctrl_name(rt, rq, wv, wi, out)
        op = self._rec("ctrl", st.fd, name=name, rt=rt, rq=rq, wv=wv, wi=wi, wl=wl, out=out.hex() if out else None)
        if (wi & 0xFF) == 0 and (wi >> 8) and rt & 0x60 == 0x20:
            st.had_xu = st.had_xu or rq == 0x01             # an XU command: this session (de)activated
            if rq in (0x01, 0x81) and wl != self.vft.get_len:
                op["wrong_xu_length"] = True
        if rt & 0x1F == 1 and (wi & 0xFF) not in st.claimed:
            self._violation("unclaimed_interface_use", "%s on interface %d" % (name, wi & 0xFF))
        if name == "REG_WRITE":
            op["reg"], op["val"] = out[8], out[16]
        self._ep0_wait(st, op, c.timeout / 1000.0)      # silence_ep0: ends at the transfer's own timeout
        if self.ctrl_cost_s:
            self.clock.advance(self.ctrl_cost_s)
        if st.gone:
            op["err"] = "ENODEV"
            st.enodev_seen = True
            raise OSError(errno.ENODEV, "no such device")
        try:
            res = self.vft.control(rt, rq, wv, wi, wl, out)
        except OSError as e:
            op["err"] = _ename(e.errno)
            raise
        if rt & 0x80:
            data = bytes(res)[:wl]
            if data:
                C.memmove(c.data, data, len(data))
            op["in"] = data[:18].hex() if name == "XU_GET_CUR" else data.hex()
            op["ret"] = len(data)
            return len(data)
        op["ret"] = wl
        return wl

    # -- interfaces

    def _holder(self, i, st):
        if self.kernel_driver.get(i):
            return self.kernel_driver[i]
        if i == 1 and self.other_owner:
            return "usbfs"
        if any(i in s.claimed for s in self._fds.values() if s is not st and not s.closed):
            return "usbfs"
        return None

    def _claim(self, st, arg):
        i = C.c_uint.from_address(arg).value
        holder = self._holder(i, st)
        if holder:
            self._rec("claim", st.fd, iface=i, err="EBUSY", holder=holder)
            raise OSError(errno.EBUSY, "interface %d claimed by %s" % (i, holder))
        st.claimed.add(i)
        st.any_claim = True
        self._rec("claim", st.fd, iface=i)
        return 0

    def _release(self, st, arg):
        i = C.c_uint.from_address(arg).value
        if i not in st.claimed:
            self._rec("release", st.fd, iface=i, err="EINVAL")
            raise OSError(errno.EINVAL, "interface %d not claimed" % i)
        st.claimed.discard(i)
        destroyed = self._destroy(st) if i == 1 else 0
        self._rec("release", st.fd, iface=i, destroyed=destroyed)
        return 0

    def _destroy(self, st):
        n = len(st.pending)
        self._cancel(st)
        for rec in st.pending:
            st.mem -= rec.length
            self._mem -= rec.length
        st.pending.clear()
        return n

    def _getdriver(self, st, arg):
        gd = U.GetDriver.from_address(arg)
        i = gd.interface
        name = self._holder(i, st) or ("usbfs" if i in st.claimed else None)
        self._rec("getdriver", st.fd, iface=i, driver=name)
        if name is None:
            raise OSError(errno.ENODATA, "no driver bound")
        gd.driver = name.encode()
        return 0

    def _disconnect_claim(self, st, arg):
        dc = U.DisconnectClaim.from_address(arg)
        i, flags, drv = dc.interface, dc.flags, dc.driver.decode("ascii", "replace")
        op = self._rec("disconnect_claim", st.fd, iface=i, flags=flags, driver=drv)
        bound = self._holder(i, st)
        if bound is not None:
            if (flags & U.DISCONNECT_CLAIM_IF_DRIVER and bound != drv) or \
                    (flags & U.DISCONNECT_CLAIM_EXCEPT_DRIVER and bound == drv):
                op["err"] = "EBUSY"
                raise OSError(errno.EBUSY, "interface %d held by %s" % (i, bound))
            if bound == "usbfs":
                self._violation("steal_from_usbfs_owner", "interface %d flags 0x%x driver %r" % (i, flags, drv))
                self.other_owner = None if i == 1 else self.other_owner
            else:
                self.kernel_driver.pop(i, None)
                if self.tree is not None:
                    self.tree.set_iface_driver(self.sysname, i, None)
        st.claimed.add(i)
        st.any_claim = True
        return 0

    def _setinterface(self, st, arg):
        s = U.SetIf.from_address(arg)
        inflight = len(st.pending) + len(st.completed)
        if s.interface not in st.claimed:
            self._violation("unclaimed_interface_use", "SETINTERFACE %d" % s.interface)
        if inflight:
            self._violation("setinterface_with_urbs_inflight", "%d URBs" % inflight)
            self._destroy(st)
        op = self._rec("setif", st.fd, iface=s.interface, alt=s.altsetting, inflight=inflight)
        self._ep0_wait(st, op, KERNEL_CTRL_TIMEOUT_S)
        self.vft.set_interface(s.interface, s.altsetting)
        return 0

    def _setconfig(self, st, arg):
        value = C.c_uint.from_address(arg).value
        op = self._rec("setconfig", st.fd, value=value)
        if st.any_claim:
            self._violation("setconfig_after_claim", "fd %d" % st.fd)
        busy = any(self.kernel_driver.values()) or self.other_owner or \
            any(s.claimed for s in self._fds.values() if not s.closed)
        if busy:
            op["err"] = "EBUSY"
            raise OSError(errno.EBUSY, "an interface is claimed")
        self._ep0_wait(st, op, KERNEL_CTRL_TIMEOUT_S)
        self.vft.set_configuration()
        return 0

    # -- URBs

    def _submit(self, st, arg):
        u = U.Urb.from_address(arg)
        index, length, m = u.usercontext or 0, u.buffer_length, self.vft.m
        op = self._rec("submit", st.fd, i=index, len=length)
        err = None
        if u.type != U.URB_TYPE_BULK or u.endpoint != 0x81 or length <= 0:
            err = errno.EINVAL
        elif 1 not in st.claimed:
            self._violation("unclaimed_interface_use", "SUBMITURB on interface 1")
            err = errno.EINVAL
        elif m.urb_max_bytes is not None and length > m.urb_max_bytes:
            err = errno.EINVAL
        elif self._mem + length > m.usbfs_memory_bytes:
            err = errno.ENOMEM
        elif any(r.addr == arg for r in st.pending) or any(r[0].addr == arg for r in st.completed):
            self._violation("urb_double_submit", "index %d" % index)
            err = errno.EINVAL
        if err is not None:
            op["err"] = _ename(err)
            raise OSError(err, "SUBMITURB refused")
        st.pending.append(_Rec(arg, u, length, index))
        st.mem += length
        self._mem += length
        st.submitted = True
        self.submits += 1
        now = self.clock.monotonic()
        before = self.vft.stream
        self.vft.urb_submitted(now)
        if self.vft.stream is not None and self.vft.stream is not before:
            t = self.vft.brownout_due(now)
            if t is not None:
                self._call_at(t, lambda: self.unplug(self.vft.m.reenumerate_s))
        self._schedule(st)
        return 0

    def _reap(self, st, arg):
        if st.completed:
            rec, status, data = st.completed.popleft()
            n = len(data)
            if n:
                C.memmove(rec.urb.buffer, bytes(data), n)
            rec.urb.status, rec.urb.actual_length, rec.urb.error_count = status, n, 0
            C.c_void_p.from_address(arg).value = rec.addr
            if status == U.URB_STATUS["ENODEV"]:
                st.enodev_seen = True                   # the driver knows the device is gone (-ESHUTDOWN: flushed only)
            st.mem -= rec.length
            self._mem -= rec.length
            self._rec("reap", st.fd, i=rec.index, st=status, len=n)
            return 0
        if st.gone:
            st.enodev_seen = True
            self._rec("reap", st.fd, err="ENODEV")
            raise OSError(errno.ENODEV, "no such device")
        raise OSError(errno.EAGAIN, "no completed URB")

    def _reap_blocking(self, st, arg):
        self._violation("blocking_reap", "USBDEVFS_REAPURB used")
        raise OSError(errno.EINVAL, "blocking reap is not supported by the fake")

    def _discard(self, st, arg):
        rec = next((r for r in st.pending if r.addr == arg), None)
        if rec is None:
            self._rec("discard", st.fd, err="EINVAL")
            raise OSError(errno.EINVAL, "URB not pending")
        head = st.pending[0] is rec
        st.pending.remove(rec)
        if head:
            self._cancel(st)
        st.completed.append((rec, U.URB_STATUS["ENOENT"], b""))
        self._rec("discard", st.fd, i=rec.index)
        if not st.pending:
            self.vft.flow_idle(self.clock.monotonic())
        self._schedule(st)
        self._cond.notify_all()
        return 0

    def _clear_halt(self, st, arg):
        ep = C.c_uint.from_address(arg).value
        op = self._rec("clear_halt", st.fd, ep=ep)
        self._ep0_wait(st, op, KERNEL_CTRL_TIMEOUT_S)
        if ep == 0x81:
            self.vft.clear_halt(self.clock.monotonic())
        return 0

    def _reset(self, st, arg):
        inflight = self.inflight()
        self._rec("reset", st.fd, sysname=self.sysname, inflight=inflight)
        if inflight:
            self._violation("reset_while_streaming", "%d URBs in flight" % inflight)
        if "." in self.sysname:
            self._violation("reset_behind_hub", self.sysname)
        keep = not self.vft.m.reset_clears_wedge
        self.unplug(None)
        self._call_at(self.clock.monotonic() + 1.0, lambda: self.replug(keep_wedge=keep))
        return 0

    def _speed(self, st, arg):
        self._rec("speed", st.fd, ret=self.vft.speed_code)
        return self.vft.speed_code

    def _caps(self, st, arg):
        C.c_uint32.from_address(arg).value = 0x1F
        self._rec("caps", st.fd, value=0x1F)
        return 0

    # -- completion events (one per URB)

    def _cancel(self, st):
        if st.event is not None:
            st.event.cancel()
            st.event = None

    def _resync(self):
        """A stream started, stopped or halted: re-plan the head URB of every fd."""
        if self._gen != self.vft.gen:
            self._gen = self.vft.gen
            for st in self._fds.values():
                self._cancel(st)
                self._schedule(st)

    def _schedule(self, st):
        if st.event is not None or not st.pending or st.gone or st.closed:
            return
        plan = self.vft.plan_urb(st.pending[0].length, self.clock.monotonic())
        if plan is None:
            return
        t_done, end = plan
        rec, gen = st.pending[0], self.vft.gen
        st.event = self._call_at(t_done, lambda: self._fire(st, rec, end, gen))

    def _fire(self, st, rec, end, gen):
        with self._lock:
            st.event = None
            if gen != self.vft.gen or st.closed or st.gone or not st.pending or st.pending[0] is not rec:
                self._resync()
                self._schedule(st)
                return
            now = self.clock.monotonic()
            status, data = self.vft.take_urb(end, now)
            if self._forced:
                (status, halt), data = self._forced.popleft(), b""
                if halt:                                # a STALL: the endpoint stays halted (fakevft.clear_halt)
                    self.vft.halted, self.vft.gen = True, self.vft.gen + 1
            st.pending.popleft()
            st.completed.append((rec, status, data))
            self.completion_events += 1
            if not st.pending or self.vft.halted:
                self.vft.flow_idle(now)
            self._resync()
            self._schedule(st)
            self._cond.notify_all()

    # -- plug events

    def system_suspend(self, seconds: float = 0.0) -> None:
        """A system suspend with the tracker kept attached (Linux usb_suspend_both): every pending URB
        completes with -ESHUTDOWN (usb_hcd_flush_endpoint) and the stream stops, but the device stays
        present: no POLLHUP, ioctls keep working, the registers are kept. `seconds` > 0 also moves the
        SimClock's boottime (clock.suspend), the jump the service's suspend detection sees."""
        with self._lock:
            self.vft.stop_stream()
            for st in self._fds.values():
                if not st.closed:
                    self._cancel(st)
                    while st.pending:
                        st.completed.append((st.pending.popleft(), U.URB_STATUS["ESHUTDOWN"], b""))
            self._rec("system_suspend", None, seconds=seconds)
            self._cond.notify_all()
        if seconds:
            self.clock.suspend(seconds)

    def unplug(self, replug_after=None, keep_wedge=False) -> None:
        """The device disappears: pending URBs complete with -ESHUTDOWN, every ioctl gives ENODEV,
        polls report HUP|ERR and the sysfs entry goes away; replug_after s later it re-enumerates."""
        with self._lock:
            if not self.present:
                return
            self.present = False
            self.vft.stop_stream()
            for st in self._fds.values():
                if st.closed:
                    continue
                st.gone = True
                self._cancel(st)
                while st.pending:
                    st.completed.append((st.pending.popleft(), U.URB_STATUS["ESHUTDOWN"], b""))
                st.claimed.clear()
            if self.tree is not None:
                self.tree.remove_device(self)
            self._rec("unplug", None, sysname=self.sysname, devnum=self.devnum)
            self._cond.notify_all()
            t = self.clock.monotonic()
        if replug_after is not None:
            self._call_at(t + replug_after, lambda: self.replug(keep_wedge))

    def replug(self, keep_wedge=False) -> None:
        """The device enumerates again with the next devnum (a fresh power-on)."""
        with self._lock:
            if self.present:
                return
            self.devnum += 1
            self.vft.power_on(keep_wedge=keep_wedge)
            self.present = True
            m = self.vft.m
            self.kernel_driver = {0: m.kernel_driver, 1: m.kernel_driver} if m.kernel_driver else {}
            if self.tree is not None:
                self.tree.write_device(self)
            self._rec("replug", None, sysname=self.sysname, devnum=self.devnum)
            self._cond.notify_all()
