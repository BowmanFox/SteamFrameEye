// SPDX-License-Identifier: MIT
// tools/FaceChecks, face log lines: FaceMonitor's first-data, stale, resume and 5-minute lines, and their rate
// limit over ten simulated minutes of degraded streams.
using SteamFrameEyeModule;
using static Checks;

static class LogChecks
{
    public static void Run()
    {
        var lines = new List<string>();
        long now = 0;
        var rx = new FaceReceiver(0, NoLog, NoLog, () => now);
        var gate = new FaceGate();
        var monitor = new FaceMonitor(500, m => lines.Add("info " + m), m => lines.Add("warn " + m), reportEveryMs: 60000);
        for (now = 0; now <= 90000; now += 10)
        {
            var sending = now < 20000 || (now >= 30000 && now < 45000);
            if (sending && now % 20 == 0) Util.Burst(rx, 0.5f, sourcePort: 50123);
            var s = rx.Snapshot(now);
            gate.Step(now, s);
            monitor.Step(now, s, gate.Live);
        }
        // At t = 1000 ms the trailing second holds 50 inferences of the 50 Hz stream (t = 20 ... 1000).
        Check(lines.FirstOrDefault() == "info first face data from Baballonia (port 50123): 50 Hz",
            $"first face data is logged after 1 s with its rate and source port ({lines.FirstOrDefault()})");
        var stopLine = "warn no face data for 0.5 s (Baballonia stopped, tracker unplugged, or headset not worn); relaxing the face";
        Check(lines.Count == 5 && lines[1] == stopLine && lines[2] == "info face data resumed", "a stop and a resume are logged");
        Check(lines.Count == 5 && lines[3].StartsWith("info face 0 Hz, live ") && lines[3].EndsWith(", gaps > 0.5 s: 2, rejected 0, unknown 0"),
            $"the periodic line reports rate, live %, gaps > 0.5 s, rejected and unknown ({(lines.Count > 3 ? lines[3] : "missing")})");
        Check(lines.Count == 5 && lines[4] == "warn no face data for 35.5 s (Baballonia stopped, tracker unplugged, or headset not worn); relaxing the face",
            $"a second stop 25 s after the first warning is reported once the minute since that warning is over ({(lines.Count > 4 ? lines[4] : "missing")})");
        Check(FaceMonitor.Summary(rx.Snapshot(now)) == $"face messages {rx.Snapshot(now).Messages} (0 rejected)", "the Teardown line counts messages");

        // Ten minutes of a degraded stream, the module's Update every 11 ms; returns the log lines with their times.
        List<(long T, string Line)> Simulate(Func<long, bool> burstAt, long untilMs = 601000)
        {
            var log = new List<(long, string)>();
            long t = 0;
            var simRx = new FaceReceiver(0, NoLog, NoLog, () => t);
            var simGate = new FaceGate();
            var simMonitor = new FaceMonitor(500, m => log.Add((t, "info " + m)), m => log.Add((t, "warn " + m)));
            var burst = FaceMapping.Map.Select(r => FaceOsc.Float(r.Address, 0.5f)).ToArray();
            for (t = 0; t <= untilMs; t++)
            {
                if (burstAt(t)) foreach (var d in burst) simRx.Feed(d, 50123);
                if (t % 11 == 0)
                {
                    var s = simRx.Snapshot(t);
                    simGate.Step(t, s);
                    simMonitor.Step(t, s, simGate.Live);
                }
            }
            return log;
        }
        var oneHz = Simulate(t => t % 1000 == 0);
        Check(oneHz.Count == 3 && oneHz[0].Line.StartsWith("info first face data from Baballonia (port 50123): ")
              && oneHz.Skip(1).All(l => l.Line.StartsWith("info face 1 Hz, live 0 %, gaps > 0.5 s: 300,")),
            $"10 minutes of 1 Hz inferences (never live, the face never shown): only the first-data line and the 5-minute lines ({oneHz.Count} lines: {string.Join(" | ", oneHz.Take(4).Select(l => l.Line))})");
        var stalls = Simulate(t => t % 3000 >= 600 && t * 60 / 1000 != (t - 1) * 60 / 1000); // 60 Hz, a 0.6 s stall every 3 s
        var warnTimes = stalls.Where(l => l.Line.EndsWith("relaxing the face")).Select(l => l.T).ToArray();
        var resumeTimes = stalls.Where(l => l.Line == "info face data resumed").Select(l => l.T).ToArray();
        var periodic = stalls.Where(l => l.Line.StartsWith("info face ") && l.Line.Contains(" Hz, live ")).Select(l => l.Line).ToArray();
        var gapCounts = periodic.Select(l => l[(l.IndexOf("gaps > 0.5 s: ", StringComparison.Ordinal) + 14)..]).Select(l => int.Parse(l[..l.IndexOf(',')])).ToArray();
        Check(warnTimes.Length is >= 9 and <= 10 && warnTimes.Zip(warnTimes.Skip(1)).All(p => p.Second - p.First >= 60000)
              && resumeTimes.Length == warnTimes.Length && resumeTimes.Zip(warnTimes).All(p => p.First > p.Second)
              && stalls.Count == 1 + warnTimes.Length + resumeTimes.Length + periodic.Length,
            $"60 Hz with a 0.6 s stall every 3 s: at most one stale/resume pair per 60 s ({warnTimes.Length} pairs and {stalls.Count} lines in 10 min)");
        Check(periodic.Length == 2 && gapCounts.All(n => n is 99 or 100),
            $"... and the 5-minute line still counts every gap, about 100 per 5 min ({string.Join(" | ", periodic)})");
        var degraded = Simulate(t => t < 5000 ? t % 33 == 0 : t % 1000 == 0, untilMs: 400000);
        Check(degraded.Count(l => l.Line.EndsWith("relaxing the face")) == 1 && degraded.Count(l => l.Line == "info face data resumed") == 1,
            $"shown once, then 1 Hz for minutes: one stale warning and one resume line, then none ({degraded.Count} lines)");
    }
}
