# SPDX-License-Identifier: MIT
"""Trace and replay (spec §7.5); the trace invariants of the driver matrix (§11.3, check_trace) are in sim.invariants.

TraceSys wraps any Sys and writes one JSON line per operation. Records in `out_path` hold at most
16 payload bytes ("hdr" of a reap); with `pixels_path` (file mode 0600, the probe's --pixels) the
same records carry the full payload ("data") as well. ReplayKernel turns such a trace back into a
Sys: control requests are answered from the next recorded request with the same (rt, rq, wv, wi),
URB completions are replayed in order with the recorded status and length, and the data is the
recorded prefix (the payload header) plus scene filler. On a SimClock a replayed control transfer also
takes its recorded duration ("dt"), so a replay keeps the recorded timeline.
"""
import collections
import ctypes as C
import errno
import json
import os
import threading
import time

from .. import usbfs as U
from .. import uvc
from .scenes import ANIMATED, make_scene_frame

__all__ = ["TraceSys", "ReplayKernel", "ctrl_name", "XU_COMMANDS"]

XU_COMMANDS = ("MAGIC", "STREAM_OFF", "STREAM_ON", "REG_WRITE", "REG_READ", "XU_SET")
_VS_NAMES = {(1, 0x01): "PROBE_SET", (1, 0x81): "PROBE_GET_CUR", (1, 0x82): "PROBE_GET_MIN",
             (1, 0x83): "PROBE_GET_MAX", (1, 0x87): "PROBE_GET_DEF", (2, 0x01): "COMMIT_SET",
             (2, 0x81): "COMMIT_GET_CUR"}


def ctrl_name(rt, rq, wv, wi, data=None) -> str:
    """A readable name of a control request: MAGIC, REG_WRITE, XU_GET_CUR, PROBE_SET, COMMIT_SET..."""
    iface, entity, sel = wi & 0xFF, wi >> 8, wv >> 8
    if rt == 0x01 and rq == 0x0B:
        return "SET_INTERFACE_RAW"
    if iface == 0 and entity:
        if rq == 0x85:
            return "XU_GET_LEN"
        if rq == 0x86:
            return "XU_GET_INFO"
        if rq == 0x81:
            return "XU_GET_CUR"
        if rq == 0x01 and data:
            head = bytes(data[:4])
            if head[:2] == b"\x51\x52":
                return "MAGIC"
            if head == b"\x50\x14\x00\x00":
                return "STREAM_OFF"
            if head == b"\x50\x14\x00\x01":
                return "STREAM_ON"
            if head[:2] == b"\x50\xab":
                return "REG_WRITE"
            if head[:2] == b"\x51\xab":
                return "REG_READ"
        return "XU_SET" if rq == 0x01 else "XU_0x%02x" % rq
    if iface == 0 and sel == 2 and rq == 0x81:
        return "ERROR_CODE"
    if iface == 1:
        return _VS_NAMES.get((sel, rq), "VS_0x%02x" % rq)
    return "CTRL"


_HDR_BYTES = 16
_ERRNO = {v: k for k, v in errno.errorcode.items()}


def _err(name):
    """An OSError for a recorded errno name ("EPIPE")."""
    n = _ERRNO.get(name, errno.EIO)
    cls = PermissionError if name in ("EACCES", "EPERM") else FileNotFoundError if name == "ENOENT" else OSError
    return cls(n, "replayed %s" % name)


