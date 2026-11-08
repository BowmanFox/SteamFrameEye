// SPDX-License-Identifier: MIT
// tools/FaceChecks, mapping == §8.3: FaceMapping's 45 rows, targets and names against the spec table (Expected),
// the enum-name check, Fill with and without smile_slant, and IndexOf.
using System.Text;
using SteamFrameEyeModule;
using static Checks;

static class MappingChecks
{
    public static void Run()
    {
        var map = FaceMapping.Map;
        var rowsOk = map.Length == Expected.Rows.Length;
        for (var i = 0; rowsOk && i < map.Length; i++)
        {
            var parts = Expected.Rows[i].Split(' ');
            var targets = parts.Skip(1).Select(p => p.Split(':')).ToArray();
            rowsOk = map[i].Address == parts[0]
                     && map[i].Ue.SequenceEqual(targets.Select(t => int.Parse(t[1])))
                     && map[i].UeNames.SequenceEqual(targets.Select(t => t[0]));
            if (!rowsOk) Console.WriteLine($"  row {i}: {map[i].Address} differs from \"{Expected.Rows[i]}\"");
        }
        Check(rowsOk, "FaceMapping.Map has the 45 rows of §8.3 in Baballonia's order, targets and names in table order");
        var all = map.SelectMany(r => r.Ue).ToArray();
        Check(all.Length == 55 && all.Distinct().Count() == 55 && all.All(u => u is >= 18 and <= 83),
            "55 distinct targets, all in 18..83 (none < 12: the eye module's Shapes[0..11] are never written)");
        var never = new[] { 58, 59, 46, 47, 16, 17, 70, 71, 34, 35, 26, 27, 28, 12, 13, 14, 15, 84, 85, 86, 87 };
        Check(!all.Intersect(never).Any(), "never written: CornerSlant (without smile_slant), UpperDeepen, CheekSquint, Tightener, LipSuckCorner, 26-28, Nasal, 84-87");
        Check(FaceMapping.CheckNames(i => i is >= 0 and < 88 ? Expected.UeNames[i] : null),
            "every target name equals the UnifiedExpressions member at its index (brief §e.6)");
        var bad = new List<string>();
        var renamed = (string[])Expected.UeNames.Clone();
        renamed[22] = "JawOpenRenamed";
        Check(!FaceMapping.CheckNames(i => i is >= 0 and < 88 ? renamed[i] : null, bad) && bad.Count == 1 && bad[0].Contains("22"),
            "a renamed enum member is reported as a mismatch (the module then turns the face path off)");

        var w = new float[45];
        for (var i = 0; i < 45; i++) w[i] = (i + 1) / 50f;
        w[0] = 1.7f;   // clamped to 1
        w[1] = -0.3f;  // clamped to 0
        var idx = new int[57];
        var val = new float[57];
        var n = FaceMapping.Fill(w, 0.5f, false, idx, val);
        var fillOk = n == 55;
        for (int i = 0, k = 0; fillOk && i < 45; i++)
        {
            var expected = Math.Clamp(w[i], 0f, 1f) * 0.5f;
            foreach (var ue in map[i].Ue)
            {
                fillOk = idx[k] == ue && val[k] == expected;
                k++;
            }
        }
        Check(fillOk, "Fill writes clamp01(w) * alpha to every target of each row (55 pairs)");
        var n2 = FaceMapping.Fill(w, 0.5f, true, idx, val);
        float ValueOf(int ue, int count) => val[Array.IndexOf(idx, ue, 0, count)];
        Check(n2 == 57 && idx[55] == 59 && idx[56] == 58 && val[55] == ValueOf(57, 55) && val[56] == ValueOf(56, 55)
              && val[55] == w[19] * 0.5f && val[56] == w[20] * 0.5f,
            "with smile_slant Fill returns 57: 59 = value of 57, 58 = value of 56");
        var seg = Enumerable.Range(0, 45).All(i =>
            FaceMapping.IndexOf(map[i].Address[1..]) == i && FaceMapping.IndexOf(map[i].Address[1..].ToUpperInvariant()) == i
            && FaceMapping.IndexOf(Encoding.ASCII.GetBytes(map[i].Address[1..].ToLowerInvariant())) == i);
        Check(seg && FaceMapping.IndexOf("nope") == -1 && FaceMapping.IndexOf("") == -1 && FaceMapping.IndexOf("/jawOpen") == -1
              && FaceMapping.IndexOf(Encoding.ASCII.GetBytes("jawOpenX")) == -1,
            "IndexOf finds all 45 segments case-insensitively (string and bytes) and returns -1 otherwise");
    }
}

