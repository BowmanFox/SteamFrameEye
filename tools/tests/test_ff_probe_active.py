# SPDX-License-Identifier: MIT
"""The probe's active sessions (probe_active.py) beyond test_ff_probe, which is at the 600-line limit:
- Settle: the settle before every activation follows the service's rule (spec §4.13, decision 10; UsbSupervisor._deadline),
  measured from the end of the last session, a clean teardown (runtime.json last_teardown_bt) or a LOST (last_loss_bt),
  whichever came later. So the probe never starts Block A within settle_s of a loss, whether the stopped service or the probe
  itself lost the tracker.
- Signals (decision 35, K3): the probe's signal handler takes no lock, like run's (cli.watch_signals): a wakeup-fd thread
  ("stop-signals") sets the open session's abort, so a real SIGTERM still abandons Block A at a transfer boundary and Block C
  and the service restart run; at a prompt the handler still ends the run at once.
- TeardownIr (§5.2 A12, B-1): a [0, 0, 0] readback passes only if some REG_READ of PWM 0x02-0x04 in the run read non-zero,
  also when A4 was dark and only A11 or an A5 rung streamed.
FakeKernel + SimClock with the inline driver and the mocked systemctl of test_ff_probe; nothing here touches a real device,
unit or port."""
import ffutil  # first: installs the port guard

import os
import signal
import threading
import time
import unittest
from unittest import mock

ffutil.setup_path()
from frameface import probe, probe_active  # noqa: E402
from frameface.state import Runtime, Store  # noqa: E402
from test_ff_probe import ACTIVE, BOOT, POSIX, ProbeCase  # noqa: E402

SETTLE_S = 8.0                                  # the config default (settle_s)
SIGS = [s for s in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)) if s is not None]


class Settle(ProbeCase):
    @staticmethod
    def first_magics(rig):
        """The time of the first MAGIC (Block A) of every session, one per open."""
        ops = rig.kernel.ops
        opens = [i for i, op in enumerate(ops) if op["kind"] == "open"] + [len(ops)]
        return [next(op["t"] for op in ops[a:b] if op["kind"] == "ctrl" and op["name"] == "MAGIC") for a, b in zip(opens, opens[1:])]

    def test_the_later_of_the_last_teardown_and_the_last_loss_sets_the_settle(self):
        for teardown_ago, loss_ago in ((30.0, 3.0), (3.0, 30.0), (3.0, None)):
            with self.subTest(teardown_ago=teardown_ago, loss_ago=loss_ago):
                rig = self.rig()
                bt = rig.clock.boottime()
                end = bt - min(t for t in (teardown_ago, loss_ago) if t is not None)     # the end of the last session
                Store(self.paths, rig.clock, boot_id=BOOT).save_runtime(Runtime(
                    boot_id=BOOT, last_teardown_bt=bt - teardown_ago, last_loss_bt=None if loss_ago is None else bt - loss_ago))
                self.assertEqual(self.run_probe(rig, *ACTIVE[:-1], "probe-%s-%s" % (teardown_ago, loss_ago)), 0)
                magic = self.first_magics(rig)[0]
                self.assertTrue(end + SETTLE_S <= magic < end + SETTLE_S + 1.0, (magic - end, teardown_ago, loss_ago))
                self.clean(rig)

    def test_after_its_own_lost_the_next_session_settles_from_the_loss(self):
        rig = self.rig(brownout_current_above=0x80)                                     # L0 browns out 1 s into the stream
        self.assertEqual(self.run_probe(rig, *ACTIVE, "--xu-len", "384"), 0)            # A4 is LOST (no FAIL), then A11
        ops, magics = rig.kernel.ops, self.first_magics(rig)
        replug = next(op for op in ops if op["kind"] == "replug")
        loss = Store(self.paths, rig.clock, boot_id=BOOT).load_runtime().last_loss_bt   # the probe's mark_lost of A4
        self.assertIsNotNone(loss)
        self.assertLess(replug["t"] + 2.0, loss + SETTLE_S)                             # the enum settle alone would be shorter
        self.assertEqual(len(magics), 2)
        self.assertTrue(loss + SETTLE_S <= magics[1] < loss + SETTLE_S + 1.0, magics[1] - loss)
        self.assertEqual(rig.kernel.violations, [])


