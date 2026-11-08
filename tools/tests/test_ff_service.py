# SPDX-License-Identifier: MIT
"""The service runtime in deterministic mode (spec §11.3 test_ff_service, task T4): inline Service on
SimClock with FakeKernel/FakeVft, StubPreprocessor and StubJpegEncoder. Clients and the worn signal
are injected; HTTP runs only in the bind-failure case (127.0.0.1, a free port held by the test).
The driver-matrix scenarios through this Service are in test_ff_service_matrix."""
import ffutil  # first: installs the port guard

import json
import os
import socket
import unittest
from unittest import mock

ffutil.setup_path()
from test_ff_driver_matrix import actions, healthy, ops, rungs  # noqa: E402
from ffsup import RigCase  # noqa: E402
from frameface.config import Config, temp_paths  # noqa: E402
from frameface.descriptors import sha1_hex  # noqa: E402
from frameface.jpeg import StubJpegEncoder  # noqa: E402
from frameface.logutil import Log  # noqa: E402
from frameface.policy import Failure as F  # noqa: E402
from frameface.preprocess import StubPreprocessor  # noqa: E402
from frameface.service import Service  # noqa: E402
from frameface.sim import VftModel, check_trace, make_rig  # noqa: E402
from frameface.state import Profile, Store  # noqa: E402
from frameface.vft import VftDriver  # noqa: E402

# The /status example of spec §4.18 (every key must always be present).
STATUS_EXAMPLE = json.loads("""
{"v":1,
 "service":{"version":"0.6.0","uptime_s":812.3,"pid":1234,"python":"3.12.3","pixels":"c","jpeg":"tj3","selftest":0},
 "state":"STREAMING","display":"streaming","severity":"ok","message":"Streaming 59 fps to 1 client","hint":"",
 "since_s":600.1,
 "policy":{"enabled":true,"power_policy":"auto","worn":true,"clients":1,"publish":true,"why":""},
 "device":{"present":true,"node":"/dev/bus/usb/001/003","sysname":"1-1","speed":480,"hub":false,"writable":true,
           "get_len":64,"xu_unit":4,"ep":129,"max_packet":512,"bcd_uvc":"0x0110","desc_sha1":"...",
           "data_role":"host","pm":{"control":"on","runtime_status":"active"}},
 "negotiated":{"probe_hex":"...","probe_len":34,"max_frame":320000,"max_payload":32768,"interval":166666,
               "framing_info":0},
 "strategy":{"name":"S0","source":"profile","ladder_pos":0,"tried":["S0"],"xu_len":64,"set_config":"skipped"},
 "ir":{"level":"L0","pwm":255,"current":178,"reason":"","trial":false},
 "stream":{"fps_in":59.8,"fps_pts":59.9,"fps_out":41.0,"frames":36000,"published":24600,
           "bad":{"size":1,"err":0,"hdr":0,"overflow":0,"fid_breaks":0,"size_614400":0,"dropped_busy":3},
           "testbar":0,"dark":0,"frozen":0,"y_phase":"odd",
           "plane":{"verdict":"identical","mad":0.0,"std_odd":31.2,"std_even":31.2,"h2_hint":false},
           "assembler":{"mode":"strict","stride":32768,"stride_learned":false},
           "kbps":4980,"jpeg_kb":12.1,"quality":92,"pre_ms":0.9,"jpeg_ms":1.1,"jpeg_bad":0,"jpeg_warn":0,
           "placeholders":0,"keepalives":12},
 "usb":{"urbs":16,"urb_bytes":32768,"inflight_min":13,"reap_batch_max":2,
        "errors":{"epipe":0,"eproto":0,"eilseq":0,"etime":0,"eoverflow":0,"other":0}},
 "recovery":{"activations":2,"soft_reopens":0,"resets":0,"unplugs":0,"brownout_suspects":0,"consecutive_failures":0,
             "backoff_s":0,"settle_left_s":0,"dirty_start":false},
 "power":{"battery_pct":71,"charging":false},
 "clients":[{"peer":"10.35.78.28","since_s":640,"sent":24600,"skipped":11400}],
 "events":["2026-11-06T10:00:01 activation strategy=S0 ms=980", "..."]}
""")


def key_tree(d):
    return {k: key_tree(v) if isinstance(v, dict) else None for k, v in d.items()}


