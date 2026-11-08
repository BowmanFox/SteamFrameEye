// SPDX-License-Identifier: MIT
namespace SteamFrameEyeModule;

/// <summary>
/// Babble45 -> UnifiedExpressions (VRCFT-free; spec §8.3, brief §e.7). Row i is the i-th address Baballonia sends
/// per inference. No gains: each target gets the clamped weight. 55 targets, all in 18..83, so the face path never
/// touches the eye module's Shapes[0..11]; with smile_slant MouthCornerSlantLeft/Right (59/58) also follow the
/// corner pull (57/56), so a full smile reads v2/MouthSmile 1.0 instead of 0.8. "Left" is the wearer's left.
/// </summary>
public static class FaceMapping
{
    public const int Count = 45;
    public const int Targets = 55;
    public const int TargetsWithSlant = 57;

    /// <summary>The 45 rows: OSC address, UnifiedExpressions indices and their enum member names.</summary>
    public static readonly (string Address, int[] Ue, string[] UeNames)[] Map =
    {
        ("/cheekPuffLeft", new[] { 19 }, new[] { "CheekPuffLeft" }),
        ("/cheekPuffRight", new[] { 18 }, new[] { "CheekPuffRight" }),
        ("/cheekSuckLeft", new[] { 21 }, new[] { "CheekSuckLeft" }),
        ("/cheekSuckRight", new[] { 20 }, new[] { "CheekSuckRight" }),
        ("/jawOpen", new[] { 22 }, new[] { "JawOpen" }),
        ("/jawForward", new[] { 25 }, new[] { "JawForward" }),
        ("/jawLeft", new[] { 24 }, new[] { "JawLeft" }),
        ("/jawRight", new[] { 23 }, new[] { "JawRight" }),
        ("/noseSneerLeft", new[] { 49 }, new[] { "NoseSneerLeft" }),
        ("/noseSneerRight", new[] { 48 }, new[] { "NoseSneerRight" }),
        ("/mouthFunnel", new[] { 39, 38, 37, 36 },
            new[] { "LipFunnelLowerLeft", "LipFunnelLowerRight", "LipFunnelUpperLeft", "LipFunnelUpperRight" }),
        ("/mouthPucker", new[] { 43, 42, 41, 40 },
            new[] { "LipPuckerLowerLeft", "LipPuckerLowerRight", "LipPuckerUpperLeft", "LipPuckerUpperRight" }),
        ("/mouthLeft", new[] { 53, 55 }, new[] { "MouthUpperLeft", "MouthLowerLeft" }),
        ("/mouthRight", new[] { 52, 54 }, new[] { "MouthUpperRight", "MouthLowerRight" }),
        ("/mouthRollUpper", new[] { 31, 30 }, new[] { "LipSuckUpperLeft", "LipSuckUpperRight" }),
        ("/mouthRollLower", new[] { 33, 32 }, new[] { "LipSuckLowerLeft", "LipSuckLowerRight" }),
        ("/mouthShrugUpper", new[] { 66 }, new[] { "MouthRaiserUpper" }),
        ("/mouthShrugLower", new[] { 67 }, new[] { "MouthRaiserLower" }),
        ("/mouthClose", new[] { 29 }, new[] { "MouthClosed" }),
        ("/mouthSmileLeft", new[] { 57 }, new[] { "MouthCornerPullLeft" }),
        ("/mouthSmileRight", new[] { 56 }, new[] { "MouthCornerPullRight" }),
        ("/mouthFrownLeft", new[] { 61 }, new[] { "MouthFrownLeft" }),
        ("/mouthFrownRight", new[] { 60 }, new[] { "MouthFrownRight" }),
        ("/mouthDimpleLeft", new[] { 65 }, new[] { "MouthDimpleLeft" }),
        ("/mouthDimpleRight", new[] { 64 }, new[] { "MouthDimpleRight" }),
        ("/mouthUpperUpLeft", new[] { 45 }, new[] { "MouthUpperUpLeft" }),
        ("/mouthUpperUpRight", new[] { 44 }, new[] { "MouthUpperUpRight" }),
        ("/mouthLowerDownLeft", new[] { 51 }, new[] { "MouthLowerDownLeft" }),
        ("/mouthLowerDownRight", new[] { 50 }, new[] { "MouthLowerDownRight" }),
        ("/mouthPressLeft", new[] { 69 }, new[] { "MouthPressLeft" }),
        ("/mouthPressRight", new[] { 68 }, new[] { "MouthPressRight" }),
        ("/mouthStretchLeft", new[] { 63 }, new[] { "MouthStretchLeft" }),
        ("/mouthStretchRight", new[] { 62 }, new[] { "MouthStretchRight" }),
        ("/tongueOut", new[] { 72 }, new[] { "TongueOut" }),
        ("/tongueUp", new[] { 73 }, new[] { "TongueUp" }),
        ("/tongueDown", new[] { 74 }, new[] { "TongueDown" }),
        ("/tongueLeft", new[] { 76 }, new[] { "TongueLeft" }),
        ("/tongueRight", new[] { 75 }, new[] { "TongueRight" }),
        ("/tongueRoll", new[] { 77 }, new[] { "TongueRoll" }),
        ("/tongueBendDown", new[] { 78 }, new[] { "TongueBendDown" }),
        ("/tongueCurlUp", new[] { 79 }, new[] { "TongueCurlUp" }),
        ("/tongueSquish", new[] { 80 }, new[] { "TongueSquish" }),
        ("/tongueFlat", new[] { 81 }, new[] { "TongueFlat" }),
        ("/tongueTwistLeft", new[] { 83 }, new[] { "TongueTwistLeft" }),
        ("/tongueTwistRight", new[] { 82 }, new[] { "TongueTwistRight" }),
    };

