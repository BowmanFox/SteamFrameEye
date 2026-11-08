// SPDX-License-Identifier: MIT
// tools/FaceChecks, status keeper (injected sender only): message bytes and the schedule on a fake clock for off,
// face and all, Status gating, VRCFT 6.0 claim reports, the 30 s ,F and OnTeardown.
using System.Text;
using SteamFrameEyeModule;
using static Checks;

static class KeeperChecks
{
    public static void Run()
    {
        Check(FaceOsc.Bool(StatusKeeper.LipAddress, true).Length == 44 && FaceOsc.Bool(StatusKeeper.ExpressionAddress, true).Length == 48
              && FaceOsc.Bool(StatusKeeper.EyeAddress, true).Length == 44,
            "keeper messages: LipTrackingActive 44 B, ExpressionTrackingActive 48 B, EyeTrackingActive 44 B");
        Check(new[] { StatusKeeper.LipAddress, StatusKeeper.ExpressionAddress, StatusKeeper.EyeAddress }.All(a =>
                FaceOsc.Bool(a, true)[^4..].SequenceEqual(new byte[] { 0x2c, 0x54, 0, 0 }) && FaceOsc.Bool(a, false)[^4..].SequenceEqual(new byte[] { 0x2c, 0x46, 0, 0 })),
            "keeper bools end in 2c 54 00 00 (,T) or 2c 46 00 00 (,F)");

        // off: nothing, ever.
        var offRun = new KeeperRun("off");
        offRun.Run(0, 60000, face: true, eye: true, active: true, usingEye: true, usingExpr: true);
        offRun.Keeper.OnTeardown();
        Check(offRun.Sent.Count == 0, "status_keeper off sends nothing");

        // face: nothing before live once; ,T every 2 s while live; one ,F after 5 s not live; ,T again on resume.
        var face = new KeeperRun("face");
        face.Run(0, 4900, face: false, eye: true, active: true, usingEye: true, usingExpr: true);
        var before = face.Sent.Count;
        face.Run(5000, 9900, face: true, eye: true, active: true, usingEye: true, usingExpr: true);
        var (trues, exprTrues) = (face.Times("Lip", true), face.Times("Expression", true));
        face.Run(10000, 19900, face: false, eye: true, active: true, usingEye: true, usingExpr: true);
        var (offs, exprOffs) = (face.Times("Lip", false), face.Times("Expression", false));
        face.Run(20000, 20500, face: true, eye: true, active: true, usingEye: true, usingExpr: true);
        Check(before == 0, "face: nothing is sent before the face was live once");
        Check(trues.SequenceEqual(new long[] { 5000, 7000, 9000 }) && exprTrues.SequenceEqual(trues),
            "face: Lip and ExpressionTrackingActive ,T at once and every 2 s while live");
        Check(offs.SequenceEqual(new long[] { 15000 }) && exprOffs.SequenceEqual(offs),
            "face: one ,F after 5 s not live, then nothing");
        Check(face.Times("Lip", true).SequenceEqual(new long[] { 5000, 7000, 9000, 20000 }) && face.Times("Lip", false).Length == 1
              && face.Sent.All(m => !m.Name.StartsWith("Eye")), "face: ,T again on resume; never the eye bool");

        // all: the eye bool follows SFEY freshness the same way.
        var all = new KeeperRun("all");
        all.Run(0, 2900, face: true, eye: false, active: true, usingEye: true, usingExpr: true);
        all.Run(3000, 6900, face: true, eye: true, active: true, usingEye: true, usingExpr: true);
        all.Run(7000, 13000, face: true, eye: false, active: true, usingEye: true, usingExpr: true);
        Check(all.Times("Eye", true).SequenceEqual(new long[] { 3000, 5000 }) && all.Times("Eye", false).SequenceEqual(new long[] { 12000 })
              && all.Times("Lip", true).First() == 0,
            "all: EyeTrackingActive ,T at once and every 2 s while SFEY is fresh, ,F 5 s after it went stale");

        // Status gating: nothing while not Active; one ,F per bool sent ,T when it turns false; ,T again when Active again.
        var gated = new KeeperRun("all");
        gated.Run(0, 3000, face: true, eye: true, active: false, usingEye: true, usingExpr: true);
        Check(gated.Sent.Count == 0, "nothing is sent while statusActive is false");
        gated.Run(3100, 4000, face: true, eye: true, active: true, usingEye: true, usingExpr: true);
        var mark = gated.Sent.Count;
        gated.Run(4100, 8000, face: true, eye: true, active: false, usingEye: true, usingExpr: true);
        var released = gated.Sent.Skip(mark).ToList();
        Check(released.Count == 3 && released.All(m => !m.Value && m.Time == 4100),
            "when statusActive turns false: one ,F for each bool sent ,T (Lip, Expression, Eye), then nothing");
        gated.Run(8100, 8100, face: true, eye: true, active: true, usingEye: true, usingExpr: true);
        Check(gated.Sent.Skip(mark + 3).Count(m => m.Value && m.Time == 8100) == 3, "back to Active while live: ,T again at once");

        // 6.0 claim reports: a bool is withheld for a capability the module does not hold.
        var eyeOnly = new KeeperRun("all");
        eyeOnly.Run(0, 10000, face: true, eye: true, active: true, usingEye: true, usingExpr: false);
        Check(eyeOnly.Sent.Count > 0 && eyeOnly.Sent.All(m => m.Name == "Eye"), "6.0 UsingExpression false: Lip/Expression withheld, Eye sent");
        var exprOnly = new KeeperRun("all");
        exprOnly.Run(0, 10000, face: true, eye: true, active: true, usingEye: false, usingExpr: true);
        Check(exprOnly.Sent.Count > 0 && exprOnly.Sent.All(m => m.Name != "Eye"), "6.0 UsingEye false: Eye withheld, Lip/Expression sent");

        // 5.4.x never reports the claims: both false while Active -> the claims Initialize returned count.
        var legacy = new KeeperRun("all");
        legacy.Run(0, 3000, face: true, eye: true, active: true, usingEye: false, usingExpr: false, claimedEye: true, claimedExpr: false);
        Check(legacy.Sent.Count > 0 && legacy.Sent.All(m => m.Name == "Eye"), "5.4.x (both Using flags false): the Initialize claims decide (eye claimed, expressions not)");
        var legacyBoth = new KeeperRun("face");
        legacyBoth.Run(0, 3000, face: true, eye: true, active: true, usingEye: false, usingExpr: false, claimedEye: true, claimedExpr: true);
        Check(legacyBoth.Times("Lip", true).SequenceEqual(new long[] { 0, 2000 }), "5.4.x with expressions claimed: Lip/Expression ,T every 2 s");

        // A host that reports the claims (VRCFT 6.0): while Active its Using flags decide, even both false (another module
        // won both claims): then nothing is sent, not even the 30 s ,F, although Initialize claimed both.
        var lost = new KeeperRun("all", hostReportsClaims: true);
        lost.Run(0, 3000, face: true, eye: true, active: true, usingEye: false, usingExpr: false, claimedEye: true, claimedExpr: true);
        lost.Run(3100, 63000, face: false, eye: false, active: true, usingEye: false, usingExpr: false, claimedEye: true, claimedExpr: true);
        lost.Keeper.OnTeardown();
        Check(lost.Sent.Count == 0, $"6.0 host, Active with UsingEye and UsingExpression false: nothing is sent, not even the 30 s ,F ({lost.Sent.Count} datagrams)");
        var reported = new KeeperRun("all", hostReportsClaims: true);
        reported.Run(0, 0, face: true, eye: true, active: true, usingEye: false, usingExpr: false);
        reported.Run(100, 2100, face: true, eye: true, active: true, usingEye: true, usingExpr: true);
        Check(reported.Times("Lip", true).SequenceEqual(new long[] { 100, 2100 }) && reported.Times("Eye", true).SequenceEqual(new long[] { 100, 2100 }),
            "6.0 host: its first status (Active, false, false) sends nothing; the claims it reports right after count");
        var exprReported = new KeeperRun("all", hostReportsClaims: true);
        exprReported.Run(0, 3000, face: true, eye: true, active: true, usingEye: false, usingExpr: true, claimedEye: true, claimedExpr: true);
        Check(exprReported.Sent.Count > 0 && exprReported.Sent.All(m => m.Name != "Eye"), "6.0 host, only the expression claim reported: Lip/Expression sent, Eye withheld");

        // One ,F 30 s after Initialize for a claimed capability that never went live.
        var never = new KeeperRun("all");
        never.Run(0, 60000, face: false, eye: false, active: true, usingEye: true, usingExpr: true);
        Check(never.Sent.Count == 3 && never.Sent.All(m => !m.Value && m.Time == 30000),
            "a claimed capability never live: one ,F for each of its bools 30 s after Initialize, nothing else");
        var notHeld = new KeeperRun("face");
        notHeld.Run(0, 60000, face: false, eye: false, active: true, usingEye: true, usingExpr: false);
        Check(notHeld.Sent.Count == 0, "no 30 s ,F for a capability the module does not hold");
        var wentLive = new KeeperRun("face");
        wentLive.Run(0, 1000, face: true, eye: false, active: true, usingEye: true, usingExpr: true);
        wentLive.Run(1100, 60000, face: false, eye: false, active: true, usingEye: true, usingExpr: true);
        Check(wentLive.Times("Lip", false).SequenceEqual(new long[] { 6100 }), "a capability that was live gets the 5 s ,F, not the 30 s one");

        // OnTeardown: ,F for every bool whose last value sent was ,T; nothing afterwards.
        var teardown = new KeeperRun("all");
        teardown.Run(0, 1000, face: true, eye: true, active: true, usingEye: true, usingExpr: true);
        mark = teardown.Sent.Count;
        teardown.Keeper.OnTeardown();
        var finalOffs = teardown.Sent.Skip(mark).ToList();
        teardown.Run(1100, 5000, face: true, eye: true, active: true, usingEye: true, usingExpr: true);
        Check(finalOffs.Count == 3 && finalOffs.All(m => !m.Value) && teardown.Sent.Count == mark + 3,
            "OnTeardown sends ,F for every bool sent ,T (3 in all mode) and nothing is sent afterwards");
        var staleTeardown = new KeeperRun("face");
        staleTeardown.Run(0, 1000, face: true, eye: false, active: true, usingEye: true, usingExpr: true);
        staleTeardown.Run(1100, 7000, face: false, eye: false, active: true, usingEye: true, usingExpr: true);
        mark = staleTeardown.Sent.Count;
        staleTeardown.Keeper.OnTeardown();
        Check(staleTeardown.Sent.Count == mark, "OnTeardown sends nothing for bools already sent ,F");
    }
}

