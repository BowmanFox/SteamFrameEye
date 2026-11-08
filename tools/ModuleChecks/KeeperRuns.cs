// SPDX-License-Identifier: MIT
// The status keeper as the module wires it (status_keeper, the claims Initialize returned, Status, VRCFT 6.0's claim
// reports, spec §8.5) and the module's warnings when a claim-reporting host gave a claim it made to another module.
// The keeper's datagrams go to an injected recorder (SteamFrameEyeModule.StatusKeeperSendOverride), never to a
// socket; the host kind is injected through SteamFrameEyeModule.HostReportsClaimsOverride, except in the run where the
// module detects it from the loaded stand-in SDK (VRCFaceTracking.SDK 6.0.0.0, a claim-reporting host).
using System.Diagnostics;
using System.Text;
using Module = SteamFrameEyeModule.SteamFrameEyeModule;

static class KeeperRuns
{
    private const string LostClaims = "another module won this module's";

    /// <summary>A keeper datagram as "Lip ,T", "Expression ,F" or "Eye ,T"; anything else is marked unexpected.</summary>
    public static string Describe(byte[] d)
    {
        var text = Encoding.ASCII.GetString(d);
        var name = text.Contains("/LipTrackingActive\0") ? "Lip" : text.Contains("/ExpressionTrackingActive\0") ? "Expression"
            : text.Contains("/EyeTrackingActive\0") ? "Eye" : "unexpected";
        var value = d.Length >= 4 && d[^4] == 0x2c && d[^2] == 0 && d[^1] == 0 && d[^3] is (byte)'T' or (byte)'F' ? $",{(char)d[^3]}" : ",?";
        return name == "unexpected" || value == ",?" ? "unexpected " + Convert.ToHexString(d) : $"{name} {value}";
    }

    private sealed record Outcome((bool Eye, bool Expression) Result, ModuleLog Log, List<(long Ms, string What)> Sent, long WarnedAtMs);

    /// <summary>
    /// Face only: Initialize, VRCFT's status update (Active and the reported claims), Baballonia for liveMs while VRCFT's
    /// update loop runs, then Teardown. Times in ms since just before Initialize. hostReportsClaims null: no override,
    /// the module detects the host from the loaded stand-in SDK (6.0.0.0, a claim-reporting host).
    /// </summary>
    private static Outcome Run(Rig rig, string mode, bool? hostReportsClaims, bool usingEye, bool usingExpression,
        bool expressionAvailable = true, int liveMs = 1500)
    {
        var facePort = Rig.FreeUdpPort();
        rig.Settings(("face_source", "babble_osc"), ("eye_enabled", false), ("face_port", facePort), ("status_keeper", mode),
            ("status_keeper_port", Rig.FreeUdpPort()));
        var clock = Stopwatch.StartNew();
        var sent = new List<(long, string)>();
        long warnedAt = -1;
        Module.HostReportsClaimsOverride = hostReportsClaims;
        Module.StatusKeeperSendOverride = d =>
        {
            lock (sent)
            {
                sent.Add((clock.ElapsedMilliseconds, Describe(d)));
            }
        };
        var (m, log) = Rig.NewModule(line =>
        {
            if (line.Contains(LostClaims, StringComparison.Ordinal) && Volatile.Read(ref warnedAt) < 0)
            {
                Volatile.Write(ref warnedAt, clock.ElapsedMilliseconds);
            }
        });
        using var sender = Rig.Sender();
        var stop = false;
        Thread? update = null;
        (bool, bool) result = default;
        try
        {
            result = m.Initialize(true, expressionAvailable);
            Rig.Status(m, true, usingEye, usingExpression);
            update = new Thread(() =>
            {
                while (!Volatile.Read(ref stop))
                {
                    m.Update();
                }
            }) { IsBackground = true, Name = "VRCFT update loop" };
            update.Start();
            while (clock.ElapsedMilliseconds < liveMs)
            {
                if (result.Item2)
                {
                    Rig.Burst(sender, facePort, _ => 0.5f);
                }
                Thread.Sleep(16);
            }
        }
        finally
        {
            Volatile.Write(ref stop, true);
            update?.Join(1000);
            m.Teardown();
            Module.HostReportsClaimsOverride = null;
            Module.StatusKeeperSendOverride = null;
        }
        lock (sent)
        {
            return new Outcome(result, log, sent.ToList(), Volatile.Read(ref warnedAt));
        }
    }