class Signals(ProbeCase):
    """K3: CPython runs a Python signal handler in the main thread between two bytecodes, also while that thread holds a lock,
    so the probe's handler must take none (the old one called the session's drv.abort.set(), the same class as runtime-1)."""

    def setUp(self):
        super().setUp()
        noop = self.noop = lambda *a: None
        old = {s: signal.signal(s, noop) for s in SIGS}                                # a stray signal never kills the run
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in old.items()])

    @staticmethod
    def kill(sig):
        """A real signal to this process: kill() on POSIX (the way systemd, ssh or a terminal send it), raise() on Windows."""
        os.kill(os.getpid(), sig) if POSIX else signal.raise_signal(sig)

    def restored(self):
        """The probe's handlers, wakeup fd and "stop-signals" thread are gone again."""
        self.assertEqual([signal.getsignal(s) for s in SIGS], [self.noop] * len(SIGS))
        self.assertEqual(signal.set_wakeup_fd(-1), -1)
        self.assertEqual([t.name for t in threading.enumerate() if t.name == "stop-signals"], [])

    def test_the_stop_handler_takes_no_lock(self):
        rig, real, returned = self.rig(), probe.Probe._stream, []

        def stream(p, ses, seconds, measure=False, until=None):
            if measure and not returned:                # A4 streams: the handler runs where the main thread holds abort's lock
                handler, done = signal.getsignal(signal.SIGTERM), threading.Event()

                def interrupted():
                    with ses.drv.abort._cond:
                        handler(signal.SIGTERM, None)
                        done.set()
                threading.Thread(target=interrupted, daemon=True).start()
                returned.append(done.wait(1.0))
            return real(p, ses, seconds, measure, until)
        with mock.patch.object(probe.Probe, "_stream", stream):
            code = self.run_probe(rig, *ACTIVE)
        rep = self.report()
        self.assertEqual(returned, [True], "the probe's stop handler blocked on the session's abort lock")
        self.assertEqual((code, rep["aborted"], rep["aborted_step"]), (1, "SIGTERM", "A4"))
        self.restored()
        self.clean(rig)

    def test_a_real_sigterm_in_block_a_reaches_the_driver_from_the_watcher_and_block_c_runs(self):
        rig, drivers, real = self.rig(), [], probe_active.VftDriver

        class Driver(real):                             # records which thread sets the session's abort
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                ev, self.setters = self.abort, []
                ev.set = lambda: (self.setters.append(threading.current_thread().name), threading.Event.set(ev))
                drivers.append(self)

        def on_xu(kind, phase):                         # STREAM_ON acknowledged: the stop lands in Block A's 500 ms sleep
            if kind == "stream_on" and phase == "done" and len(drivers) == 1 and not drivers[0].setters:
                self.kill(signal.SIGTERM)
                end = time.monotonic() + 5.0
                while not drivers[0].abort.is_set() and time.monotonic() < end:
                    time.sleep(0.005)
        rig.vft.on_xu = on_xu
        with mock.patch.object(probe_active, "VftDriver", Driver):
            code = self.run_probe(rig, "--active", "--yes", "--stop-service", "--seconds", "3", "--bundle-name", "probe-term")
        rep, ops = self.report("probe-term"), rig.kernel.ops
        self.assertEqual((code, rep["aborted"], rep["aborted_step"]), (1, "SIGTERM", "A1"), self.out.getvalue())
        self.assertEqual((len(drivers), drivers[0].setters), (1, ["stop-signals"]))   # not the handler: no lock in it
        names = [op["name"] for op in ops if op["kind"] == "ctrl"]
        self.assertEqual((names.count("STREAM_ON"), names.count("COMMIT_SET"), [op for op in ops if op["kind"] == "submit"]),
                         (1, 0, []))                    # Block A abandoned after STREAM_ON: no Block B, no URB
        regs = [n for n in names if n in ("REG_WRITE", "REG_READ")]        # Block A's 11 ON and Block C's 11 OFF writes, then
        self.assertEqual(regs, ["REG_WRITE"] * 22 + ["REG_READ"] * 3)      # the A12 readback (A2's REG_READ: never reached)
        self.assertEqual(self.verbs()[-1], "start")                                     # the service runs again
        self.assertFalse(Store(self.paths, rig.clock, boot_id=BOOT).load_runtime().dirty)
        self.restored()
        self.clean(rig)

    def test_a_signal_at_the_prompt_ends_the_run_at_once(self):
        rig, kill = self.rig(), self.kill

        class Terminal:                                 # the y/N prompt of a run without --yes; Ctrl-C while it waits
            @staticmethod
            def readline():
                kill(signal.SIGINT)
                time.sleep(5.0)
                return "y\n"
        t0 = time.monotonic()
        code = self.run_probe(rig, "--active", "--seconds", "1", "--bundle-name", "probe-p", stdin=Terminal())
        self.assertEqual((code, self.opened(rig)), (2, []), self.out.getvalue())
        self.assertIn("probe: refused: SIGINT", self.out.getvalue())
        self.assertLess(time.monotonic() - t0, 4.0)
        self.restored()


