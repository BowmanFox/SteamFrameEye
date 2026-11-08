# SPDX-License-Identifier: MIT
"""The FrameFace service (spec §4.1, §4.6, §4.14, §4.17-§4.20).

One supervisor tick every 100 ms: suspend detection, the 1 Hz presence scan and disabled flag, the
2 Hz worn poll, Type-C and battery every 10 s, the HTTP bind retry, then the state machine of §4.6
with `run_decision` (§4.14) deciding when the tracker runs and when frames are published. The USB
half (OPENING .. STREAMING, health gate, recovery) is supervisor.UsbSupervisor.

Threaded mode (production): run(stop) ticks in the calling thread; the driver's reaper thread, one
pixel thread per stream and the HTTP threads do the rest. Deterministic mode (threaded=False,
tests): step(dt) advances the clock in slices of at most 10 ms and runs, per slice, the tick when it
is due, driver.pump(0) and one pixel-stage iteration; the driver pumps inline while it sleeps.
stop() (or a SIGTERM through request_stop()) abandons an activation at the next control-transfer
boundary, except inside the cadence window (MAGIC #2 .. STREAM_ON's SET_CUR), where it waits for that
SET_CUR within the §4.6 stop budget (vft, xublocks); it runs Block C whenever a session is open, then
closes HTTP. The Service never takes the flock and writes only below paths.state_dir.
"""
import collections
import datetime
import math
import os
import platform
import sys
import threading
import time

from . import __version__
from .clock import RealClock
from .jpeg import JpegEncoder, JpegError, validate_jpeg
from .logutil import Log
from .mailbox import Latest
from .policy import AbsenceTracker, BrownoutTracker, RunDecision, RunInputs, display, parse_cpus, run_decision
from .preprocess import make_preprocessor
from .server import StreamServer, port_owners
from .state import Store
from .pixels import PixelStage
from .statusdoc import build_status, display_ctx
from .supervisor import UsbSupervisor
from .sysfs import kind, read_battery, read_power, read_typec, scan
from .usbfs import DeviceGone, errno_name
from .worn import WornMonitor

__all__ = ["Service", "TICK_S", "STEP_S", "PORT_RETRY_S"]

TICK_S = 0.1
STEP_S = 0.01
PORT_RETRY_S = 30.0
SUSPEND_JUMP_S = 2.0
SUMMARY_S = 60.0
STOP_WAIT_S = 10.0
STREAMING = ("STARTING", "STREAMING")


def _iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _r(v, nd=1):
    return None if v is None else round(v, nd)


