// SPDX-License-Identifier: MIT
// tools/FaceChecks, claims: ModuleClaims over all 32 input combinations and in Initialize's call pattern (the §8.1
// rows themselves run on the real module in tools/ModuleChecks), and LostClaims, the warnings for claims a VRCFT 6.0
// host gave to another module.
using SteamFrameEyeModule;
using static Checks;

static class ClaimChecks
{
    public static void Run()
    {
        var combos = 0;
        var ok = true;
        for (var bits = 0; bits < 32; bits++)
        {
            var i = new ModuleClaims.Inputs((bits & 1) != 0, (bits & 2) != 0, (bits & 4) != 0, (bits & 8) != 0, (bits & 16) != 0);
            foreach (var eyeStarted in new[] { false, true })
            foreach (var faceStarted in new[] { false, true })
            {
                combos++;
                var startEye = i.EyeAvailable && i.EyeEnabled;
                var startFace = i.ExpressionAvailable && i.FaceEnabled && i.EnumOk;
                var (eye, expression) = ModuleClaims.Decide(i, eyeStarted, faceStarted);
                var good = ModuleClaims.ShouldStartEye(i) == startEye && ModuleClaims.ShouldStartFace(i) == startFace
                           && eye == (startEye && eyeStarted) && expression == (startFace && faceStarted)
                           && (!eye || (i.EyeAvailable && i.EyeEnabled && eyeStarted))
                           && (!expression || (i.ExpressionAvailable && i.FaceEnabled && i.EnumOk && faceStarted));
                if (!good)
                {
                    ok = false;
                    Console.WriteLine($"  claims wrong for {i}, started ({eyeStarted}, {faceStarted}): ({eye}, {expression})");
                }
            }
        }
        Check(ok && combos == 128, "ModuleClaims over all 32 input combinations x 4 start results: never claims what was not offered or did not start");

        // ModuleClaims alone, row by row through the §8.1 table, in the call pattern Initialize uses (a start is attempted
        // only if ShouldStart*) with simulated start results. The real module runs these rows in tools/ModuleChecks.
        (bool, bool, bool, bool) CallPattern(bool eyeAvail, bool exprAvail, bool face, bool enumOk, bool eyeBinds, bool faceBinds)
        {
            var i = new ModuleClaims.Inputs(eyeAvail, exprAvail, true, face, enumOk);
            bool eyeTried = false, faceTried = false;
            bool StartEye() { eyeTried = true; return eyeBinds; }
            bool StartFace() { faceTried = true; return faceBinds; }
            var eyeStarted = ModuleClaims.ShouldStartEye(i) && StartEye();
            var faceStarted = ModuleClaims.ShouldStartFace(i) && StartFace();
            var (e, x) = ModuleClaims.Decide(i, eyeStarted, faceStarted);
            return (e, x, eyeTried, faceTried);
        }
        const string Pattern = "ModuleClaims call pattern, §8.1 row";
        Check(CallPattern(true, true, true, true, true, true) == (true, true, true, true), $"{Pattern} 1: T T babble_osc, both start -> (T,T)");
        Check(CallPattern(true, true, false, true, true, true) == (true, false, true, false), $"{Pattern} 2: face_source off -> (T,F), face start not attempted");
        Check(CallPattern(true, true, true, true, true, false) == (true, false, true, true), $"{Pattern} 3: face start fails (8888 in use) -> (T,F)");
        Check(CallPattern(false, true, true, true, true, true) == (false, true, false, true), $"{Pattern} 4: eyes not offered -> (F,T), eye start not attempted");
        Check(CallPattern(true, false, true, true, true, true) == (true, false, true, false), $"{Pattern} 5: expressions not offered -> (T,F), face start not attempted");
        Check(CallPattern(false, false, true, true, true, true) == (false, false, false, false) && CallPattern(false, false, false, true, true, true) == (false, false, false, false),
            $"{Pattern} 6: nothing offered -> (F,F) for any face_source, no start attempted");
        Check(CallPattern(true, true, true, true, false, true) == (false, true, true, true), $"{Pattern} 7: eye start fails (9021 in use) -> (F,T)");
        Check(CallPattern(true, true, true, false, true, true) == (true, false, true, false), $"{Pattern} 8: enum check failed -> (T,F), face start not attempted");
        var disabled = new ModuleClaims.Inputs(true, true, false, true, true);
        Check(ModuleClaims.Decide(disabled, true, true) == (false, true) && !ModuleClaims.ShouldStartEye(disabled), "ModuleClaims: eye_enabled false, eyes never claimed");
    }