    /// <summary>smile_slant extras, appended after the 55 targets: (source target, target, its enum name).</summary>
    public static readonly (int FromUe, int Ue, string UeName)[] Slant =
    {
        (57, 59, "MouthCornerSlantLeft"),
        (56, 58, "MouthCornerSlantRight"),
    };

    private static readonly byte[][] LowerNames = Map.Select(r => r.Address[1..].ToLowerInvariant().Select(c => (byte)c).ToArray()).ToArray();

    /// <summary>Row of an address segment (the part after the last '/'), ordinal case-insensitive; -1 if unknown.</summary>
    public static int IndexOf(string lastSegment)
    {
        if (lastSegment is null)
        {
            return -1;
        }
        for (var i = 0; i < Map.Length; i++)
        {
            if (Map[i].Address.AsSpan(1).Equals(lastSegment.AsSpan(), StringComparison.OrdinalIgnoreCase))
            {
                return i;
            }
        }
        return -1;
    }

    /// <summary><see cref="IndexOf(string)"/> for the ASCII bytes of a received address segment (no allocation).</summary>
    public static int IndexOf(ReadOnlySpan<byte> lastSegment)
    {
        for (var i = 0; i < LowerNames.Length; i++)
        {
            var name = LowerNames[i];
            if (name.Length != lastSegment.Length)
            {
                continue;
            }
            var k = 0;
            while (k < name.Length && Lower(lastSegment[k]) == name[k])
            {
                k++;
            }
            if (k == name.Length)
            {
                return i;
            }
        }
        return -1;
    }

    private static byte Lower(byte b) => b is >= (byte)'A' and <= (byte)'Z' ? (byte)(b + 32) : b;

    /// <summary>
    /// Writes the (UE index, weight) pairs of all 45 inputs into <paramref name="idx"/>/<paramref name="val"/>:
    /// weight = clamp01(w45[i]) * alpha for every target of row i. Returns 55, or 57 with
    /// <paramref name="smileSlant"/> (then 59 = the value of 57 and 58 = the value of 56).
    /// </summary>
    public static int Fill(ReadOnlySpan<float> w45, float alpha, bool smileSlant, Span<int> idx, Span<float> val)
    {
        var n = smileSlant ? TargetsWithSlant : Targets;
        if (w45.Length < Count || idx.Length < n || val.Length < n)
        {
            throw new ArgumentException($"FaceMapping.Fill needs {Count} inputs and room for {n} targets");
        }
        var k = 0;
        float smileLeft = 0f, smileRight = 0f;
        for (var i = 0; i < Count; i++)
        {
            var v = Clamp01(w45[i]) * alpha;
            foreach (var ue in Map[i].Ue)
            {
                idx[k] = ue;
                val[k] = v;
                k++;
                if (ue == Slant[0].FromUe)
                {
                    smileLeft = v;
                }
                else if (ue == Slant[1].FromUe)
                {
                    smileRight = v;
                }
            }
        }
        if (smileSlant)
        {
            idx[k] = Slant[0].Ue;
            val[k++] = smileLeft;
            idx[k] = Slant[1].Ue;
            val[k++] = smileRight;
        }
        return k;
    }

    /// <summary>
    /// Checks every target (including the smile_slant ones) against <paramref name="nameOf"/>, the module's
    /// Enum.GetName(typeof(UnifiedExpressions), i). Adds a line per mismatch to <paramref name="bad"/>.
    /// </summary>
    public static bool CheckNames(Func<int, string?> nameOf, List<string>? bad = null)
    {
        var ok = true;
        void Check(int ue, string expected)
        {
            var actual = nameOf(ue);
            if (!string.Equals(actual, expected, StringComparison.Ordinal))
            {
                ok = false;
                bad?.Add($"{ue} is {actual ?? "undefined"}, expected {expected}");
            }
        }
        foreach (var row in Map)
        {
            for (var j = 0; j < row.Ue.Length; j++)
            {
                Check(row.Ue[j], row.UeNames[j]);
            }
        }
        foreach (var s in Slant)
        {
            Check(s.Ue, s.UeName);
        }
        return ok;
    }

    private static float Clamp01(float v) => v > 0f ? (v < 1f ? v : 1f) : 0f; // NaN -> 0
}
