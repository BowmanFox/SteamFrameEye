# SPDX-License-Identifier: MIT
"""The probe's active steps (spec §5.2), split off probe.py for the 600-line limit: ActiveSteps is a mixin of probe.Probe.

One session = settle, open, Block A, Block B, start (A1-A3 when it is the main session); _stream feeds frames through the
service's own assembler and health gate (A4); _close runs Block C with the A12 readback hook (REG_READ 0x02-0x04 after the OFF
writes) and the Store's mark. A5-A11 are built from sessions; _a12 judges decision 33: PASS only if all three PWM registers
read back 0 (a non-zero value: IR still on; no answer, or 0 although a session set the PWM and no REG_READ of 0x02-0x04 in the
run, A2/A11 after STREAM_ON or a teardown readback, ever read non-zero: cannot verify, a REG_READ that always says 0 proves
nothing); the USB/battery currents are information only (the "before" sample is taken while the stream still runs, so its
drop says nothing about the LEDs). A stop signal reaches a session's driver from the "stop-signals" thread (probe._wake);
_session sets the abort itself when the stop came before the session was published. Dense like probe.py: method groups
without blank lines.
"""
import contextlib, json, os, statistics, time  # noqa: E401
from collections import Counter
from types import SimpleNamespace as NS

from . import uvc
from .assembler import BulkReassembler, FrameAssembler, FramePool
from .config import atomic_write
from .descriptors import sha1_hex
from .jpeg import JpegEncoder, JpegError
from .policy import IR_LEVELS, STRATEGIES, settle_deadline, xu_len_for
from .quality import HealthTracker, analyze_layout
from .sim.replay import ctrl_name
from .state import Profile
from .sysfs import find_vft
from .usbfs import DeviceGone, UsbDevice, errno_name
from .vft import ActivationError, DriverConfig, ProbeReject, VftDriver, XuError, XuTimeout

__all__ = ["ActiveSteps", "OK_S", "STEP_S", "IDLE_S", "IR_SWEEP", "ERRS"]
OK_S, STEP_S, IDLE_S = 2.0, 5.0, 60.0           # OK_S: a step "streams" (the profile rule of §5.2)
IR_SWEEP = tuple((k, 0xFF, IR_LEVELS[k]) for k in ("L0", "L1", "L2", "L3")) + (("L0 pwm80", 0x80, IR_LEVELS["L0"]),)   # A6: name, PWM, current
ERRS = ("epipe", "eproto", "eilseq", "etime", "eoverflow")
A12_MAGIC3_HINT = "the trailing MAGIC (teardown_magic3) is on already: report the bundle (install-frameface.ps1 -ProbeActive copies it)"
A12_UNREAD_HINT = ('REG_READ does not show the IR registers on this tracker, so the probe cannot see the IR state: set '
                   '"teardown_magic3": true in ~/.config/frameface/config.json to be safe')
_mean = lambda xs: round(statistics.fmean(xs), 2) if xs else None  # noqa: E731
_kv = lambda d: " ".join("%s=%s" % i for i in d.items() if not isinstance(i[1], (list, dict)) or len(str(i[1])) < 60)  # noqa: E731
_bytes = lambda d: d.encode("utf-8") if isinstance(d, str) else bytes(d)  # noqa: E731