class SvcCase(RigCase):
    """Rigs and inline Services in temp directories (shared with test_ff_service_matrix)."""

    def service(self, cfg=None, profile=None, rig=None, paths=None, clients=1, worn=True, http=False, bind=None,
                encoder=None, pre=None, **knobs):
        if rig is None:
            rig = make_rig(VftModel(**knobs))
            self.addCleanup(rig.close)
        paths = paths or temp_paths(self.tmpdir())
        if profile:
            Store(paths, rig.clock).save_profile(Profile(desc_sha1=sha1_hex(rig.vft.descriptors), **profile))
        self.worn = {"v": worn}
        log = Log("debug", paths.events, journal=True, stream=None, clock=rig.clock, keep=200)
        svc = Service(cfg if cfg is not None else Config(token=""), paths, sys=rig.kernel, clock=rig.clock,
                      sys_root=rig.sys_root, dev_root=rig.dev_root, proc_root=rig.proc_root,
                      encoder=encoder if encoder is not None else StubJpegEncoder(),
                      pre=pre if pre is not None else StubPreprocessor(),
                      worn=lambda: self.worn["v"], threaded=False, http=http, log=log, bind=bind)
        svc.trace_note = rig.kernel.note
        svc.client_override = clients
        svc.k = rig.kernel                      # the attribute T2's matrix helpers (ops) read
        self.addCleanup(svc.stop)
        return rig, svc

    @staticmethod
    def run_for(svc, seconds, until=None, stop_at=("FAULT", "STOPPED")):
        end = svc.clock.monotonic() + seconds
        while svc.clock.monotonic() < end and svc.state not in stop_at:
            if until is not None and until(svc):
                return True
            svc.step(0.01)
        return bool(until(svc)) if until is not None else None

    def stream_ok(self, svc, seconds=30, n=60):
        self.assertTrue(self.run_for(svc, seconds, healthy(n)), "not streaming: %s %s" % (svc.states[-3:],
                                                                                         svc.failures))

    def check(self, rig, svc):
        self.assertEqual(rig.kernel.violations, [])
        self.assertEqual(check_trace(rig.kernel.ops), [])
        self.assertEqual(svc.errors, [])

    def done(self, rig, svc):
        svc.stop()
        self.assertEqual((svc.state, svc.store.load_runtime().dirty), ("STOPPED", False))
        self.check(rig, svc)

    def profile(self, rig, svc):
        return svc.store.load_profile(sha1_hex(rig.vft.descriptors))


def teardown_xu(svc, since):
    """(name, reg, val) of the XU SET_CURs after `since` up to the first DISCARDURB."""
    out = []
    for op in svc.k.ops:
        if op["t"] < since:
            continue
        if op["kind"] == "discard":
            break
        if op["kind"] == "ctrl" and op["name"] in ("MAGIC", "STREAM_OFF", "STREAM_ON", "REG_WRITE"):
            out.append((op["name"], op.get("reg"), op.get("val"), op["t"]))
    return out


OFF_SEQUENCE = ["MAGIC", "STREAM_OFF", "MAGIC"] + ["REG_WRITE"] * 11