static class Expected
{
    /// <summary>Spec §8.3, transcribed: address, then UE name:index per target in table order.</summary>
    public static readonly string[] Rows =
    {
        "/cheekPuffLeft CheekPuffLeft:19",
        "/cheekPuffRight CheekPuffRight:18",
        "/cheekSuckLeft CheekSuckLeft:21",
        "/cheekSuckRight CheekSuckRight:20",
        "/jawOpen JawOpen:22",
        "/jawForward JawForward:25",
        "/jawLeft JawLeft:24",
        "/jawRight JawRight:23",
        "/noseSneerLeft NoseSneerLeft:49",
        "/noseSneerRight NoseSneerRight:48",
        "/mouthFunnel LipFunnelLowerLeft:39 LipFunnelLowerRight:38 LipFunnelUpperLeft:37 LipFunnelUpperRight:36",
        "/mouthPucker LipPuckerLowerLeft:43 LipPuckerLowerRight:42 LipPuckerUpperLeft:41 LipPuckerUpperRight:40",
        "/mouthLeft MouthUpperLeft:53 MouthLowerLeft:55",
        "/mouthRight MouthUpperRight:52 MouthLowerRight:54",
        "/mouthRollUpper LipSuckUpperLeft:31 LipSuckUpperRight:30",
        "/mouthRollLower LipSuckLowerLeft:33 LipSuckLowerRight:32",
        "/mouthShrugUpper MouthRaiserUpper:66",
        "/mouthShrugLower MouthRaiserLower:67",
        "/mouthClose MouthClosed:29",
        "/mouthSmileLeft MouthCornerPullLeft:57",
        "/mouthSmileRight MouthCornerPullRight:56",
        "/mouthFrownLeft MouthFrownLeft:61",
        "/mouthFrownRight MouthFrownRight:60",
        "/mouthDimpleLeft MouthDimpleLeft:65",
        "/mouthDimpleRight MouthDimpleRight:64",
        "/mouthUpperUpLeft MouthUpperUpLeft:45",
        "/mouthUpperUpRight MouthUpperUpRight:44",
        "/mouthLowerDownLeft MouthLowerDownLeft:51",
        "/mouthLowerDownRight MouthLowerDownRight:50",
        "/mouthPressLeft MouthPressLeft:69",
        "/mouthPressRight MouthPressRight:68",
        "/mouthStretchLeft MouthStretchLeft:63",
        "/mouthStretchRight MouthStretchRight:62",
        "/tongueOut TongueOut:72",
        "/tongueUp TongueUp:73",
        "/tongueDown TongueDown:74",
        "/tongueLeft TongueLeft:76",
        "/tongueRight TongueRight:75",
        "/tongueRoll TongueRoll:77",
        "/tongueBendDown TongueBendDown:78",
        "/tongueCurlUp TongueCurlUp:79",
        "/tongueSquish TongueSquish:80",
        "/tongueFlat TongueFlat:81",
        "/tongueTwistLeft TongueTwistLeft:83",
        "/tongueTwistRight TongueTwistRight:82",
    };

    /// <summary>UnifiedExpressions 0..87 (brief §e.6, identical in VRCFT 5.4.5 and 6.0).</summary>
    public static readonly string[] UeNames =
    {
        "EyeSquintRight", "EyeSquintLeft", "EyeWideRight", "EyeWideLeft", "BrowPinchRight", "BrowPinchLeft",
        "BrowLowererRight", "BrowLowererLeft", "BrowInnerUpRight", "BrowInnerUpLeft", "BrowOuterUpRight", "BrowOuterUpLeft",
        "NasalDilationRight", "NasalDilationLeft", "NasalConstrictRight", "NasalConstrictLeft", "CheekSquintRight", "CheekSquintLeft",
        "CheekPuffRight", "CheekPuffLeft", "CheekSuckRight", "CheekSuckLeft", "JawOpen", "JawRight",
        "JawLeft", "JawForward", "JawBackward", "JawClench", "JawMandibleRaise", "MouthClosed",
        "LipSuckUpperRight", "LipSuckUpperLeft", "LipSuckLowerRight", "LipSuckLowerLeft", "LipSuckCornerRight", "LipSuckCornerLeft",
        "LipFunnelUpperRight", "LipFunnelUpperLeft", "LipFunnelLowerRight", "LipFunnelLowerLeft", "LipPuckerUpperRight", "LipPuckerUpperLeft",
        "LipPuckerLowerRight", "LipPuckerLowerLeft", "MouthUpperUpRight", "MouthUpperUpLeft", "MouthUpperDeepenRight", "MouthUpperDeepenLeft",
        "NoseSneerRight", "NoseSneerLeft", "MouthLowerDownRight", "MouthLowerDownLeft", "MouthUpperRight", "MouthUpperLeft",
        "MouthLowerRight", "MouthLowerLeft", "MouthCornerPullRight", "MouthCornerPullLeft", "MouthCornerSlantRight", "MouthCornerSlantLeft",
        "MouthFrownRight", "MouthFrownLeft", "MouthStretchRight", "MouthStretchLeft", "MouthDimpleRight", "MouthDimpleLeft",
        "MouthRaiserUpper", "MouthRaiserLower", "MouthPressRight", "MouthPressLeft", "MouthTightenerRight", "MouthTightenerLeft",
        "TongueOut", "TongueUp", "TongueDown", "TongueRight", "TongueLeft", "TongueRoll",
        "TongueBendDown", "TongueCurlUp", "TongueSquish", "TongueFlat", "TongueTwistRight", "TongueTwistLeft",
        "SoftPalateClose", "ThroatSwallow", "NeckFlexRight", "NeckFlexLeft",
    };
}