/// <summary>A status keeper on a fake clock with a recording sender.</summary>
sealed class KeeperRun
{
    private long _now;

    public KeeperRun(string mode, bool hostReportsClaims = false)
    {
        Keeper = new StatusKeeper(9000, mode, () => _now, Record, hostReportsClaims);
    }

    public StatusKeeper Keeper { get; }
    public List<(long Time, string Name, bool Value)> Sent { get; } = new();

    /// <summary>Ticks every 100 ms from <paramref name="from"/> to <paramref name="to"/> (inclusive).</summary>
    public void Run(long from, long to, bool face, bool eye, bool active, bool usingEye, bool usingExpr,
        bool claimedEye = true, bool claimedExpr = true)
    {
        for (_now = from; _now <= to; _now += 100)
        {
            Keeper.Tick(face, eye, active, usingEye, usingExpr, claimedEye, claimedExpr);
        }
    }

    public long[] Times(string name, bool value) => Sent.Where(m => m.Name == name && m.Value == value).Select(m => m.Time).ToArray();

    private void Record(byte[] datagram)
    {
        var text = Encoding.ASCII.GetString(datagram);
        var name = text.Contains("/LipTrackingActive\0") ? "Lip" : text.Contains("/ExpressionTrackingActive\0") ? "Expression"
            : text.Contains("/EyeTrackingActive\0") ? "Eye" : "?";
        var value = datagram[^4..].SequenceEqual(new byte[] { 0x2c, 0x54, 0, 0 });
        var size = name == "Expression" ? 48 : 44;
        if (name == "?" || datagram.Length != size || !(value || datagram[^4..].SequenceEqual(new byte[] { 0x2c, 0x46, 0, 0 })))
        {
            throw new InvalidOperationException("unexpected keeper datagram " + Convert.ToHexString(datagram));
        }
        Sent.Add((_now, name, value));
    }
}
