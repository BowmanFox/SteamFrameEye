# SPDX-License-Identifier: MIT
"""Sup (ffsup, the supervisor of the driver matrix) keeps modelling the service's state machine (supervisor.UsbSupervisor,
spec §4.6-§4.13): the same scenarios run on Sup and on the inline Service, each on its own FakeKernel + SimClock rig, and
give the same decisions and waits. Covered: the settle from the end of the last session, a clean teardown or a LOST
(§4.13, decision 10); every decision's settle_mult (row 7 doubles one settle); a LOST soon after every activation backs off
(B1); 60 s of healthy streaming end the ladder pass, so a transient retries the proven rung (row 11); no USB reset within
600 s of the last one (§4.12); and the brown-out display at the lowest IR level."""
import ffutil  # first: installs the port guard

import unittest
from unittest import mock

ffutil.setup_path()
from test_ff_driver_matrix import actions, healthy, ops, rungs  # noqa: E402
from test_ff_service import SvcCase  # noqa: E402
from frameface.policy import DISPLAY, Decision, Failure as F, backoff_s  # noqa: E402


class Parity(SvcCase):
    def both(self, scenario, **knobs):
        """{"sup": Sup, "svc": Service} after scenario(kind, rig, s, run) on each, where run(seconds, until) steps it."""
        out = {}
        for kind in ("sup", "svc"):
            rig, s = self.make(**knobs) if kind == "sup" else self.service(**knobs)
            run = (lambda sec, until=None: s.run(sec, until)) if kind == "sup" else \
                (lambda sec, until=None: self.run_for(s, sec, until, stop_at=()))
            scenario(kind, rig, s, run)
            self.assertEqual(rig.kernel.violations, [], kind)
            out[kind] = s
        return out

    @staticmethod
    def pause(kind, s, on):                     # an idle teardown (Sup.idle / no client), or the way back
        if kind == "sup":
            return s.idle() if on else s.resume()
        s.client_override = 0 if on else 1

    def test_a_lost_settles_from_the_loss_unless_the_tracker_was_absent(self):
        def scenario(kind, rig, s, run):
            s.gaps = []
            for how in ("enodev", "glitch", "unplug"):
                self.assertTrue(run(90, lambda s: s.state == "STREAMING" and s.clock.monotonic() -
                                    s.activations[-1]["t"] > 31.0), (kind, how))       # past the B1 window
                n = len(s.activations)
                if how == "enodev":
                    rig.kernel.force_status(-19)    # an ENODEV completion: LOST, the node stays present
                else:
                    rig.kernel.unplug(replug_after=0.4 if how == "glitch" else 3.0)
                self.assertTrue(run(30, lambda s: len(s.activations) > n), (kind, how))
                s.gaps.append(s.activations[-1]["t"] - s.store.load_runtime().last_loss_bt)
        r = self.both(scenario, wedge_on_dirty_close=False)
        for kind, s in r.items():
            self.assertTrue(s.gaps[0] >= 8.0 and s.gaps[1] >= 8.0 and s.gaps[2] < 8.0, (kind, s.gaps))
            self.assertEqual((s.failures, s.counts["unplugs"]), ([], 3), kind)
        for a, b in zip(r["sup"].gaps, r["svc"].gaps):
            self.assertAlmostEqual(a, b, delta=1.0)

    def test_row_7_doubles_one_settle_only(self):
        def scenario(kind, rig, s, run):
            run(300, lambda s: s.state == "FAULT")
        r = self.both(scenario, ack_never=True)
        for kind, s in r.items():
            gaps = [b["t"] - a["t"] for a, b in zip(s.activations, s.activations[1:])]
            self.assertEqual(actions(s), [("retry_same", 7), ("next_strategy", 8), ("reset_port", 13), ("fault", 15)], kind)
            self.assertTrue(gaps[0] >= 16.0 and max(gaps[1:]) < 12.0, (kind, gaps))
            self.assertEqual((s.state, len(ops(s, "reset"))), ("FAULT", 1), kind)

    def test_a_lost_soon_after_every_activation_backs_off(self):
        def scenario(kind, rig, s, run):
            def hook(xu, phase):                # deep brown-out: off the bus 1 s after STREAM_ON, back 0.5 s later
                if xu == "stream_on" and phase == "done":
                    rig.clock.call_at(rig.clock.monotonic() + 1.0, lambda: rig.kernel.unplug(0.5))
            rig.vft.on_xu = hook
            s.shown = set()

            def watch(s):                       # the display while waiting at L3, once the lowering's 60 s are over
                l3 = [a["t"] for a in s.activations if a["level"] == "L3"]
                if s.state == "SETTLING" and l3 and s.clock.monotonic() > l3[0] + 60.0:
                    s.shown.add(s.display()[:3] if kind == "sup" else (lambda st: (st["display"], st["severity"],
                                                                                  st["message"]))(s.status()))
                return False
            run(600, watch)
        r = self.both(scenario)
        for kind, s in r.items():
            losses = [t for st, t in s.states if st == "LOST"]
            waits = [a["t"] - t for t, a in zip(losses, s.activations[1:])]
            self.assertTrue(len(s.activations) < 20 and all(w >= backoff_s(i) for i, w in enumerate(waits, 1)), (kind, waits))
            self.assertEqual(s.activations[-1]["level"], "L3", kind)
            self.assertIn(("brownout", DISPLAY["brownout"][0], "Tracker lost power, IR lowered to L3"), s.shown, kind)
        self.assertLessEqual(abs(len(r["sup"].activations) - len(r["svc"].activations)), 1)

    def transient_after_proof(self, kind, rig, s, run):
        self.assertTrue(run(120, healthy(30)), kind)
        run(70.0)                               # 60 s healthy: the ladder pass ends, S0 is proven
        self.pause(kind, s, True)
        self.assertTrue(run(40, lambda s: s.state == "IDLE"), kind)
        n, rig.vft.m.scene = len(s.failures), "bars"         # one activation shows test bars (stale XU state)
        self.pause(kind, s, False)
        self.assertTrue(run(120, lambda s: len(s.failures) > n), kind)
        rig.vft.m.scene = "mouth"
        self.assertTrue(run(60, healthy(30)), kind)

    def test_a_transient_after_a_proven_stream_retries_the_rung(self):
        r = self.both(self.transient_after_proof, fps=15.0)
        for kind, s in r.items():
            self.assertEqual((rungs(s), actions(s)), (["S0", "S0", "S0"], [("retry_same", 11)]), kind)

    def test_the_proven_rung_stays_first_when_the_profile_cannot_be_written(self):     # review finding A-2
        def scenario(kind, rig, s, run):
            def full(p):
                raise OSError(28, "No space left on device")
            s.store.save_profile = full         # profile.json cannot be written: S1 stays its known-good rung
            self.transient_after_proof(kind, rig, s, run)
        r = self.both(scenario, fps=15.0, profile={"strategy": "S1", "xu_len": 64, "source": "service"},
                      setif_required="after_commit")     # the stale known-good S1 shows test bars, S0 streams
        for kind, s in r.items():
            self.assertEqual((rungs(s), actions(s)), (["S1", "S0", "S0", "S0"], [("next_strategy", 12),
                                                                                  ("retry_same", 11)]), kind)
            self.assertTrue(s.log.has("W state: profile write failed"), kind)

    def test_no_usb_reset_within_600_s_of_the_last(self):
        def scenario(kind, rig, s, run):
            self.assertTrue(run(30, healthy(60)), kind)
            s.store.mark_reset()                # e.g. a reset of the previous process just now
            with mock.patch("ffsup.on_failure" if kind == "sup" else "frameface.supervisor.on_failure",
                            return_value=Decision("reset_port", strategy="S0", row=13)):
                if kind == "sup":
                    s.fail(F.NO_FRAMES)
                else:
                    s.act.request = (F.NO_FRAMES, {})
                    self.assertTrue(run(1, lambda s: s.state == "RESETTING"), kind)
            self.assertTrue(run(30, healthy(60)), kind)
        r = self.both(scenario)
        for kind, s in r.items():
            self.assertEqual((ops(s, "reset"), s.counts["resets"], len(s.activations)), ([], 0, 2), kind)
            self.assertTrue(s.log.has("W usb: reset skipped"), kind)


if __name__ == "__main__":
    unittest.main()