class Service(UsbSupervisor, PixelStage):
    def __init__(self, cfg, paths, sys=None, clock=None, sys_root="/sys", dev_root="/dev", proc_root="/proc",
                 encoder=None, pre=None, worn=None, threaded=True, http=True, log=None, bind=None):
        self.cfg, self.paths, self.threaded = cfg, paths, threaded
        self.clock = clock if clock is not None else RealClock()
        self.sys_root, self.dev_root, self.proc_root = sys_root, dev_root, proc_root
        self.log = log if log is not None else Log(cfg.log_level, paths.events, clock=self.clock)
        self._usb_sys = sys
        self.store = Store(paths, self.clock)
        self.library_error = None
        if encoder is None:
            try:
                encoder = JpegEncoder.load(quality=cfg.jpeg_quality)
            except JpegError as e:
                encoder, self.library_error = None, str(e)
        self.encoder = encoder
        self.pre = pre if pre is not None else make_preprocessor(None, cfg.preprocess, self.log)
        if worn is None:
            worn = WornMonitor(clock=self.clock, worn_timeout_s=cfg.worn_timeout_s)
        self._worn_src = worn.poll if hasattr(worn, "poll") else worn
        self.jpegs = Latest(self.clock)
        self.placeholder = self._make_placeholder()
        self.bind = tuple(bind) if bind is not None else ("0.0.0.0", cfg.http_port)
        self.server = StreamServer(cfg, self.jpegs, self.status, self.clock, self.placeholder, self.log,
                                   self.bind) if http else None
        self.col0 = 200 if cfg.camera == "right" else 0
        self.trace_note = None                  # tests/selftest/sim: kernel.note(strategy=, n=, set_config=; stop=)
        self.client_override = None             # tests: a stream-client count instead of the server's
        self.inline_lag = 0.0                   # tests: extra seconds after each inline pump (a late reaper)
        self.lock_info = "none"                 # the CLI sets "ok" once it holds the flock
        self.state, self.state_t, self.states = "INIT", self.clock.monotonic(), []
        self.node = self.desc = self.enum_bt = self.ident = self.driver = self.act = None
        self._opening = self._closing = None    # the driver in OPENING / in a Block C (request_stop reaches both)
        self.present, self.data_role, self.pm = False, None, {}
        self.battery = {"pct": None, "charging": None}
        self.worn = self.unworn_since = self.last_client_t = None
        self.flag_absent = True
        self.decision, self.publish = RunDecision("idle", False, ""), False
        self.rung = self.rung_source = self.pass_ladder = self.proven = None   # proven: (rung, s), see _end_pass
        self.tried, self.ladder_restarts, self.cxu, self.soft_in_row, self.fails_in_row = set(), 0, 0, 0, 0
        self.settle_mult, self.backoff_until, self.settle_left = 1.0, -math.inf, 0.0
        self.reset_allowance, self.dirty_start, self.start_t, self.start_bt = 0, False, None, None
        self.level, self.trial, self.dark_suppressed_until, self.inherit_seen_ok = None, False, -math.inf, False
        self.brown, self.absence = BrownoutTracker(), AbsenceTracker()
        self.n_urbs, self.grown, self.saved_pair = cfg.n_urbs, False, False
        self.phase, self.layouts, self.plane_info, self.h2_hinted = None, [], None, False
        self.counts = collections.Counter()
        self.activations, self.decisions, self.failures, self.events, self.drivers = [], [], [], [], []
        self.fault = self.port_fault = self.owner = self.fault_detail = None
        self._fault_absent = self._fault_replug = False
        self.overlay, self.overlay_until, self.last_failure, self.next_rung = "", -math.inf, None, None
        self.published = self.jpeg_bad = self.frames_done = 0
        self.pre_ms = self.jpeg_ms = self.jpeg_kb = None
        self.http_port = self.http_retry_t = self.reset_until = None
        self._last_profile = None
        self._profile_lock, self._profile_updates = threading.Lock(), {}
        self._stop_req, self._done = threading.Event(), threading.Event()
        self._running = self._started = self._shut = self._in_tick = False
        self._run_thread = None
        self._next_tick = self._next_scan = self._next_worn = self._next_power = -math.inf
        self._next_summary = math.inf
        self._last_d = None
        self._logged_once, self._htc_logged = set(), set()
        self.errors = []

    # -- small helpers

    @property
    def usb_sys(self):
        if self._usb_sys is None:
            from .usbfs import LinuxSys
            self._usb_sys = LinuxSys()
        return self._usb_sys

    @property
    def healthy(self):
        return self.act.healthy if self.act is not None else 0

    def _jpeg_name(self):
        return getattr(self.encoder, "api", "stub") if self.encoder is not None else "missing"

    def _make_placeholder(self):
        if self.encoder is None:
            return None
        try:
            data = self.encoder.encode_gray(bytes(160000))
        except (JpegError, ValueError) as e:
            self.log.w("http", "placeholder encode failed", error=str(e))
            return None
        return data if validate_jpeg(data) else None

    def _once(self, key, component, event, **kv):
        if key not in self._logged_once:
            self._logged_once.add(key)
            self.log.w(component, event, **kv)

    def _priority(self, what, cpus, nice):
        """CPU affinity and nice of the calling thread (Linux; failures logged once, never fatal)."""
        if not sys.platform.startswith("linux"):
            return
        try:
            cpu_set = parse_cpus(cpus)
            if cpu_set:
                os.sched_setaffinity(0, cpu_set)
        except (OSError, ValueError, AttributeError) as e:
            self._once("affinity:" + what, "service", "cpu affinity failed", thread=what, error=str(e))
        if nice:
            try:
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), int(nice))
            except (OSError, AttributeError) as e:
                self._once("nice:" + what, "service", "nice failed", thread=what, nice=nice, error=str(e))

    def _raise_nice_limit(self):
        try:
            import resource
            soft, hard = resource.getrlimit(resource.RLIMIT_NICE)
            resource.setrlimit(resource.RLIMIT_NICE, (hard, hard))
        except (ImportError, AttributeError, ValueError, OSError) as e:
            self._once("rlimit", "service", "RLIMIT_NICE not raised", error=str(e))

    def _set_state(self, s, why=""):
        if s == self.state:
            return
        prev, self.state, self.state_t = self.state, s, self.clock.monotonic()
        self.states.append((s, self.state_t))
        del self.states[:-4000]
        self.log.i("usb", "state from=%s to=%s" % (prev, s), why=why or "-")

    def _set_publish(self, on, why):
        if on != self.publish:
            self.publish = on
            self.log.i("policy", "publish %s" % ("on" if on else "off"), why=why or "-")

    def _clients(self):
        if self.client_override is not None:
            return int(self.client_override)
        if self.server is not None and self.http_port:
            return self.server.stream_count()
        return 0

    def _store_warnings(self):
        """Store collects load problems (a corrupt runtime.json or profile.json) instead of logging; they
        are logged (repeats suppressed for 10 s) and dropped, so a file re-read every tick never piles up."""
        n = len(self.store.warnings)            # other threads may append meanwhile: those stay for the next call
        new = self.store.warnings[:n]
        del self.store.warnings[:n]
        for w in new:
            self.log.w("state", w[len("state: "):] if w.startswith("state: ") else w)

    def _battery_low(self):
        pct, charging = self.battery.get("pct"), self.battery.get("charging")
        return pct is not None and self.cfg.min_battery_pct > 0 and pct < self.cfg.min_battery_pct and charging is not True

    # -- lifecycle

    def startup(self):
        """Once, before the first tick: dirty-start detection, the start line, FAULT(library), the bind."""
        if self._started:
            return
        self._started = True
        self.start_t, self.start_bt = self.clock.monotonic(), self.clock.boottime()
        self._last_d = self.start_bt - self.start_t
        rt = self.store.load_runtime()
        self.dirty_start = self.store.same_boot(rt) and rt.dirty
        self.reset_allowance = 1 if self.dirty_start else 0
        self._store_warnings()
        self.log.i("service", "start", version=__version__, python=platform.python_version(), pixels=self.pre.name,
                   jpeg=self._jpeg_name(), port=self.bind[1], lock=self.lock_info)
        self.log.event("start", version=__version__, pixels=self.pre.name, jpeg=self._jpeg_name(),
                       dirty_start=self.dirty_start)
        if self.dirty_start:
            self.log.w("service", "dirty start: settle from start and one port reset allowed")
        self._set_state("NO_DEVICE", "start")
        if self.encoder is None:
            self._enter_fault("library", math.inf, detail=self.library_error)
        if self.server is not None:
            self._http_bind()
        self._next_summary = self.start_t + SUMMARY_S

    def run(self, stop: threading.Event) -> int:
        self._run_thread, self._running = threading.current_thread(), True
        self._done.clear()
        try:
            if self.threaded and sys.platform.startswith("linux"):
                self._raise_nice_limit()
                self._priority("main", self.cfg.cpu_io, 0)
            self.startup()
            while not stop.is_set() and not self._stop_req.is_set():
                if not self.threaded:
                    self.step(TICK_S)
                    continue
                t0 = time.monotonic()
                self._guarded(self._tick)
                rest = TICK_S - (time.monotonic() - t0)
                if rest > 0:
                    stop.wait(rest)
        finally:
            self._shutdown()
            self._running = False
            self._done.set()
        return 0

    def step(self, dt: float) -> None:
        """Deterministic mode: advance the clock by dt in slices of at most 10 ms; per slice the tick
        (when due), driver.pump(0) and one pixel-stage iteration."""
        self.startup()
        left = max(0.0, float(dt))
        while not self._shut:
            chunk = min(left, STEP_S)
            if chunk > 0:
                self.clock.sleep(chunk)
            left -= chunk
            self._guarded(self._iteration)
            if self._stop_req.is_set() or left <= 1e-9:
                break
        if self._stop_req.is_set() and not self._in_tick:
            self._shutdown()

    def request_stop(self) -> None:
        """Ask the service to stop. Thread-safe; never call it from a Python signal handler, because it
        sets Events and takes their locks: cli._install_stop's "stop-signals" thread calls it. An
        activation is abandoned at the next control-transfer boundary (one stopped between MAGIC #2 and
        STREAM_ON's SET_CUR first sends that SET_CUR, within the §4.6 stop budget); run()/step() then run
        Block C and close HTTP."""
        self._stop_req.set()
        for drv in (self.driver, self._opening, self._closing):
            if drv is not None:
                drv.abort.set()
        self._note(stop=True)                   # after the aborts: a stop noted before Block C is one its driver saw

    def stop(self) -> None:
        self.request_stop()
        if self._running and threading.current_thread() is not self._run_thread:
            self._done.wait(STOP_WAIT_S)
        elif not self._running and not self._in_tick:
            self._shutdown()

    def _shutdown(self):
        if self._shut:
            return
        self._shut = True
        self._set_publish(False, "stop")
        if self.driver is not None:
            self.driver.abort.set()             # the stop rules of Block C (§4.6), also when run() ended on `stop`
            self._note(stop=True)               # before request_stop() reached the driver
            self._teardown("stop")
        if self.server is not None:
            try:
                self.server.stop()
            except OSError:
                pass
        self.log.i("service", "stop")
        self.log.event("stop")
        self._set_state("STOPPED", "stop")

    def _guarded(self, fn):
        self._in_tick = True
        try:
            fn()
        except Exception as e:                  # the service never dies on a bug of one tick
            self.errors.append(e)
            del self.errors[:-100]
            self.log.e("service", "internal error", error=repr(e))
        finally:
            self._in_tick = False

    def _iteration(self):
        now = self.clock.monotonic()
        if now >= self._next_tick:
            self._next_tick = now + TICK_S
            self._tick()
        drv, a = self.driver, self.act
        if drv is None or a is None or a.pool is None or self.state not in STREAMING:
            return
        try:
            drv.pump(0)
        except DeviceGone:
            return self._lost("gone")
        if self.inline_lag:
            self.clock.sleep(self.inline_lag)
        got = a.pool.take(0)
        if got is not None:
            self._process(a, *got)

    # -- the tick

    def _tick(self):
        now, bt = self.clock.monotonic(), self.clock.boottime()
        if self._last_d is not None and (bt - now) - self._last_d > SUSPEND_JUMP_S:
            self._last_d = bt - now
            return self._on_suspend()
        self._last_d = bt - now
        if now >= self._next_scan:
            self._next_scan = now + 1.0
            self._scan(bt)
            self.flag_absent = not os.path.exists(self.paths.disabled_flag)
        if now >= self._next_worn:
            self._next_worn = now + 0.5
            self._poll_worn(now)
        if now >= self._next_power:
            self._next_power = now + 10.0
            self._poll_power()
        clients = self._clients()
        if clients:
            self.last_client_t = now
        if self.server is not None and self.http_port is None and self.http_retry_t is not None and \
                now >= self.http_retry_t and not (self.fault and self.fault["kind"] == "port"):
            self._http_bind()
        self._apply_profile_updates()
        self._store_warnings()
        if now >= self._next_summary:
            self._next_summary = now + SUMMARY_S
            if self.state == "STREAMING":
                self._summary(clients)
        st = self.state
        if st == "STOPPED":
            return
        if st == "FAULT":
            return self._fault_tick(now)
        if not (self.cfg.enabled and self.flag_absent):
            if st != "DISABLED":
                if self.driver is not None:
                    self.log.i("policy", "teardown", why="disabled")
                    self._teardown("disabled")
                self.node = None
                self._set_state("DISABLED", "disabled")
            return
        if st == "DISABLED":
            self._next_scan = now
            return self._set_state("NO_DEVICE", "enabled")
        if st == "RESETTING":
            if now >= self.reset_until:
                self._set_state("NO_DEVICE", "reset done")
            return
        streaming = st in STREAMING
        settle_ok = False
        if self.node is not None and not streaming:
            deadline = self._deadline(bt)
            settle_ok, self.settle_left = bt >= deadline, max(0.0, deadline - bt)
        dec = run_decision(RunInputs(True, self.node is not None, self._battery_low(), self.worn, clients, streaming,
                                     now, self.last_client_t, self.unworn_since, settle_ok), self.cfg)
        self.decision = dec
        self._set_publish(dec.publish, dec.why)
        if streaming:
            if dec.usb == "stop":
                self.log.i("policy", "teardown", why=dec.why)
                self._teardown(dec.why)
                return self._set_state("IDLE", dec.why)
            if self.driver is not None:
                self._watchdogs(now)
            return
        wanted = dec.usb == "start" or dec.why == "settling"
        if st == "BACKOFF":
            if not wanted:
                return self._set_state("IDLE", dec.why)
            if bt < self.backoff_until:
                return
            self._set_state("SETTLING", "backoff done")
            st = "SETTLING"
        if st == "IDLE" and wanted:
            self._set_state("SETTLING", dec.why or "start")
            st = "SETTLING"
        if st == "SETTLING":
            if dec.usb == "start":
                return self._open_activate()
            if not wanted:
                self._set_state("IDLE", dec.why)

    def _on_suspend(self):
        """boottime jumped past monotonic: the tracker state is unknown, Block C (LOST on error), reopen."""
        self.events.append("suspend")
        del self.events[:-2000]
        self.log.w("service", "suspend detected")
        self.log.event("suspend")
        if self.driver is not None:
            self._teardown("suspend")
            self._set_state("SETTLING", "suspend")

    def _scan(self, bt):
        try:
            nodes = scan(self.sys_root, self.dev_root)
        except OSError:
            nodes = []
        vft = next((n for n in nodes if kind(n) == "vft"), None)
        self.absence.scan(bt, vft is not None)
        self.present = vft is not None
        for n in nodes:
            if kind(n) == "htc" and n.pid not in self._htc_logged:
                self._htc_logged.add(n.pid)
                self.log.w("usb", "HTC device %04x: firmware mode 'HTC Boot'?" % n.pid)
        if self.state == "FAULT":
            if vft is None:
                self._fault_absent = True
            elif self._fault_absent:
                self._fault_replug = True
            return
        if vft is None:
            if self.driver is None and self.state not in ("RESETTING", "STOPPED"):
                self.node = None
                if self.state in ("IDLE", "SETTLING", "BACKOFF"):
                    self._set_state("NO_DEVICE", "gone")
            if self.state == "NO_DEVICE" and any(kind(n) == "focus3" for n in nodes):
                self.fault_detail = "VIVE Focus facial tracker is not supported"
                self._enter_fault("unsupported", 60.0, detail=self.fault_detail)
            return
        if self.state == "NO_DEVICE":
            self.node, self.enum_bt, self.desc = vft, bt, vft.descriptors
            self.log.i("usb", "device found", sysname=vft.sysname, node=vft.devpath, speed=vft.speed_mbps,
                       hub="yes" if vft.behind_hub else "no")
            self.log.event("device_found", sysname=vft.sysname, speed=vft.speed_mbps, hub=vft.behind_hub)
            self._set_state("IDLE", "found")
        elif self.driver is None and self.state not in ("RESETTING", "DISABLED", "STOPPED"):
            self.node, self.desc = vft, vft.descriptors

    def _poll_worn(self, now):
        try:
            w = self._worn_src()
        except Exception as e:                  # fail-open
            self._once("worn", "policy", "worn source failed", error=repr(e))
            w = None
        if w is False:
            if self.worn is not False:
                self.unworn_since = now
        else:
            self.unworn_since = None
        self.worn = w

    def _poll_power(self):
        self.battery = read_battery(self.sys_root)
        self.data_role = read_typec(self.sys_root)["data_role"]
        if self.node is not None:
            self.pm = read_power(self.sys_root, self.node.sysname)

    def _summary(self, clients):
        a = self.act
        asm = a.asm if a is not None else None
        rates = self.server.rates() if self.server is not None and self.http_port else {"kbps": 0, "fps_out": 0.0}
        bad = sum(asm.stats[k] for k in ("bad_size", "bad_err", "bad_hdr", "overflow")) if asm else 0
        usb = self.driver.stats if self.driver is not None else {}
        self.log.i("stream", "summary", fps_in=_r(asm.fps.fps_in) if asm else 0, fps_out=rates["fps_out"],
                   kbps=rates["kbps"], q=self.cfg.jpeg_quality, bad=bad, testbar=self.counts["testbar"],
                   dark=self.counts["dark"], frozen=self.counts["frozen"], clients=clients,
                   inflight_min=usb.get("inflight_min"), reap_batch_max=usb.get("reap_batch_max"))

    # -- FAULT and HTTP

    def _enter_fault(self, kind_, delay_s, hint="", detail=None, owner=None):
        now = self.clock.monotonic()
        self.fault = {"kind": kind_, "since": now, "until": now + delay_s, "hint": hint, "detail": detail}
        if owner is not None:
            self.owner = owner
        self._fault_absent = self._fault_replug = False
        name, _, _, text = display("FAULT", self._ctx(now, 0))
        self.log.e("http" if kind_ == "port" else "usb", "fault", kind=kind_, hint=text)
        self.log.event("fault", kind=kind_, retry_s=None if math.isinf(delay_s) else delay_s, detail=detail)
        self._set_state("FAULT", kind_)

    def _fault_tick(self, now):
        f = self.fault
        if f["kind"] == "port":
            if self.http_retry_t is not None and now >= self.http_retry_t:
                self._http_bind()
            return
        if now >= f["until"] or (f["kind"] in ("wedged", "no_ir") and self._fault_replug):
            self._clear_fault("replug" if self._fault_replug else "retry")

    def _clear_fault(self, why):
        """A retry starts a fresh ladder pass; the reset allowance and the session IR level stay."""
        self.log.i("usb", "fault retry", kind=self.fault["kind"], why=why)
        self.fault = self.owner = self.fault_detail = None
        self.pass_ladder, self.rung, self.tried, self.ladder_restarts = None, None, set(), 0
        self.cxu = self.soft_in_row = 0
        self.settle_mult = 1.0
        self.last_failure = self.next_rung = None
        self.node = None
        self._next_scan = self.clock.monotonic()
        self._set_state("NO_DEVICE", why)

    def _http_bind(self):
        port = self.bind[1]
        try:
            self.http_port = self.server.start()
        except OSError as e:
            self.http_port = None
            own = [(p, c) for p, c in port_owners(port, self.proc_root) if p != os.getpid()]
            owner = "%s pid %d" % (own[0][1], own[0][0]) if own else None
            name = errno_name(e)
            self.log.e("http", "bind failed", port=port, errno=name, owner=owner)
            self.port_fault = {"kind": "port", "port": port, "errno": name, "owner": owner, "since": _iso()}
            try:
                self.store.set_fault(self.port_fault)
            except OSError:
                pass
            self.http_retry_t = self.clock.monotonic() + PORT_RETRY_S
            if self.state != "FAULT":
                self._teardown("port")
                self._enter_fault("port", PORT_RETRY_S, owner=owner, detail=name)
            return False
        self.http_retry_t = None
        rt = self.store.load_runtime()
        if self.port_fault is not None or (rt.fault and rt.fault.get("kind") == "port"):
            try:
                self.store.set_fault(None)
            except OSError:
                pass
        self.port_fault = None
        self.log.i("http", "listening", host=self.bind[0], port=self.http_port)
        if self.fault and self.fault["kind"] == "port":
            self.owner = None
            self._clear_fault("bound")
        return True

    # -- /status (§4.18): statusdoc

    def _ctx(self, now, nclients):
        return display_ctx(self, now, nclients)

    def status(self) -> dict:
        return build_status(self)