class IdlePolicy(SvcCase):
    def test_start_needs_a_client_and_worn(self):
        rig, s = self.service(clients=0)
        self.run_for(s, 10)
        self.assertEqual((s.state, s.activations, s.status()["display"]), ("IDLE", [], "waiting_client"))
        self.worn["v"] = False                  # polled at 2 Hz: off before the client arrives
        self.run_for(s, 1)
        s.client_override = 1
        self.run_for(s, 5)
        st = s.status()
        self.assertEqual((s.state, s.activations, st["display"], st["policy"]["why"]), ("IDLE", [], "standby",
                                                                                        "not_worn"))
        self.worn["v"] = None                   # unknown worn (no eye server): fail-open
        self.assertTrue(self.run_for(s, 3, lambda s: s.state == "STREAMING"))
        self.worn["v"] = True
        self.stream_ok(s)
        self.assertEqual((len(s.activations), s.publish, s.status()["display"]), (1, True, "streaming"))
        self.done(rig, s)

    def test_idle_teardown_30s_after_the_last_client(self):
        rig, s = self.service()
        self.stream_ok(s)
        s.client_override = 0
        gone = s.clock.monotonic()
        self.assertTrue(self.run_for(s, 40, lambda s: s.state == "IDLE"))
        td = next(t for st, t in s.states if st == "TEARDOWN" and t >= gone)
        self.assertTrue(30.0 <= td - gone < 30.3, td - gone)
        self.assertEqual([x[0] for x in teardown_xu(s, td)], OFF_SEQUENCE)
        self.assertTrue(s.log.has("I policy: teardown why=no_clients"))
        self.assertEqual((s.status()["display"], s.store.load_runtime().dirty), ("waiting_client", False))
        self.assertGreater(s.frames_done, 60)
        self.assertEqual(s.status()["stream"]["frames"], s.frames_done)     # the torn-down stream counted once
        self.done(rig, s)

    def test_unworn_publish_off_at_once_teardown_after_60s(self):
        rig, s = self.service()
        self.stream_ok(s)
        self.assertTrue(s.publish)
        self.worn["v"] = False
        off = s.clock.monotonic()
        self.assertTrue(self.run_for(s, 2, lambda s: not s.publish))
        self.assertLessEqual(s.clock.monotonic() - off, 0.61)          # the next 2 Hz worn poll
        published = s.published
        self.run_for(s, 30)
        self.assertEqual((s.state, s.published), ("STREAMING", published))     # still streaming, never published
        self.assertTrue(self.run_for(s, 40, lambda s: s.state == "IDLE"))
        td = next(t for st, t in s.states if st == "TEARDOWN")
        self.assertTrue(60.0 <= td - off < 60.7, td - off)
        self.assertTrue(s.log.has("I policy: teardown why=not_worn"))
        self.assertEqual(s.status()["display"], "standby")
        self.done(rig, s)

    def test_no_thrash_with_7s_reconnects_for_5_minutes(self):
        rig, s = self.service(fps=15.0)         # the idle policy does not depend on the frame rate
        self.stream_ok(s)
        t0 = s.clock.monotonic()

        def baballonia(s):                      # reconnects every 7 s: 0.5 s without a connection
            s.client_override = 0 if (s.clock.monotonic() - t0) % 7.0 < 0.5 else 1
            return False
        self.run_for(s, 300, baballonia)
        self.assertEqual((len(s.activations), s.state, s.failures), (1, "STREAMING", []))
        self.assertFalse(any(st == "TEARDOWN" for st, _ in s.states))
        self.done(rig, s)

    def test_kill_switch_runs_the_teardown_xu_part_within_1_5_s(self):
        rig, s = self.service()
        self.stream_ok(s)
        os.makedirs(s.paths.config_dir, exist_ok=True)
        with open(s.paths.disabled_flag, "w"):          # `off`
            pass
        flag = s.clock.monotonic()
        self.assertTrue(self.run_for(s, 3, lambda s: s.state == "DISABLED"))
        seq = teardown_xu(s, flag)
        self.assertEqual([x[0] for x in seq], OFF_SEQUENCE)
        self.assertEqual([(r, v) for _, r, v, _ in seq if r in (2, 3, 4)], [(2, 0), (3, 0), (4, 0)])
        self.assertLessEqual(seq[-1][3] - flag, 1.5)
        st = s.status()
        self.assertEqual((st["display"], st["policy"]["enabled"], s.publish), ("disabled", False, False))
        os.remove(s.paths.disabled_flag)        # `on`: streams again
        self.assertTrue(self.run_for(s, 15, lambda s: len(s.activations) == 2 and s.state == "STREAMING"))
        self.done(rig, s)

    def test_battery_low_stops_and_charging_restarts(self):
        rig, s = self.service()
        self.stream_ok(s)
        bat = os.path.join(rig.sys_root, "class", "power_supply", "battery")
        with open(os.path.join(bat, "capacity"), "w") as f:
            f.write("3\n")
        self.assertTrue(self.run_for(s, 11, lambda s: s.state == "IDLE"))
        self.assertTrue(s.log.has("I policy: teardown why=battery"))
        self.assertEqual((s.decision.why, s.status()["power"]), ("battery", {"battery_pct": 3, "charging": False}))
        with open(os.path.join(bat, "status"), "w") as f:
            f.write("Charging\n")
        self.assertTrue(self.run_for(s, 25, lambda s: len(s.activations) == 2 and s.state == "STREAMING"))
        self.done(rig, s)


