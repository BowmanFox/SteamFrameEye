// SPDX-License-Identifier: MIT
// tools/FaceChecks, gate: FaceGate on a fake clock (one burst never live, 9 vs 10 bursts in a second at every
// phase, steady streams, the trailing 1 s window, fade-in, stale, decay, zero durations, the dt clamp).
using SteamFrameEyeModule;
using static Checks;

static class GateChecks
{
    public static void Run()
    {
        var idx = new int[57];
        var val = new float[57];
        long now = 0;
        var rx = new FaceReceiver(0, NoLog, NoLog, () => now);
        var gate = new FaceGate();
        var wroteBefore = false;
        for (now = 0; now <= 500; now += 10) wroteBefore |= gate.Compose(now, rx.Snapshot(now), false, idx, val) != 0;
        Check(!wroteBefore, "nothing is written before the first message");

        // One burst (Baballonia's single inference of a placeholder frame) never opens the gate.
        now = 1000;
        Util.Burst(rx, 0.8f);
        var opened = false;
        var count = 0;
        for (now = 1000; now <= 3000; now += 10)
        {
            count = gate.Compose(now, rx.Snapshot(now), false, idx, val);
            opened |= gate.Live || gate.Alpha > 0;
        }
        Check(!opened && count == 55 && val.Take(55).All(v => v == 0f), "one burst never opens the gate; the module then writes zeros");

        // Nine bursts in a second stay below 10 Hz; the tenth opens the gate, wherever the bursts start.
        var phaseFails = new List<string>();
        for (long start = 10000; start < 10250; start += 25)
        {
            foreach (var offset in start == 10225 ? new long[] { 10225, 10249 } : new[] { start })
            {
                var rate = new FaceReceiver(0, NoLog, NoLog, () => now);
                var rateGate = new FaceGate();
                bool liveAfter9 = true, liveAfter10 = false;
                for (var b = 0; b < 10; b++)
                {
                    now = offset + 100 * b;
                    Util.Burst(rate, 0.5f);
                    rateGate.Step(now, rate.Snapshot(now));
                    if (b == 8) liveAfter9 = rateGate.Live;
                    if (b == 9) liveAfter10 = rateGate.Live;
                }
                if (liveAfter9 || !liveAfter10 || rate.Snapshot(now).RateHz != 10)
                {
                    phaseFails.Add($"start {offset}: RateHz {rate.Snapshot(now).RateHz}");
                }
            }
        }
        Check(phaseFails.Count == 0, "9 bursts in 1 s are not live, 10 bursts in 1 s are (RateHz 10), for starts 10000..10249 ms" +
            (phaseFails.Count == 0 ? "" : $" (wrong at {string.Join("; ", phaseFails)})"));

        // RateHz counts a true trailing second: a steady stream at N Hz is live all the time with face_min_rate_hz N,
        // at any phase against the module's Update (every 11 ms here); 9 Hz never is.
        (double LivePercent, int MinRate, int MaxRate) Steady(int hz, int minRate, long phase)
        {
            long t = 0;
            var steadyRx = new FaceReceiver(0, NoLog, NoLog, () => t);
            var steadyGate = new FaceGate(minRate, 500, 150, 300);
            long steps = 0, liveSteps = 0, k = 0;
            int minHz = int.MaxValue, maxHz = 0;
            for (t = 0; t <= 8000; t++)
            {
                while (1000 + phase + k * 1000 / hz == t)
                {
                    Util.Burst(steadyRx, 0.5f);
                    k++;
                }
                if (t % 11 == 0)
                {
                    var s = steadyRx.Snapshot(t);
                    steadyGate.Step(t, s);
                    if (t >= 2500)
                    {
                        steps++;
                        liveSteps += steadyGate.Live ? 1 : 0;
                        (minHz, maxHz) = (Math.Min(minHz, s.RateHz), Math.Max(maxHz, s.RateHz));
                    }
                }
            }
            return (100.0 * liveSteps / steps, minHz, maxHz);
        }
        var steadyOk = true;
        var steadyReport = new List<string>();
        foreach (var (hz, minRate) in new[] { (10, 10), (11, 10), (12, 10), (13, 10), (9, 10), (30, 30), (20, 20), (60, 60) })
        {
            var runs = Enumerable.Range(0, 8).Select(p => Steady(hz, minRate, p * 31)).ToArray();
            var worst = runs.Min(r => r.LivePercent);
            var best = runs.Max(r => r.LivePercent);
            steadyOk &= hz < minRate ? best == 0 : worst >= 99;
            steadyReport.Add($"{hz} Hz/N={minRate}: live {worst:F1}..{best:F1} %, RateHz {runs.Min(r => r.MinRate)}..{runs.Max(r => r.MaxRate)}");
        }
        Check(steadyOk, $"steady 10-13 Hz with face_min_rate_hz 10, 20/20, 30/30 and 60/60 Hz are live >= 99 % at 8 phases, 9 Hz never ({string.Join("; ", steadyReport)})");
        long tw = 0;
        var window = new FaceReceiver(0, NoLog, NoLog, () => tw);
        var windowRates = new List<int>();
        for (tw = 0; tw < 2000; tw++)
        {
            if (tw % 10 == 0) window.Feed(FaceOsc.Float("/jawOpen", 0.5f));
            if (tw >= 1000) windowRates.Add(window.Snapshot(tw).RateHz);
        }
        Check(windowRates.All(r => r == 100) && window.Snapshot(1990 + 999).RateHz == 1 && window.Snapshot(1990 + 1000).RateHz == 0,
            $"one message per 10 ms reads RateHz 100 at every ms ({windowRates.Min()}..{windowRates.Max()}): the window is the trailing 1000 ms (an arrival at t counts until t + 999)");
        long tc = 0;
        var flood = new FaceReceiver(0, NoLog, NoLog, () => tc);
        for (tc = 0; tc < 1000; tc++)
        {
            flood.Feed(FaceOsc.Float("/jawOpen", 0.5f));
            flood.Feed(FaceOsc.Float("/jawOpen", 0.5f));
        }
        Check(flood.Snapshot(tc).RateHz == 128, $"2000 messages per second read RateHz 128, where the count stops (above face_min_rate_hz's maximum of 60; {flood.Snapshot(tc).RateHz})");

        // Baballonia at 62.5 Hz from t=20000 to t=30000, gate stepped every 10 ms (the module's Update).
        var live = new FaceReceiver(0, NoLog, NoLog, () => now);
        var g = new FaceGate();
        var alpha = new Dictionary<long, float>();
        var lives = new Dictionary<long, bool>();
        for (now = 19000; now <= 31500; now++)
        {
            if (now >= 20000 && now <= 30000 && (now - 20000) % 16 == 0) Util.Burst(live, 0.6f);
            if (now % 10 == 0)
            {
                alpha[now] = g.Step(now, live.Snapshot(now));
                lives[now] = g.Live;
            }
        }
        var firstLive = lives.Where(kv => kv.Value).Min(kv => kv.Key);
        var rampOk = Enumerable.Range(0, 14).All(k => Math.Abs(alpha[firstLive + 10 * k] - (k + 1) / 15f) < 1e-6) && alpha[firstLive + 140] == 1f;
        Check(rampOk, $"fade-in: linear over 150 ms from the last closed step (alpha 1 at {firstLive + 140 - 20000} ms after the first message)");
        Check(Enumerable.Range(0, 51).All(k => alpha[30000 + 10 * k] == 1f && lives[30000 + 10 * k]),
            "after the last message the gate stays open for face_stale_ms 500 (the 1 s rate window still holds >= 10 messages)");
        Check(!lives[30510] && Math.Abs(alpha[30650] - 0.5f) < 1e-6 && alpha[30790] > 0f && alpha[30800] == 0f && alpha[31500] == 0f,
            "then it fades linearly over face_decay_ms 300 to 0 (alpha 0.5 at +650 ms, 0 at +800 ms)");
        count = g.Compose(31500, live.Snapshot(31500), false, idx, val);
        Check(count == 55 && val.Take(55).All(v => v == 0f), "after the fade the module keeps writing 0 (the face relaxes, not frozen)");

        // Zero durations step at once; dt is clamped to 100 ms.
        var instant = new FaceGate(10, 500, 0, 0);
        var z = new FaceReceiver(0, NoLog, NoLog, () => now);
        for (var b = 0; b < 10; b++)
        {
            now = 40000 + 10 * b;
            Util.Burst(z, 0.5f);
        }
        var a1 = instant.Step(now, z.Snapshot(now));
        now += 1000;
        var a2 = instant.Step(now, z.Snapshot(now));
        Check(a1 == 1f && a2 == 0f, "face_fade_in_ms 0 and face_decay_ms 0 switch at the first step");
        var slow = new FaceGate();
        for (var b = 0; b < 10; b++)
        {
            now = 50000 + 10 * b;
            Util.Burst(z, 0.5f);
        }
        slow.Step(now, z.Snapshot(now));
        for (var b = 0; b < 10; b++)
        {
            now = 51000 + 10 * b; // ten more bursts, but no step until 1000 ms after the last one
            Util.Burst(z, 0.5f);
        }
        var after = slow.Step(now, z.Snapshot(now));
        Check(Math.Abs(after - 100f / 150f) < 1e-6, "dt between steps is clamped to 100 ms");
    }
}