    /// <summary>
    /// LostClaims: what the module warns when a claim-reporting host (VRCFT 6.0) does not report a claim Initialize
    /// returned. Each lost claim once, naming the claim and its data; the loss of every claim, none warned about yet, in
    /// one warning; nothing again while the reports stay the same. (The module asks only while Active, 1 s after
    /// Initialize and not at the first Update; tools/ModuleChecks runs that on the real module.)
    /// </summary>
    public static void Lost()
    {
        const string Won = "another module won this module's ";
        const string Fix = "; remove the other module and restart VRCFaceTracking";
        string One(string claim) => $"{Won}{claim} claim (Steam Link module? VRCFT-Babble?), so VRCFaceTracking ignores this module's {claim} data{Fix}";
        string All(string claims) => $"{Won}{claims} (Steam Link module? VRCFT-Babble?), so VRCFaceTracking ignores its data{Fix}";
        // The claims Initialize returned, then the host's reports in order: the warning (or null) after each report.
        string?[] Reports(bool eye, bool expression, params (bool Eye, bool Expression)[] reports)
        {
            var lost = new LostClaims();
            return reports.Select(r => lost.Check(eye, expression, r.Eye, r.Expression)).ToArray();
        }
        string Show(string?[] warnings) => string.Join(" | ", warnings.Select(w => w ?? "-"));
        const string Claims = "LostClaims, claims (T,T)";

        var eye = Reports(true, true, (true, true), (false, true), (false, true), (false, true));
        Check(eye.SequenceEqual(new[] { null, One("eye"), null, null }),
            $"{Claims}, reports (T,T) then (F,T) three times: one warning that another module won the eye claim and the host ignores this module's eye data, no repeat ({Show(eye)})");
        var expression = Reports(true, true, (true, true), (true, false), (true, false), (true, false));
        Check(expression.SequenceEqual(new[] { null, One("expression"), null, null }),
            $"{Claims}, reports (T,T) then (T,F) three times: one warning for the expression claim and data, no repeat ({Show(expression)})");
        var race = Reports(true, true, (false, true));
        Check(race.SequenceEqual(new[] { One("eye") }),
            $"{Claims}, first report (F,T) (another module's init won the eye claim): the eye warning ({Show(race)})");
        var both = Reports(true, true, (false, false), (false, false));
        Check(both.SequenceEqual(new[] { All("eye and expression claims"), null }),
            $"{Claims}, reports (F,F) twice: one warning for both claims, as before ({Show(both)})");
        var single = Reports(false, true, (false, false), (false, false)).Concat(Reports(true, false, (false, false))).ToArray();
        Check(single.SequenceEqual(new[] { All("expression claim"), null, All("eye claim") }),
            $"LostClaims, claims (F,T) or (T,F) and reports (F,F): the warning for all of the module's claims, as before ({Show(single)})");
        var later = Reports(true, true, (false, true), (false, false), (false, false));
        Check(later.SequenceEqual(new[] { One("eye"), One("expression"), null }),
            $"{Claims}, reports (F,T), (F,F), (F,F): the eye warning, then one for the expression claim alone ({Show(later)})");
        var back = Reports(true, true, (false, true), (true, true), (false, true));
        Check(back.SequenceEqual(new[] { One("eye"), null, null }),
            $"{Claims}, reports (F,T), (T,T), (F,T): one eye warning per run ({Show(back)})");
        var none = Reports(true, true, (true, true), (true, true)).Concat(Reports(false, false, (false, false))).ToArray();
        Check(none.All(w => w is null), $"LostClaims: no warning while the host reports every claim made, or for a module that claimed nothing ({Show(none)})");
    }
}