class Recovery(SvcCase):
    def test_stop_request_during_activating_runs_block_c(self):
        rig, s = self.service()

        def hook(kind, phase):                  # the stop-signals thread's path: request_stop() after REG_WRITE 1
            if kind == "reg_write" and phase == "done" and s.state == "ACTIVATING" and not s._stop_req.is_set():
                s.request_stop()
        rig.vft.on_xu = hook
        self.run_for(s, 30)
        self.assertEqual((s.state, s.store.load_runtime().dirty, len(s.activations)), ("STOPPED", False, 1))
        names = [op["name"] for op in s.k.ops if op["kind"] == "ctrl" and op["name"] in ("MAGIC", "STREAM_OFF",
                                                                                         "STREAM_ON", "REG_WRITE")]
        block_a = ["MAGIC", "STREAM_OFF", "MAGIC"] + ["REG_WRITE"] * 11 + ["MAGIC", "STREAM_ON"]
        self.assertEqual(names, block_a + OFF_SEQUENCE)     # in the cadence window: Block A ends at STREAM_ON first
        self.assertEqual(s.failures, [])        # (a stop before MAGIC #2 abandons it: test_ff_cadence)
        self.assertEqual([op["kind"] for op in s.k.ops if op["kind"] in ("release", "close")][-3:],
                         ["release", "release", "close"])
        self.check(rig, s)

    def test_dirty_start_settles_from_start_and_resets_once(self):
        rig = make_rig(VftModel())
        self.addCleanup(rig.close)
        paths = temp_paths(self.tmpdir())
        Store(paths, rig.clock).mark_dirty()    # the previous process died mid-stream
        rig.vft.wedged = True
        rig, s = self.service(rig=rig, paths=paths)
        self.stream_ok(s, 90)
        self.assertEqual((s.dirty_start, s.reset_allowance, s.counts["resets"]), (True, 0, 1))
        self.assertEqual((s.failures, actions(s), rungs(s)), ([F.XU_TIMEOUT], [("reset_port", 6)], ["S0", "S0"]))
        self.assertGreaterEqual(s.activations[0]["t"] - s.start_bt, 8.0)
        self.assertTrue(s.status()["recovery"]["dirty_start"])
        s.client_override = 0                   # idle, then wedged again: the allowance is gone
        self.assertTrue(self.run_for(s, 40, lambda s: s.state == "IDLE"))
        rig.vft.wedged = True
        s.client_override = 1
        self.assertTrue(self.run_for(s, 30, lambda s: len(s.decisions) == 2))
        self.assertEqual((actions(s)[-1], s.counts["resets"]), (("retry_same", 7), 1))
        self.done(rig, s)

    def test_seen_ok_is_set_by_the_first_ok_frame_and_inherited_by_an_ir_trial(self):
        rig, s = self.service(dark_from_s=20.0)
        self.assertTrue(self.run_for(s, 10, lambda s: s.state == "STARTING"))
        a = s.act
        self.assertEqual((a.seen_ok, a.healthy), (False, 0))
        self.assertTrue(self.run_for(s, 5, lambda s: s.act.healthy >= 1))
        self.assertTrue(a.seen_ok)
        self.assertTrue(self.run_for(s, 60, lambda s: len(s.activations) == 2 and s.state == "STREAMING"))
        trial = s.act
        self.assertIsNot(trial, a)
        self.assertEqual((s.activations[1]["trial"], trial.seen_ok, trial.healthy), (True, True, 0))
        self.assertTrue(self.run_for(s, 30, lambda s: len(s.decisions) == 2))
        self.assertEqual(actions(s), [("ir_trial", 4), ("suppress", 5)])   # row 5 needs the inherited seen_ok
        self.done(rig, s)

    def test_known_good_pair_needs_10_s_and_300_healthy_frames(self):
        for fps, wait in ((60.0, 10.0), (20.0, 15.0)):         # 300 frames: 5 s at 60 fps, 15 s at 20 fps
            rig, s = self.service(fps=fps)
            self.stream_ok(s, n=1)
            t = s.act.stream_t
            self.run_for(s, wait - 0.6 - (s.clock.monotonic() - t))
            self.assertIsNone(self.profile(rig, s), fps)
            self.assertTrue(self.run_for(s, 1.5, lambda s: s.saved_pair), fps)
            self.assertGreaterEqual(s.act.healthy, 300)
            p = self.profile(rig, s)
            self.assertEqual((p.strategy, p.xu_len, p.source), ("S0", 64, "service"))
            self.assertAlmostEqual(p.fps_pts, fps, delta=0.2)
            self.done(rig, s)

    def test_runtime_and_profile_are_written(self):
        rig, s = self.service()
        self.stream_ok(s)
        rt = s.store.load_runtime()
        self.assertTrue(rt.dirty and rt.dirty_since_bt is not None and s.store.same_boot(rt))
        s.client_override = 0
        self.assertTrue(self.run_for(s, 40, lambda s: s.state == "IDLE"))
        rt = s.store.load_runtime()
        self.assertEqual((rt.dirty, rt.last_teardown_bt is not None), (False, True))
        with open(s.paths.events, encoding="utf-8") as f:
            evs = [json.loads(line)["ev"] for line in f]
        self.assertIn("activation", evs)
        self.assertIn("profile", evs)
        self.assertTrue(os.path.exists(s.paths.profile))
        self.done(rig, s)

    def test_corrupt_state_files_are_logged_and_replaced(self):
        rig = make_rig(VftModel())
        self.addCleanup(rig.close)
        paths = temp_paths(self.tmpdir())
        os.makedirs(paths.state_dir)
        for name in (paths.runtime, paths.profile):
            with open(name, "w") as f:
                f.write("{not json")
        rig, s = self.service(rig=rig, paths=paths)
        self.assertTrue(self.run_for(s, 30, lambda s: s.saved_pair))
        self.assertTrue(s.log.has("W state: %s is corrupt" % paths.runtime))
        self.assertTrue(s.log.has("W state: %s is corrupt" % paths.profile))
        self.assertEqual((s.dirty_start, self.profile(rig, s).strategy), (False, "S0"))
        self.done(rig, s)

    def test_an_unusable_runtime_json_does_not_pile_up_warnings_while_idle(self):   # review finding runtime-4
        rig = make_rig(VftModel())
        self.addCleanup(rig.close)
        paths = temp_paths(self.tmpdir())
        os.makedirs(paths.state_dir)
        with open(paths.runtime, "w") as f:
            f.write("{not json")
        rig, s = self.service(rig=rig, paths=paths, clients=0)         # plugged in, nobody connects: idle
        self.run_for(s, 600)
        self.assertEqual(s.state, "IDLE")
        self.assertLessEqual(len(s.store.warnings), 1)                  # re-read every tick, logged, dropped
        lines = [line for line in s.log.lines if line.startswith("W state: %s is corrupt" % paths.runtime)]
        self.assertTrue(1 <= len(lines) <= 61, len(lines))              # the 10 s repeat suppression holds
        self.done(rig, s)

    def test_get_len_16_is_unsupported_without_any_xu_command(self):
        rig, s = self.service(get_len=16)
        self.run_for(s, 20)
        self.assertEqual((s.state, s.fault["kind"], s.status()["display"]), ("FAULT", "unsupported", "unsupported"))
        self.assertIn("cannot carry a 17-byte register write", s.fault_detail)
        self.assertEqual([op for op in ops(s, "ctrl") if op["name"] in ("MAGIC", "REG_WRITE")], [])
        self.check(rig, s)

    def test_frame_index_other_than_1_is_unsupported(self):
        rig = make_rig(VftModel())
        self.addCleanup(rig.close)
        raw = bytearray(rig.vft.descriptors)
        raw[raw.find(bytes.fromhex("1e2405")) + 3] = 2           # FRAME_UNCOMPRESSED bFrameIndex 2
        rig.vft.descriptors = bytes(raw)
        rig.kernel.tree.write_device(rig.kernel)
        rig, s = self.service(rig=rig)
        self.run_for(s, 20)
        self.assertEqual((s.state, s.fault["kind"]), ("FAULT", "unsupported"))
        self.assertIn("frame 2", s.fault_detail)
        self.assertEqual(rungs(s), [])
        self.check(rig, s)

    def test_an_unexpected_error_is_a_fault_with_a_retry_not_a_stuck_state(self):
        for name in ("open", "negotiate"):
            rig, s = self.service()
            with mock.patch.object(VftDriver, name, side_effect=TypeError("a bug in %s" % name)):
                self.run_for(s, 20)
            self.assertEqual((s.state, s.fault["kind"], s.driver), ("FAULT", "unsupported", None), name)
            self.assertEqual(([type(e) for e in s.errors], s.store.load_runtime().dirty), ([TypeError], False), name)
            self.assertAlmostEqual(s.fault["until"] - s.fault["since"], 60.0)
            self.assertEqual((rig.kernel.violations, check_trace(rig.kernel.ops)), ([], []), name)
            s.errors.clear()
            self.assertTrue(self.run_for(s, 90, healthy(30), stop_at=()), name)      # the 60 s retry streams
            self.done(rig, s)

    def test_a_runtime_write_error_never_stops_the_state_machine(self):
        rig, s = self.service()
        full = OSError(28, "No space left on device")
        with mock.patch.object(Store, "mark_dirty", side_effect=full), \
                mock.patch.object(Store, "mark_clean_teardown", side_effect=full):
            self.stream_ok(s)
            s.client_override = 0
            self.assertTrue(self.run_for(s, 40, lambda s: s.state == "IDLE"))
        self.assertTrue(s.log.has("W state: runtime write failed mark=dirty"))
        self.assertTrue(s.log.has("W state: runtime write failed mark=clean_teardown"))
        self.assertEqual((s.errors, s.drivers[-1].teardown_path), ([], "clean"))
        self.done(rig, s)

    def test_library_missing_is_a_permanent_fault(self):
        class NoJpeg(StubJpegEncoder):
            pass
        rig, s = self.service(encoder=NoJpeg())
        s.encoder, s.library_error = None, "libturbojpeg not usable"
        self.run_for(s, 120)
        st = s.status()
        self.assertEqual((s.state, st["display"], st["service"]["jpeg"]), ("FAULT", "library", "missing"))
        self.assertEqual(ops(s, "open"), [])
        self.check(rig, s)