class ActiveSteps:
    # -- sessions (settle, open, Block A/B, start), streaming through the health gate, Block C (§5.2)
    def _settle(self, delay=None):              # before every activation: the service's settle rules (A9: `delay` on purpose)
        end = self.clock.monotonic() + 10.0     # after a LOST: up to 10 s for the tracker to come back; a new node is a new enumeration
        while (v := find_vft(self.e["sys_root"], self.e["dev_root"])) is None and self.clock.monotonic() < end: self._sleep(1.0)  # noqa: E701
        rt, bt = self.store.load_runtime(), self.clock.boottime()
        self.enum_bt, self.vft = bt if v and v.devpath != self.vft.devpath else self.enum_bt, v or self.vft
        last = max((t for t in (rt.last_teardown_bt, rt.last_loss_bt) if t is not None), default=None)  # session end, a LOST too (_deadline)
        d = (rt.last_teardown_bt or bt) + delay if delay is not None else settle_deadline(
            bt, same_boot=self.store.same_boot(rt), enum_bt=self.enum_bt, start_bt=self.start_bt, dirty_start=self.dirty_start,
            last_teardown_bt=last, last_reset_bt=rt.last_reset_bt, absent_since_teardown=False, settle_s=self.cfg.settle_s,
            enum_settle_s=self.cfg.enum_settle_s, settle_mult=1.0, backoff_until_bt=-1e18)
        self.settles.append(round(max(0.0, d - bt), 3)); self._sleep(max(0.0, d - bt))  # noqa: E702
    def _mark(self, what):                      # runtime.json; a write error (full disk) must not stop the run or its Block C
        with contextlib.suppress(OSError): getattr(self.store, "mark_" + what)()  # noqa: E701
    def _saw(self, regs):                       # A12: a REG_READ of PWM 0x02-0x04 that read non-zero shows that REG_READ reads them
        vals = [regs.get(k) for k in ("02", "03", "04")] if isinstance(regs, dict) else regs
        self.pwm_shown = self.pwm_shown or any(isinstance(v, int) and v for v in vals)
        return regs
    @staticmethod
    def _q(drv, setup, reg=None, ack_ms=1000):  # a raw control read (hex; through vft._ctrl: it tracks EP0, and once a stop
        try:                                    # is pending a silent EP0 gets nothing), or REG_READ's byte 17; None or error text
            return drv._ctrl_in(setup).hex() if reg is None else drv.xu(uvc.reg_read(reg, drv.n), ack_ms)[1][17]
        except (XuTimeout, XuError, IndexError):
            return None
        except DeviceGone:
            raise
        except OSError as e:
            return "error:" + errno_name(e)
    def _session(self, rung, n=None, pwm=None, cur=None, settle=None, main=None):
        # settle, open, Block A, B, start; main (A4/A11): the A1 queries, REG_READ after STREAM_ON (S3: after the start), A3 hex; error: Block C ran
        s, ses = STRATEGIES[rung], NS(rung=rung, n=n, error=None, stage="A1", dirty=False, dump=None, tdir=None, mark=None)
        self._settle(settle)
        cur, pwm = self.cur if cur is None else cur, self.cfg.ir_pwm if pwm is None else pwm      # the configured IR unless A6 sweeps
        self.pwm_set = self.pwm_set or bool(pwm)                                                  # A12: the IR may have been on
        rep = lambda cid, *x, **kv: main and self._chk("A11" if main == "A11" else cid, *x, **kv)  # noqa: E731
        drv = ses.drv = VftDriver(UsbDevice(self.trace, self.vft.devpath), self.vft, self.clock, self.log, DriverConfig(
            n_urbs=self.cfg.n_urbs, urb_bytes=self.cfg.urb_bytes, teardown_magic3=self.magic3), not self.e["threaded"])
        self.ses, regs, q = ses, (lambda: self._saw({"%02x" % r: self._q(drv, None, r) for r in uvc.IR_REG_ADDRS})), (
            lambda st: main and self._q(drv, st))
        try:                                    # self.ses first, then stop_req: a stop _wake missed (no session yet) is seen here
            (self.aborted or self.stop_req.is_set()) and drv.abort.set(); ident = drv.open(set_config=s.set_config)  # noqa: E702
            n = ses.n = n or xu_len_for(rung, self.cfg.xu_len, self.known, self.prof_n, ident.get_len)
            self.e["note"] and self.e["note"](strategy=rung, n=n, set_config=s.set_config, stop=drv.abort.is_set())
            rep("A1", "PASS", **dict({k: v for k, v in vars(ident).items() if k != "layout"}, xu_len=n, sel1_get_info=q(uvc.xu_setup(
                uvc.GET_INFO, 1, ident.xu_unit, 1)), sel1_get_len=q(uvc.xu_setup(uvc.GET_LEN, 2, ident.xu_unit, 1)), first_get_cur=q(
                uvc.xu_setup(uvc.GET_CUR, n, ident.xu_unit))))
            ses.stage, ses.dirty = "A2", self._mark("dirty") or True
            drv.activate(s, pwm, cur, n)
            r = regs() if main and not s.xu_between else None
            ses.stage, L = "A3", uvc.probe_length(ident.layout.bcd_uvc)
            before = {k: q(uvc.vs_setup(rq, uvc.VS_PROBE, L, drv.vs_if)) for k, rq in (("GET_CUR", uvc.GET_CUR), ("GET_DEF", uvc.GET_DEF),
                                                                                      ("GET_MIN", uvc.GET_MIN), ("GET_MAX", uvc.GET_MAX))}
            mark, params, lay = self.trace.records[-1], drv.negotiate(s, pwm, cur, n), ident.layout     # after: Block B's own transfers
            after = ["%s %s" % (ctrl_name(c["rt"], c["rq"], c["wv"], c["wi"]), c["in"] or c["out"]) for c in self._since(mark, "ctrl") if
                     c["wi"] & 0xFF == drv.vs_if]
            ses.pool, ses.health = FramePool(lay.width * lay.height * 2), HealthTracker(self.phase)
            ses.asm = FrameAssembler(lay.width * lay.height * 2, ses.pool, self.clock)
            ses.mark = self.trace.records[-1]; drv.start(params, BulkReassembler(ses.asm, params.max_payload, lay.max_packet))  # noqa: E702
            r, act = regs() if main and s.xu_between else r, drv.last_activation
            rep("A2", "PASS", None, None, None, **dict(vars(act), busy_polls=sum(c["busy"] or 0 for c in act.cmds), regs=r))
            rep("A3", "PASS", None, None, None, **dict(vars(params), before=before, after=after))
        except (ActivationError, XuTimeout, XuError, ProbeReject, OSError, ValueError, RuntimeError) as e:
            ses.error = "%s: %s" % (type(e).__name__, e)
            rep(ses.stage, "FAIL", ses.error)
            self.ses is ses and self._close(ses, lost=isinstance(e, DeviceGone) or drv.gone)
        return ses
    def _stream(self, ses, seconds, measure=False, until=None):
        # frames through the health gate for `seconds` or until(st); LOST path if gone; measure (A4): plane, timing, the 256-completion wire
        st = NS(kinds=Counter(), ok=0, dark_start=0, dark_after=0, best=0.0, run_t=None, means=[], layouts=Counter(), pre_ms=[], jpeg_ms=[],
                jpeg={90: [], 92: [], 95: []}, cur=None, wire=None, lost=False)
        drv, t0, w0, cpu0, mark = ses.drv, self.clock.monotonic(), time.monotonic(), sum(os.times()[:2]), ses.mark   # mark: before start()
        b0, threaded = drv.stats["bytes"], self.e["threaded"]
        try:
            while self.clock.monotonic() < t0 + seconds and not (until and until(st)):
                self._check()
                if drv.gone or isinstance(drv.urbs.reaper_error, DeviceGone): raise DeviceGone(19, "tracker gone")  # noqa: E701
                threaded or drv.pump(0.01)
                got = ses.pool.take(0.05 if threaded else 0)
                st.cur = st.cur or (self._power() if self.clock.monotonic() >= t0 + seconds / 2 else None)
                st.wire = self._since(mark, "reap")[:256] if measure and len(st.wire or ()) < 256 else st.wire   # the stream's first 256
                if not got: continue  # noqa: E701
                (buf, info), h = got, ses.health.classify(got[0]); st.kinds[h.kind] += 1  # noqa: E702
                ses.dump is not None and len(ses.dump) < self.args.dump and ses.dump.append((bytes(buf), info.seq, h.kind))
                if h.kind != "ok":
                    st.run_t, st.dark_after, st.dark_start = None, st.dark_after + (h.kind == "dark" and st.ok > 0), st.dark_start + (h.kind == "dark" and not st.ok)
                    continue
                st.ok, st.run_t = st.ok + 1, info.t_mono if st.run_t is None else st.run_t
                st.best, st.means = max(st.best, info.t_mono - st.run_t), (st.means + [h.mean])[-5000:]
                if st.ok % 30 == 1:
                    st.layouts[analyze_layout(buf).verdict] += 1
                    t = time.perf_counter(); px = self._pre()(buf, self.phase, self.col0) if measure else None  # noqa: E702
                    st.pre_ms.append((time.perf_counter() - t) * 1000)
                    for q in (90, 92, 95) if measure and self._enc() else ():
                        t = time.perf_counter(); st.jpeg[q].append(len(self.enc.encode_gray(px, quality=q)))  # noqa: E702
                        st.jpeg_ms.append((time.perf_counter() - t) * 1000)
        except DeviceGone:
            st.lost = self._close(ses, lost=True) or True
        el = max(1e-6, self.clock.monotonic() - t0); st.secs, st.kbps = round(el, 2), int((drv.stats["bytes"] - b0) * 8 / el / 1000)  # noqa: E702
        st.cpu = round(100 * (sum(os.times()[:2]) - cpu0) / max(1e-6, time.monotonic() - w0), 1)
        st.wire = self._since(mark, "reap")[:256] if measure and len(st.wire or ()) < 256 else st.wire
        return st
    def _enc(self):                             # the JPEG encoder of A4/A8 (loaded once), False without TurboJPEG
        with contextlib.suppress(JpegError, OSError):
            self.enc = (self.e["jpeg"] or (lambda: JpegEncoder.load(quality=self.cfg.jpeg_quality)))() if self.enc is None else self.enc
        self.enc = self.enc or False
        return self.enc
    def _close(self, ses, lost=False):          # Block C (lost: the LOST path) with the A12 readback hook, then the Store's mark
        self.ses, drv, self.readback, before = None, ses.drv, None, self._power()
        try:
            drv.teardown(clean=not lost, after_xu=lambda d: setattr(self, "readback", self._saw([self._q(d, None, r, 200) for r in (2, 3, 4)])))
        except Exception as e:                  # Block C never takes the probe down
            self.log.e("usb", "teardown failed", error=repr(e))
        ses.dirty and self._mark("lost" if drv.teardown_path == "lost" else "clean_teardown")
        if drv.teardown_path == "clean":        # A12: the currents 1 s after the close
            ses.tdir = self.tdir = {"pwm_readback": self.readback, "magic3": self.magic3, "current_before": before,
                                    "current_after": self._sleep(1.0, quiet=True) or self._power()}
    def _cand(self, rung, n, st):               # §5.2: a later step with >= 2 s of ok frames is the known-good pair unless A2-A4 streamed
        self.good = self.good or ((rung, n) if st is not None and st.best >= OK_S else None)
        if not (self.base_ok or self.wrote or st is None or st.best < OK_S):
            p = self.store.load_profile(sha1_hex(self.desc)) or Profile(desc_sha1=sha1_hex(self.desc))
            p.strategy, p.xu_len, p.source = rung, n, "probe"; self.store.save_profile(p)  # noqa: E702
            self.wrote, self.report["profile"] = True, {"action": "written", "strategy": rung, "xu_len": n}
    def _run(self, rung, n=None, secs=STEP_S, pwm=None, cur=None, settle=None):
        ses = self._session(rung, n, pwm, cur, settle)
        st = None if ses.error else self._stream(ses, secs)
        self.ses is ses and self._close(ses)
        return ses, st, dict(strategy=rung, xu_len=ses.n, error=ses.error, ok_run_s=round(st.best, 2) if st else 0.0, health=dict(st.kinds)
                             if st else {}, dark_from_start=st.dark_start if st else 0, lost=bool(st and st.lost))
    def _main(self, rung, n, sid):
        # A1-A4 (A11: at XU length n): one session, --seconds of streaming; the 256-completion wire trace gives the payload/flag histograms
        self.step = "A1" if sid == "A4" else sid
        ses = self._session(rung, n, main=sid)
        if ses.error: return ses, None  # noqa: E701
        self.step = sid
        st, asm = self._stream(ses, self.args.seconds, measure=True), ses.asm
        k, w = st.kinds, [dict(x, dt_us=round((x["t"] - p["t"]) * 1e6)) for p, x in zip(st.wire[:1] + st.wire, st.wire)]  # + dt
        hdrs = [int(x["hdr"][2:4], 16) for x in w if x.get("hdr", "")[:2] == "0c" and len(x["hdr"]) >= 4]
        d = dict(frames=dict(asm.stats), health=dict(k), dark_from_start=st.dark_start, dark_after_ok=st.dark_after, ok_run_s=round(st.best, 2),
                 fps_in=round(asm.fps.fps_in, 2), fps_pts=asm.fps.fps_pts, payload_sizes=dict(Counter(x["len"] for x in w if x.get("st") == 0)),
                 header_flags=dict(Counter("%02x" % f for f in hdrs)), eof_payloads=sum(f & uvc.EOF > 0 for f in hdrs), fid_toggles=sum(
                     (a ^ b) & uvc.FID for a, b in zip(hdrs, hdrs[1:])), plane=dict(st.layouts), pre_ms=_mean(st.pre_ms), jpeg_ms=_mean(st.jpeg_ms),
                 jpeg_bytes={q: _mean(v) for q, v in st.jpeg.items()}, cpu_pct=st.cpu, usb_kbps=st.kbps, seconds=st.secs, usb=dict(ses.drv.stats),
                 wire=w)
        self._chk(sid, "PASS" if st.best >= OK_S else "WARN" if k["ok"] or sid == "A11" else "FAIL", "%s at XU length %s: %d ok, dark %d from "
                  "start + %d after ok, %d test bars, %d frozen, ok run %.1f s%s" % (rung, ses.n, k["ok"], st.dark_start, st.dark_after, k["testbar"],
                                                                                   k["frozen"], st.best, ", lost" if st.lost else ""), **d)
        sid == "A11" and self.ses is ses and self._close(ses)
        return ses, st
    def _a6(self):
        rows, (rung, n) = [], self.good or (self.rung, None)
        for name, pwm, cur in IR_SWEEP:
            bat, (ses, st, row) = self._power()["pct"], self._run(rung, n, STEP_S, pwm, cur)
            ts, susp, end = sorted(ses.drv.urbs.xfer_err_times), None, self.clock.monotonic() + 3.0
            while susp is None and self.clock.monotonic() < end:                # idle (fd closed): autosuspended?
                susp = self._power() if self._pm()["runtime_status"] == "suspended" else self._sleep(0.5)
            rows.append(dict(row, level=name, pwm=pwm, current=cur, during=st and st.cur, battery_before=bat, battery_after=self._power()[
                "pct"], idle=ses.tdir and ses.tdir["current_after"], suspended=susp, brightness=_mean(st.means) if st else None,
                brightness_std=round(statistics.pstdev(st.means), 2) if st and st.means else None, errors={k: ses.drv.stats[k] for k in ERRS},
                brownout_suspect=row["lost"] or any(ts[i + 50] - ts[i] <= 1.0 for i in range(len(ts) - 50))))
            if rows[-1]["brownout_suspect"]: break  # noqa: E701
        self._chk("A6", "WARN" if rows[-1]["brownout_suspect"] else "PASS", "; ".join("%s: ok run %s s, brightness %s%s" % (
            r["level"], r["ok_run_s"], r["brightness"], ", brown-out suspect" if r["brownout_suspect"] else "") for r in rows), levels=rows)
    def _a7(self, ses):
        rows, t0, bat = [], self.clock.monotonic(), self._power()["pct"]
        while self.ses is ses and len(rows) * 10 < self.args.soak:
            st, p = self._stream(ses, min(10.0, self.args.soak - len(rows) * 10)), self._power()
            rows.append({"t": round(self.clock.monotonic() - t0, 1), "fps_in": round(ses.asm.fps.fps_in, 1), "ok": st.ok, "errors": sum(
                ses.drv.stats[k] for k in ERRS), "temp_c": p["temp_c"], "battery_pct": p["pct"]})
        h, last = (self.clock.monotonic() - t0) / 3600.0, rows[-1]["battery_pct"] if rows else None
        rate = round((bat - last) / h, 1) if h and None not in (bat, last) else None
        self.ses is ses and self._close(ses)                       # then 60 s idle after the teardown
        idle = [dict(self._power(), runtime_status=self._pm()["runtime_status"]) for i in (0, 1) if self._sleep(IDLE_S * i) is None]
        self._chk("A7", "PASS" if rows and all(r["ok"] for r in rows) else "WARN", "%s s: fps %s, battery %s %%/h, temperature %s C; idle USB "
                  "input %s -> %s mA, runtime_status %s" % (self.args.soak, [r["fps_in"] for r in rows], rate, [r["temp_c"] for r in rows], idle[0][
                      "iin_ma"], idle[1]["iin_ma"], idle[1]["runtime_status"]), samples=rows, battery_pct_per_h=rate, idle=idle)
    def _a8(self, ses):
        want, ses.dump, man = self.args.dump, [], []
        self._stream(ses, max(5.0, want / 5.0), until=lambda st: len(ses.dump) >= want)
        for i, (raw, seq, health) in enumerate(ses.dump):
            plane, px = raw[self.phase::2], self._pre()(raw, self.phase, self.col0)
            files = {"raw_%d.bin" % i: raw, "odd_%d.pgm" % i: (400, raw[1::2]), "even_%d.pgm" % i: (400, raw[0::2]), "pre_%d.pgm" % i: (400, px),
                     "pre_%d.jpg" % i: self._enc() and self.enc.encode_gray(px)}
            for side, c in (("left", 0), ("right", 200)):
                files["%s_%d.pgm" % (side, i)] = (200, b"".join(plane[r * 400 + c:r * 400 + c + 200] for r in range(400)))
            for name, data in filter(lambda kv: kv[1], files.items()):
                atomic_write(os.path.join(self.images, name), b"P5\n%d 400\n255\n" % data[0] + bytes(data[1]) if isinstance(data, tuple) else data)
            man.append({"i": i, "seq": seq, "health": health, "files": sorted(k for k, v in files.items() if v)})
        atomic_write(os.path.join(self.images, "manifest.json"), _bytes(json.dumps({"strategy": ses.rung, "xu_len": ses.n, "phase": self.phase,
                                                                                    "col0": self.col0, "frames": man}, indent=1)))
        self._chk("A8", "PASS" if len(man) == want else "WARN", "%d of %d frames in %s" % (len(man), want, self.images), frames=len(man), dir=self.images)
        ses.dump = None
    def _a10(self, ses):                        # one REG_WRITE to each IR current register (05-07, the session's value) while streaming
        t, ack = self.clock.monotonic(), {}
        for reg in (0x05, 0x06, 0x07):
            try:
                ack["%02x" % reg] = ses.drv.xu(uvc.reg_write(reg, self.cur, ses.n), 1000)[0]
            except (XuTimeout, XuError) as e:
                ack["%02x" % reg] = "error: %s" % e
        ms, st = round((self.clock.monotonic() - t) * 1000, 1), self._stream(ses, 3.0)
        self._chk("A10", "PASS" if st.best >= OK_S else "WARN", "REG_WRITE 05-07=%02x while streaming: %s in %s ms, then %d ok frames (ok "
                  "run %.1f s)" % (self.cur, _kv(ack), ms, st.ok, st.best), ack=ack, ms=ms, after=dict(st.kinds))
    def _a12(self):                             # decision 33, judged on the last clean Block C of the run (tdir)
        t = self.tdir
        if t is None:
            return
        rb, b, a = t["pwm_readback"], t["current_before"], t["current_after"]
        vals = list(rb) if isinstance(rb, (list, tuple)) else []
        blind = self.pwm_set and not self.pwm_shown     # a session set the PWM, but no REG_READ of 02-04 (A2/A11, teardowns) read it
        why = "cannot verify: REG_READ never read 02-04 as non-zero, so 0 proves nothing" if blind and vals == [0, 0, 0] else "IR off" \
            if vals == [0, 0, 0] else "IR still on" if any(isinstance(v, int) and v for v in vals) else "cannot verify: REG_READ did not answer"
        self.report["teardown_ir"] = t
        self._chk("A12", "PASS" if why == "IR off" else "WARN", "%s: PWM 02-04 read back %s after the OFF writes (%s); information only: USB "
                  "input %s -> %s mA, battery %s -> %s uA (before: while streaming)" % (why, rb, "trailing MAGIC" if t["magic3"] else
                                                                                       "no trailing MAGIC", b["iin_ma"], a["iin_ma"], b["battery_ua"],
                                                                                       a["battery_ua"]),
                  None if why == "IR off" else A12_MAGIC3_HINT if t["magic3"] else A12_UNREAD_HINT if why[0] == "c" else None, **t)
