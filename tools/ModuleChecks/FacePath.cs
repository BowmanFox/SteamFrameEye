// SPDX-License-Identifier: MIT
// The face path through the real module's Update: which shapes it writes (§8.1, §8.3) and how soon the last row of a
// Baballonia inference reaches UnifiedTracking.Data. Face only (eye_enabled false), so no eye receiver starts.
using System.Diagnostics;
using SteamFrameEyeModule;
using VRCFaceTracking;

static class FacePath
{
    /// <summary>The 55 (57 with smile_slant) §8.3 shapes and nothing else: never Shapes[0..11], Eye or Head.</summary>
    public static void WriteSet(Rig rig)
    {
        foreach (var slant in new[] { false, true })
        {
            var facePort = Rig.FreeUdpPort();
            rig.Settings(("face_source", "babble_osc"), ("eye_enabled", false), ("face_port", facePort), ("smile_slant", slant));
            Rig.Markers();
            var shapes = UnifiedTracking.Data.Shapes;
            var expected = FaceMapping.Map.SelectMany(r => r.Ue).Concat(slant ? FaceMapping.Slant.Select(s => s.Ue) : Enumerable.Empty<int>()).ToHashSet();
            var (m, log) = Rig.NewModule();
            using var sender = Rig.Sender();
            try
            {
                var result = m.Initialize(true, true);
                Rig.Status(m, true, false, true); // the stand-in host reports the claims, as VRCFT 6.0 does
                for (var i = 0; i < 10; i++)
                {
                    m.Update();
                }
                var untouched = shapes.All(s => s.Weight == Rig.Marker) && Rig.EyeAndHeadUntouched();
                rig.Check(result == (false, true) && untouched,
                    $"smile_slant {slant}: Initialize(True, True) with eye_enabled false -> {result}; nothing is written before the first face message", log);
                var watch = Stopwatch.StartNew();
                long next = 0;
                var done = false;
                while (!done && watch.ElapsedMilliseconds < 3000)
                {
                    if (watch.ElapsedMilliseconds >= next)
                    {
                        Rig.Burst(sender, facePort, _ => 0.7f);
                        next += 16;
                    }
                    m.Update();
                    done = expected.All(i => shapes[i].Weight == 0.7f);
                }
                m.Update();
                var wrong = Enumerable.Range(0, shapes.Length)
                    .Where(i => expected.Contains(i) ? shapes[i].Weight != 0.7f : shapes[i].Weight != Rig.Marker).ToArray();
                rig.Check(wrong.Length == 0 && expected.Count == (slant ? FaceMapping.TargetsWithSlant : FaceMapping.Targets) && expected.Min() >= 18,
                    $"smile_slant {slant}: the face path wrote exactly the {expected.Count} §8.3 shapes (indices {expected.Min()}..{expected.Max()}), " +
                    $"every other shape incl. Shapes[0..11] kept its marker ({watch.ElapsedMilliseconds} ms of bursts; wrong: {string.Join(",", wrong)})", log);
                rig.Check(Rig.EyeAndHeadUntouched(), $"smile_slant {slant}: the face path never writes Eye or Head", log);
            }
            finally
            {
                m.Teardown();
            }
        }
    }

    /// <summary>
    /// VRCFT calls Update in a loop; the face receiver wakes it. The last datagram of each inference (row 44,
    /// TongueTwistRight, Shapes[82]) must reach UnifiedTracking.Data right after it was sent, not at the next wake
    /// (the next burst, an SFEY packet or Update's 20 ms wait).
    /// </summary>
    public static void WakeOnLastRow(Rig rig)
    {
        var facePort = Rig.FreeUdpPort();
        rig.Settings(("face_source", "babble_osc"), ("eye_enabled", false), ("face_port", facePort));
        var shapes = UnifiedTracking.Data.Shapes;
        const int Bursts = 20;
        var sentAt = new long[Bursts];
        var seenAt = new long[Bursts];
        var stop = false;
        var (m, log) = Rig.NewModule();
        using var sender = Rig.Sender();
        Thread? update = null, poller = null;
        try
        {
            m.Initialize(true, true);
            Rig.Status(m, true, false, true);
            update = new Thread(() =>
            {
                while (!Volatile.Read(ref stop))
                {
                    m.Update();
                }
            }) { IsBackground = true, Name = "VRCFT update loop" };
            update.Start();
            var warm = Stopwatch.StartNew();
            while (warm.ElapsedMilliseconds < 600) // the gate opens and fades in to alpha 1
            {
                Rig.Burst(sender, facePort, _ => 0.003f);
                Thread.Sleep(15);
            }
            poller = new Thread(() =>
            {
                var last = -1f;
                while (!Volatile.Read(ref stop))
                {
                    var w = shapes[82].Weight;
                    if (w != last)
                    {
                        last = w;
                        var k = (int)MathF.Round(w * 100) - 1;
                        if (k is >= 0 and < Bursts && seenAt[k] == 0 && MathF.Abs(w - (k + 1) / 100f) < 1e-6f)
                        {
                            seenAt[k] = Stopwatch.GetTimestamp();
                        }
                    }
                    Thread.SpinWait(20);
                }
            }) { IsBackground = true, Name = "poller" };
            poller.Start();
            for (var k = 0; k < Bursts; k++)
            {
                var v = (k + 1) / 100f;
                Rig.Burst(sender, facePort, _ => v);
                sentAt[k] = Stopwatch.GetTimestamp();
                Thread.Sleep(40);
            }
            Thread.Sleep(50);
        }
        finally
        {
            Volatile.Write(ref stop, true);
            update?.Join(1000);
            poller?.Join(1000);
            m.Teardown();
        }
        var lags = Enumerable.Range(0, Bursts).Where(k => seenAt[k] != 0)
            .Select(k => (seenAt[k] - sentAt[k]) * 1000.0 / Stopwatch.Frequency).OrderBy(x => x).ToArray();
        var median = lags.Length > 0 ? lags[lags.Length / 2] : double.NaN;
        rig.Check(lags.Length == Bursts && median < 8,
            $"the last row of each inference (TongueTwistRight, Shapes[82]) is written {median:F1} ms (median; slowest {(lags.Length > 0 ? lags[^1] : double.NaN):F1} ms) " +
            $"after the burst's last datagram was sent ({lags.Length} of {Bursts} inferences seen)", log);
    }
}