class PortFault(SvcCase):
    def fake_owner(self, rig, port):
        net = os.path.join(rig.proc_root, "net")
        os.makedirs(net, exist_ok=True)
        with open(os.path.join(net, "tcp"), "w") as f:
            f.write("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
                    "   0: 0100007F:%04X 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 "
                    "55555 1 0000000000000000 100 0 0 10 0\n" % port)
        fd = os.path.join(rig.proc_root, "4242", "fd")
        os.makedirs(fd)
        with open(os.path.join(rig.proc_root, "4242", "comm"), "w") as f:
            f.write("holder\n")
        with open(os.path.join(fd, "3"), "w") as f:
            f.write("socket:[55555]")

    def test_bind_failure_is_fault_port_with_a_30_s_retry(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        rig = make_rig(VftModel())
        self.addCleanup(rig.close)
        self.fake_owner(rig, port)
        rig, s = self.service(rig=rig, http=True, bind=("127.0.0.1", port))
        binds = []
        start = s.server.start

        def counted():
            binds.append(s.clock.monotonic())
            return start()
        s.server.start = counted
        self.run_for(s, 25, stop_at=())
        self.assertEqual(s.state, "FAULT")
        st = s.status()
        self.assertEqual((st["display"], st["severity"]), ("port_in_use", "error"))
        self.assertEqual(st["message"], "Face stream port %d is in use" % port)
        self.assertIn("holder pid 4242", st["hint"])
        fault = s.store.load_runtime().fault
        self.assertEqual((fault["kind"], fault["port"], fault["owner"]), ("port", port, "holder pid 4242"))
        self.assertTrue(s.log.has('E http: bind failed port=%d' % port))
        self.assertEqual((ops(s, "open"), len(binds)), ([], 1))             # never opened, one bind so far
        holder.close()
        t_free = s.clock.monotonic()
        self.assertTrue(self.run_for(s, 10, lambda s: s.http_port == port, stop_at=()))
        self.assertAlmostEqual(binds[1] - binds[0], 30.0, delta=0.15)
        self.assertGreater(binds[1], t_free)
        self.assertIsNone(s.store.load_runtime().fault)
        self.stream_ok(s)                        # the process stayed up and now serves
        self.done(rig, s)


class StatusDocument(SvcCase):
    def test_status_has_every_key_and_the_display_mapping(self):
        rig, s = self.service(clients=0)
        st0 = s.status()                        # before the first step: INIT
        self.assertEqual(key_tree(st0), key_tree(STATUS_EXAMPLE))
        self.run_for(s, 3)
        self.assertEqual((s.status()["display"], s.status()["hint"]), ("waiting_client", 'Face Camera Address '
                         'http://10.35.78.1:9023/stream (python3 -I ~/frameface/frameface_main.py url prints it), '
                         '"This is a..." = Wireless/IP Camera'))
        s.client_override = 1
        seen = {}

        def watch(s):
            seen.setdefault(s.status()["display"], s.state)
            return s.state == "STREAMING" and s.healthy > 30
        self.assertTrue(self.run_for(s, 20, watch))
        self.assertIn(seen.get("starting"), ("OPENING", "ACTIVATING", "NEGOTIATING", "STARTING"))
        st = s.status()
        self.assertEqual(key_tree(st), key_tree(STATUS_EXAMPLE))
        self.assertEqual((st["display"], st["severity"], st["state"]), ("streaming", "ok", "STREAMING"))
        self.assertRegex(st["message"], r"^Streaming \d+ fps to 1 client$")
        self.assertEqual((st["device"]["get_len"], st["strategy"]["name"], st["ir"]["level"], st["v"]), (64, "S0",
                                                                                                         "L0", 1))
        self.assertIsInstance(st["clients"], list)
        self.assertTrue(all(isinstance(e, str) for e in st["events"]))
        json.dumps(st)                          # JSON-serialisable (no Infinity, no objects)
        self.done(rig, s)

    def test_display_dark_start_and_test_pattern_name_the_next_rung(self):
        rig, s = self.service(programs_at_len=384)
        self.assertTrue(self.run_for(s, 60, lambda s: s.failures and s.state == "BACKOFF"))
        st = s.status()
        self.assertEqual((st["display"], st["message"]), ("dark_start", "Tracker image black, trying S1"))
        self.assertTrue(s.log.has("W stream: dark from start strategy=S0 xu_len=64 next=S1"))
        rig2, s2 = self.service(profile={"strategy": "S1"}, setif_required="after_commit")
        self.assertTrue(self.run_for(s2, 60, lambda s: s.failures and s.state == "BACKOFF"))
        st = s2.status()
        self.assertEqual((st["display"], st["message"]), ("test_pattern", "Tracker shows test bars, trying S0"))
        self.assertTrue(s2.log.has("W stream: test bars count=30 strategy=S1 next=S0"))
        self.assertTrue(self.run_for(s2, 30, lambda s: s.state == "STREAMING" and s.healthy > 0))
        self.assertEqual(s2.status()["display"], "streaming")       # until the next healthy frame
        self.done(rig, s)
        self.done(rig2, s2)

    def test_swar_pixels_are_capped_at_30_fps_with_a_hint(self):
        class SwarLike(StubPreprocessor):
            name, max_fps = "swar", 30
        rig, s = self.service(pre=SwarLike())
        self.stream_ok(s)
        p0, t0 = s.published, s.clock.monotonic()
        self.run_for(s, 5)
        rate = (s.published - p0) / (s.clock.monotonic() - t0)
        self.assertTrue(28.0 <= rate <= 31.0, rate)                    # of a 60 fps stream
        st = s.status()
        self.assertEqual((st["service"]["pixels"], st["display"]), ("swar", "streaming"))
        self.assertIn("reduced frame rate (python pixels): run install.sh again", st["hint"])
        self.done(rig, s)

    def test_no_device_and_unplug(self):
        rig, s = self.service()
        self.stream_ok(s)
        rig.kernel.unplug()
        self.assertTrue(self.run_for(s, 3, lambda s: s.state == "NO_DEVICE"))
        st = s.status()
        self.assertEqual((st["display"], st["device"]["present"], s.counts["unplugs"]), ("no_device", False, 1))
        self.assertEqual(s.drivers[-1].teardown_path, "lost")
        self.done(rig, s)


if __name__ == "__main__":
    unittest.main()