class TeardownIr(ProbeCase):
    """B-1: REG_READ that is acknowledged but always says 0 (write-only registers, or no register reads on the HS persona) must
    not let A12 pass with the IR on, also when A4 was dark at the configured rung and only A11 or an A5 rung streamed."""

    def run_a12(self, name, *extra, zero=False, **knobs):
        q = probe_active.ActiveSteps._q
        read = (lambda drv, setup, reg=None, ms=1000: q(drv, setup, reg, ms) if reg is None else q(drv, setup, reg, ms) and 0) \
            if zero else q
        rig = self.rig(**knobs)
        with mock.patch.object(probe_active.ActiveSteps, "_q", staticmethod(read)):
            self.run_probe(rig, *ACTIVE[:-1], name, *extra)
        if os.path.exists(self.paths.profile):          # the next run starts at the configured rung again (§5.2 profile rule)
            os.remove(self.paths.profile)
        rep = self.report(name)
        reads = [[(c["data"].get("regs") or {}).get(k) for k in ("02", "03", "04")] for c in rep["checks"] if "regs" in c["data"]]
        return self.checks(rep), reads, rep["teardown_ir"]["pwm_readback"], [rig.vft.regs.get(a) for a in (2, 3, 4)]

    def test_a_zero_readback_passes_only_after_a_reg_read_showed_the_pwm(self):
        why = "cannot verify: REG_READ never read 02-04 as non-zero, so 0 proves nothing: PWM 02-04 read back [0, 0, 0] "
        for extra in (("--xu-len", "384"), ("--matrix",)):                   # A4 (S0 at 64) dark; A11 / A5's S5 at 384 lit
            with self.subTest(args=extra):
                c, reads, rb, pwm = self.run_a12("probe-z%d" % len(extra), *extra, zero=True, programs_at_len=384)
                self.assertEqual((c["A4"]["status"], rb, pwm), ("FAIL", [0, 0, 0], [255, 255, 255]))   # the IR is still on
                self.assertEqual(c["A12"]["status"], "WARN", c["A12"]["detail"])
                self.assertTrue(c["A12"]["detail"].startswith(why), c["A12"]["detail"])
                self.assertIn('"teardown_magic3": true', c["A12"]["hint"])
        # a REG_READ that works: A11 read PWM 0xff (A2 read 0: nothing latched at 64), so after the teardown's trailing MAGIC
        # the [0, 0, 0] readback is the IR off
        c, reads, rb, pwm = self.run_a12("probe-ok", "--xu-len", "384", "--teardown-magic3", programs_at_len=384)
        self.assertEqual((reads, rb, pwm), ([[0, 0, 0], [255, 255, 255]], [0, 0, 0], [0, 0, 0]))
        self.assertEqual(c["A12"]["status"], "PASS", c["A12"]["detail"])


if __name__ == "__main__":
    unittest.main()