    private static string Summary(List<(long Ms, string What)> sent) =>
        sent.Count == 0 ? "nothing sent" : string.Join(", ", sent.Select(s => $"{s.What} at {s.Ms} ms"));

    public static void Run(Rig rig)
    {
        // 5.4.x never reports the claims (both stay false while Active): the claims Initialize returned count.
        foreach (var mode in new[] { "face", "all" })
        {
            var legacy = Run(rig, mode, hostReportsClaims: false, usingEye: false, usingExpression: false);
            var trues = legacy.Sent.Where(s => s.What.EndsWith(",T")).Select(s => s.What).Distinct().Order().ToArray();
            var last = legacy.Sent.TakeLast(2).Select(s => s.What).Order().ToArray();
            var bools = mode == "face" ? "Lip/ExpressionTrackingActive" : "Lip/Expression/EyeTrackingActive";
            rig.Check(legacy.Result == (false, true) && legacy.Log.Has($"status keeper \"{mode}\" sends {bools} to 127.0.0.1:")
                      && trues.SequenceEqual(new[] { "Expression ,T", "Lip ,T" }) && last.SequenceEqual(new[] { "Expression ,F", "Lip ,F" })
                      && legacy.Sent.All(s => s.What is "Lip ,T" or "Lip ,F" or "Expression ,T" or "Expression ,F") && !legacy.Log.Has(LostClaims),
                $"status_keeper {mode}, 5.4.x host, face only (F,T): the module's keeper sends Lip/ExpressionTrackingActive ,T while the face is live, " +
                $"no eye bool (eyes not claimed), and ,F at Teardown ({Summary(legacy.Sent)})", legacy.Log);
        }

        // A claim-reporting host (6.0) that gave the expression claim to another module: Active, both flags false. No
        // override: the module itself detects the host from the loaded VRCFaceTracking.SDK (the stand-ins, 6.0.0.0).
        var lost = Run(rig, "face", hostReportsClaims: null, usingEye: false, usingExpression: false);
        rig.Check(lost.Result == (false, true) && lost.Sent.Count == 0,
            $"6.0 host (detected, no override), Active with UsingEye and UsingExpression false: the keeper sends nothing, not even at Teardown ({Summary(lost.Sent)})", lost.Log);
        rig.Check(lost.Log.Count(LostClaims) == 1
                  && lost.Log.Has("Warning: SteamFrameEye: another module won this module's expression claim (Steam Link module? VRCFT-Babble?), so " +
                                  "VRCFaceTracking ignores its data; remove the other module and restart VRCFaceTracking")
                  && lost.WarnedAtMs >= 950,
            $"... and the module warns once that another module won its expression claim, {lost.WarnedAtMs} ms after Initialize", lost.Log);

        var held = Run(rig, "face", hostReportsClaims: true, usingEye: false, usingExpression: true);
        rig.Check(held.Sent.Any(s => s.What == "Lip ,T") && held.Sent.Any(s => s.What == "Expression ,T") && !held.Log.Has(LostClaims)
                  && held.Log.Count("VRCFaceTracking uses this module for eye=False expression=True") == 1,
            $"6.0 host reporting the expression claim: Lip/ExpressionTrackingActive are sent, no warning, the claims are logged once ({Summary(held.Sent)})", held.Log);

        var off = Run(rig, "off", hostReportsClaims: false, usingEye: false, usingExpression: false, liveMs: 800);
        rig.Check(off.Sent.Count == 0 && !off.Log.Has("status keeper"), "status_keeper off: no keeper is created and nothing is sent", off.Log);

        var none = Run(rig, "face", hostReportsClaims: true, usingEye: false, usingExpression: false, expressionAvailable: false, liveMs: 1300);
        rig.Check(none.Result == (false, false) && none.Sent.Count == 0 && !none.Log.Has("status keeper") && !none.Log.Has(LostClaims),
            "nothing claimed (expressions not offered, eye_enabled false): no keeper and no lost-claims warning", none.Log);

        FirstUpdate(rig);
        OneClaimLost(rig);
    }