class TraceSys:
    """A Sys that records every operation of `inner` (wait() is not recorded). `records` holds every record
    when there is no output file; with one only the last `keep` stay in memory (the file has them all), so a
    long probe run (A7 soak: about 1200 records/s) cannot grow the process without bound."""

    def __init__(self, inner, out_path=None, pixels_path=None, clock=None, keep=4096):
        self.inner, self.clock = inner, clock
        self.records = collections.deque(maxlen=None if not (out_path or pixels_path) else keep)
        self._t0 = None
        self._lock = threading.Lock()
        self._out = open(out_path, "w", encoding="utf-8", newline="\n") if out_path else None
        self._px = None
        if pixels_path:
            fd = os.open(pixels_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
            if os.name == "posix":
                os.chmod(pixels_path, 0o600)
            self._px = os.fdopen(fd, "w", encoding="utf-8", newline="\n")

    def close_trace(self):
        for f in (self._out, self._px):
            if f is not None:
                f.close()
        self._out = self._px = None

    def _new(self, op, **kv):
        now = self.clock.monotonic() if self.clock is not None else time.monotonic()
        if self._t0 is None:
            self._t0 = now
        rec = {"t": round(now - self._t0, 6), "op": op}
        rec.update(kv)
        return rec

    def _emit(self, rec, data=None):
        with self._lock:
            self.records.append(rec)
            if self._out is not None:
                self._out.write(json.dumps(rec) + "\n")
            if self._px is not None:
                full = dict(rec, data=data.hex()) if data is not None else rec
                self._px.write(json.dumps(full) + "\n")

    def open(self, path, flags):
        try:
            fd = self.inner.open(path, flags)
        except OSError as e:
            self._emit(self._new("open", path=path, flags=flags, err=U.errno_name(e)))
            raise
        self._emit(self._new("open", path=path, flags=flags, ret=fd))
        return fd

    def close(self, fd):
        self._emit(self._new("close", fd=fd))
        self.inner.close(fd)

    def wait(self, fd, timeout_s):
        return self.inner.wait(fd, timeout_s)

    def ioctl(self, fd, request, arg):
        rec = self._pre(request, arg)
        t = self.clock.monotonic() if self.clock is not None else time.monotonic()
        try:
            ret = self.inner.ioctl(fd, request, arg)
        except OSError as e:
            if not (request == U.REAPURBNDELAY and e.errno == errno.EAGAIN):
                rec["err"] = U.errno_name(e)
                self._emit(rec)
            raise
        data = self._post(request, arg, rec, ret)
        if request == U.CONTROL:                    # the transfer's duration, replayed on a SimClock
            rec["dt"] = round((self.clock.monotonic() if self.clock is not None else time.monotonic()) - t, 6)
        self._emit(rec, data)
        return ret

    def _pre(self, request, arg):
        if request == U.CONTROL:
            c = U.Ctrl.from_address(arg)
            out = C.string_at(c.data, c.wLength).hex() if not c.bRequestType & 0x80 and c.wLength else None
            return self._new("ctrl", rt=c.bRequestType, rq=c.bRequest, wv=c.wValue, wi=c.wIndex, wl=c.wLength,
                             out=out, **{"in": None})
        if request == U.SUBMITURB:
            u = U.Urb.from_address(arg)
            return self._new("submit", i=u.usercontext or 0, len=u.buffer_length)
        if request == U.DISCARDURB:
            return self._new("discard", i=U.Urb.from_address(arg).usercontext or 0)
        if request in (U.CLAIMINTERFACE, U.RELEASEINTERFACE):
            return self._new("claim" if request == U.CLAIMINTERFACE else "release",
                             **{"if": C.c_uint.from_address(arg).value})
        if request == U.SETINTERFACE:
            s = U.SetIf.from_address(arg)
            return self._new("setif", alt=s.altsetting, **{"if": s.interface})
        if request == U.SETCONFIGURATION:
            return self._new("setconfig", value=C.c_uint.from_address(arg).value)
        if request == U.GETDRIVER:
            return self._new("getdriver", **{"if": U.GetDriver.from_address(arg).interface})
        if request == U.DISCONNECT_CLAIM:
            dc = U.DisconnectClaim.from_address(arg)
            return self._new("disconnect_claim", flags=dc.flags, driver=dc.driver.decode("ascii", "replace"),
                             **{"if": dc.interface})
        if request == U.CLEAR_HALT:
            return self._new("clear_halt", ep=C.c_uint.from_address(arg).value)
        names = {U.REAPURBNDELAY: "reap", U.RESET: "reset", U.GET_SPEED: "speed", U.GET_CAPABILITIES: "caps"}
        return self._new(names.get(request, "ioctl"), req=request)

    def _post(self, request, arg, rec, ret):
        if request == U.CONTROL:
            c = U.Ctrl.from_address(arg)
            if c.bRequestType & 0x80:
                rec["in"] = C.string_at(c.data, ret).hex() if ret else ""
            rec["ret"] = ret
        elif request == U.REAPURBNDELAY:
            u = U.Urb.from_address(C.c_void_p.from_address(arg).value)
            n = max(0, u.actual_length)
            data = C.string_at(u.buffer, n) if n else b""
            rec.update(i=u.usercontext or 0, st=u.status, len=n, hdr=data[:_HDR_BYTES].hex())
            return data
        elif request == U.GETDRIVER:
            rec["driver"] = U.GetDriver.from_address(arg).driver.decode("ascii", "replace")
        elif request == U.GET_CAPABILITIES:
            rec["value"] = C.c_uint32.from_address(arg).value
        else:
            rec["ret"] = ret
        return None


class ReplayKernel:
    """A Sys answering from a TraceSys trace (see the module docstring). Completions are due at
    their recorded time relative to the first SUBMITURB; wait() sleeps (or advances a SimClock).
    It never writes past a URB's buffer_length: a SUBMITURB smaller than the largest recorded reap
    raises ValueError (replay with the recorded URB size, `urb_bytes`)."""

    def __init__(self, trace_path, clock, scene="mouth", layout="h1_copy"):
        self.clock, self.scene, self.layout = clock, scene, layout
        with open(trace_path, encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
        self.ctrl = collections.defaultdict(collections.deque)
        self.last = {}
        self.kinds = collections.defaultdict(collections.deque)
        self.max_payload = 32768
        submits = [r for r in records if r["op"] == "submit" and "err" not in r]
        t_sub = submits[0]["t"] if submits else 0.0
        self.urb_bytes = max((r["len"] for r in submits), default=None)  # the largest recorded URB
        self.completions = collections.deque(
            (r["t"] - t_sub, r) for r in records
            if r["op"] == "reap" and "err" not in r and r.get("st") not in (-2, -104))
        self.reap_max = max([r.get("len", 0) for _, r in self.completions if r.get("st", 0) == 0] + [0])
        for r in records:
            if r["op"] == "ctrl":
                key = (r["rt"], r["rq"], r["wv"], r["wi"])
                self.ctrl[key].append(r)
                if ctrl_name(r["rt"], r["rq"], r["wv"], r["wi"]) == "COMMIT_SET" and r.get("out") and "err" not in r:
                    self.max_payload = uvc.unpack_probe(bytes.fromhex(r["out"])).max_payload or 32768
            elif r["op"] != "reap":
                self.kinds[r["op"]].append(r)
        self.ops = []
        self._fds, self._next_fd = set(), 300
        self._pending, self._done = collections.deque(), collections.deque()
        self._t_sub0 = None
        self._fid, self._frame, self._pos = None, -1, 0
        self.completion_events = 0

    def _next(self, kind):
        q = self.kinds.get(kind)
        return q.popleft() if q else None

    def open(self, path, flags):
        r = self._next("open")
        if r is not None and "err" in r:
            raise _err(r["err"])
        self._next_fd += 1
        self._fds.add(self._next_fd)
        self.ops.append({"op": "open"})
        return self._next_fd

    def close(self, fd):
        self._next("close")
        self._fds.discard(fd)
        self._pending.clear()
        self._done.clear()
        self.ops.append({"op": "close"})

    def ioctl(self, fd, request, arg):
        if fd not in self._fds:
            raise OSError(errno.EBADF, "replay: fd not open")
        if request == U.CONTROL:
            return self._control(arg)
        if request == U.SUBMITURB:
            u = U.Urb.from_address(arg)
            if u.buffer_length < self.reap_max:
                raise ValueError("replay: the trace reaps up to %d bytes per URB, this URB holds %d; replay with "
                                 "urb_bytes=%s (the recorded URB size)" % (self.reap_max, u.buffer_length,
                                                                          self.urb_bytes))
            if self._t_sub0 is None:
                self._t_sub0 = self.clock.monotonic()
            self._pending.append((arg, u))
            self.ops.append({"op": "submit", "i": u.usercontext or 0})
            return 0
        if request == U.REAPURBNDELAY:
            self._complete_due()
            if not self._done:
                raise OSError(errno.EAGAIN, "no completed URB")
            addr, u, status, data = self._done[0]
            if len(data) > u.buffer_length:                 # never past the URB (SUBMITURB refused it already)
                raise ValueError("replay: %d bytes for a %d-byte URB" % (len(data), u.buffer_length))
            self._done.popleft()
            if data:
                C.memmove(u.buffer, data, len(data))
            u.status, u.actual_length, u.error_count = status, len(data), 0
            C.c_void_p.from_address(arg).value = addr
            return 0
        if request == U.DISCARDURB:
            for item in self._pending:
                if item[0] == arg:
                    self._pending.remove(item)
                    self._done.append((arg, item[1], U.URB_STATUS["ENOENT"], b""))
                    return 0
            raise OSError(errno.EINVAL, "URB not pending")
        kind = {U.CLAIMINTERFACE: "claim", U.RELEASEINTERFACE: "release", U.SETINTERFACE: "setif",
                U.SETCONFIGURATION: "setconfig", U.GETDRIVER: "getdriver", U.DISCONNECT_CLAIM: "disconnect_claim",
                U.CLEAR_HALT: "clear_halt", U.RESET: "reset", U.GET_SPEED: "speed",
                U.GET_CAPABILITIES: "caps"}.get(request)
        if kind is None:
            raise OSError(errno.ENOTTY, "replay: unknown ioctl")
        r = self._next(kind) or {}
        self.ops.append({"op": kind})
        if "err" in r:
            raise _err(r["err"])
        if kind == "getdriver":
            U.GetDriver.from_address(arg).driver = (r.get("driver") or "usbfs").encode()
        elif kind == "caps":
            C.c_uint32.from_address(arg).value = r.get("value", 0x1F)
        return int(r.get("ret", 0) or 0) if kind == "speed" else 0

    def _control(self, arg):
        c = U.Ctrl.from_address(arg)
        key = (c.bRequestType, c.bRequest, c.wValue, c.wIndex)
        q = self.ctrl.get(key)
        r = q.popleft() if q else self.last.get(key)
        self.ops.append({"op": "ctrl", "rt": key[0], "rq": key[1], "wv": key[2], "wi": key[3], "wl": c.wLength})
        if r is None:
            raise OSError(errno.EPIPE, "replay: no recorded answer")
        self.last[key] = r
        if r.get("dt") and hasattr(self.clock, "advance"):
            self.clock.advance(r["dt"])
        if "err" in r:
            raise _err(r["err"])
        if c.bRequestType & 0x80:
            data = bytes.fromhex(r.get("in") or "")[:c.wLength]
            if data:
                C.memmove(c.data, data, len(data))
            return len(data)
        return c.wLength

    def _due(self):
        if self._t_sub0 is None or not self.completions or not self._pending:
            return None
        return self._t_sub0 + self.completions[0][0]

    def _complete_due(self):
        now = self.clock.monotonic()
        while self._pending and self.completions and self._due() <= now:
            _, r = self.completions.popleft()
            addr, u = self._pending.popleft()
            self._done.append((addr, u, r.get("st", 0), self._data(r, u.buffer_length)))
            self.completion_events += 1

    def _data(self, r, urb_len):
        n = r.get("len", 0)
        if r.get("st", 0) != 0 or n <= 0:
            return b""
        hdr = bytes.fromhex(r.get("hdr", ""))
        out = bytearray(n)
        heads = list(range(0, n, self.max_payload))
        for j, off in enumerate(heads):
            h = bytearray(hdr[:12]) if len(hdr) >= 12 and hdr[0] == 12 else None
            if h is not None and j:
                h[1] &= ~0x02 & 0xFF
                if j == len(heads) - 1 and n < urb_len:
                    h[1] |= 0x02                        # a short URB ends the frame
            if h is not None and (h[1] & 1) != self._fid:
                self._fid, self._frame, self._pos = h[1] & 1, self._frame + 1, 0
            start = off + (len(h) if h is not None else 0)
            end = min(n, off + self.max_payload)
            img = make_scene_frame(self.scene, self._frame / 60.0, self.layout)
            if self.scene in ANIMATED:
                img = bytearray(img)
                img[7], img[14] = self._frame & 0xFF, (self._frame * 37) & 0xFF
            take = img[self._pos:self._pos + end - start]
            out[start:start + len(take)] = take
            self._pos += end - start
            if h is not None:
                out[off:off + len(h)] = h
        out[:len(hdr)] = hdr
        return bytes(out)

    def wait(self, fd, timeout_s):
        if fd not in self._fds:
            return U.POLLNVAL
        end = self.clock.monotonic() + max(0.0, timeout_s)
        while True:
            self._complete_due()
            now = self.clock.monotonic()
            if self._done or now >= end:
                return U.POLLOUT if self._done else 0
            due = self._due()
            self.clock.sleep(min(end, due) - now if due is not None else end - now)