    /// <summary>
    /// A claim-reporting host that takes one of the module's two claims (eye and expression) away: one warning that names
    /// that claim and its data, none for the other, the change logged once, and no repeat while the report stays the
    /// same. Both receivers start (the eye one binds 0.0.0.0), so this runs only with MODULECHECKS_ALLOW_WILDCARD=1.
    /// </summary>
    private static void OneClaimLost(Rig rig)
    {
        foreach (var (usingEye, usingExpression) in new[] { (false, true), (true, false) })
        {
            var name = $"6.0 host, claims (True, True) reported (True, True), then ({usingEye}, {usingExpression})";
            if (!rig.Wildcard)
            {
                rig.Skip(name);
                continue;
            }
            var facePort = Rig.FreeUdpPort();
            var eyePort = Rig.FreeUdpPort();
            while (eyePort == facePort)
            {
                eyePort = Rig.FreeUdpPort();
            }
            rig.Settings(("face_source", "babble_osc"), ("port", eyePort), ("face_port", facePort));
            Module.HostReportsClaimsOverride = true;
            var (m, log) = Rig.NewModule();
            try
            {
                var result = m.Initialize(true, true);
                Rig.Status(m, true, false, false); // 6.0: Active first, the claims it gave right after
                m.Update();
                Rig.Status(m, true, true, true);
                var clock = Stopwatch.StartNew();
                while (clock.ElapsedMilliseconds < 1300)
                {
                    m.Update(); // waits up to 20 ms for data
                }
                var before = log.Count(LostClaims);
                Rig.Status(m, true, usingEye, usingExpression);
                var updates = 0;
                clock.Restart();
                while (clock.ElapsedMilliseconds < 500)
                {
                    m.Update();
                    updates++;
                }
                var lost = usingEye ? "expression" : "eye";
                var warning = $"Warning: SteamFrameEye: another module won this module's {lost} claim (Steam Link module? VRCFT-Babble?), so " +
                              $"VRCFaceTracking ignores this module's {lost} data; remove the other module and restart VRCFaceTracking";
                rig.Check(result == (true, true) && before == 0 && log.Count(LostClaims) == 1 && log.Has(warning)
                          && log.Count($"VRCFaceTracking uses this module for eye={usingEye} expression={usingExpression}") == 1,
                    $"{name}: one warning that another module won the {lost} claim and VRCFaceTracking ignores this module's {lost} data, " +
                    $"none before, no repeat in {updates} more Updates ({log.Count(LostClaims)} lost-claims warnings)", log);
            }
            finally
            {
                m.Teardown();
                Module.HostReportsClaimsOverride = null;
            }
        }
    }

    /// <summary>6.0 sends Active with both claims false first and the real claims right after: never judged at the first Update.</summary>
    private static void FirstUpdate(Rig rig)
    {
        rig.Settings(("face_source", "babble_osc"), ("eye_enabled", false), ("face_port", Rig.FreeUdpPort()));
        Module.HostReportsClaimsOverride = true;
        var (m, log) = Rig.NewModule();
        try
        {
            m.Initialize(true, true);
            Rig.Status(m, true, false, false);
            Thread.Sleep(1100);
            m.Update();
            var afterFirst = log.Count(LostClaims);
            m.Update();
            m.Update();
            rig.Check(afterFirst == 0 && log.Count(LostClaims) == 1,
                $"6.0 host: the lost-claims warning never comes from the first Update, even 1.1 s after Initialize ({afterFirst} after the first Update, {log.Count(LostClaims)} after three)", log);
        }
        finally
        {
            m.Teardown();
            Module.HostReportsClaimsOverride = null;
        }
    }
}
