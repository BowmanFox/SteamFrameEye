// SPDX-License-Identifier: MIT
// The §8.1 table on the real module: what Initialize returns and logs, and which receiver holds its port. A face start
// that must not happen is caught by holding the face port meanwhile (an attempt would log the bind error); an eye
// start that must not happen throws in TestPorts, because the eye port of those rows is the forbidden 9021.
using System.Diagnostics;
using System.Net.Sockets;
using VRCFaceTracking.Core.Params.Expressions;
using Module = SteamFrameEyeModule.SteamFrameEyeModule;

static class ClaimRows
{
    private const string FaceListening = "listening for Baballonia OSC on 127.0.0.1:{F} ";
    private const string FaceError = "cannot bind 127.0.0.1:{F} (";
    private const string FaceErrorEnd = "); is the VRCFT-Babble module or another Baballonia listener running? Face tracking is off";
    private const string EyeListening = "listening on UDP {E};";
    private const string EyeError = "cannot bind UDP port {E}:";
    private const string EyesTaken = "eye tracking was already claimed by another module";
    private const string ExpressionsTaken = "expressions were already claimed by another module";
    private const string FaceOff = "face_source is \"off\"; expressions not claimed";
    private const string EyeDisabled = "eye_enabled is false; eyes not claimed";
    private const string EnumBad = "this VRCFaceTracking's UnifiedExpressions differ from the face mapping (";

    /// <summary>One row; {F} and {E} in Logged stand for the face and eye ports of the run.</summary>
    private sealed record Row(string Name, bool EyeAvailable, bool ExpressionAvailable, bool EyeEnabled, bool Face,
        (bool Eye, bool Expression) Expected, string[] Logged)
    {
        public bool BlockFace { get; init; }
        public bool BlockEye { get; init; }
        public bool BadEnum { get; init; }
        /// <summary>The face start must not be attempted: the port is held meanwhile and no bind error may appear.</summary>
        public bool NoFaceStart { get; init; }
        public bool EyeStarts => EyeAvailable && EyeEnabled;
    }

    public static void Run(Rig rig)
    {
        var rows = new[]
        {
            // eye_enabled false or eyes not offered: the eye receiver never starts, so these rows run everywhere.
            new Row("1, face side: T T babble_osc (eye_enabled false)", true, true, false, true, (false, true), new[] { EyeDisabled, FaceListening }),
            new Row("2, face side: T T, face_source absent (eye_enabled false)", true, true, false, false, (false, false), new[] { EyeDisabled, FaceOff }) { NoFaceStart = true },
            new Row("3, face side: T T babble_osc, face port in use (eye_enabled false)", true, true, false, true, (false, false), new[] { FaceError, FaceErrorEnd }) { BlockFace = true },
            new Row("4: eyes not offered", false, true, true, true, (false, true), new[] { EyesTaken, FaceListening }),
            new Row("5, face side: expressions not offered (eye_enabled false)", true, false, false, true, (false, false), new[] { ExpressionsTaken }) { NoFaceStart = true },
            new Row("6: nothing offered (babble_osc)", false, false, true, true, (false, false), new[] { EyesTaken, ExpressionsTaken }) { NoFaceStart = true },
            new Row("6: nothing offered (face_source absent)", false, false, true, false, (false, false), new[] { EyesTaken, FaceOff }) { NoFaceStart = true },
            new Row("8, face side: enum check failed (eye_enabled false)", true, true, false, true, (false, false), new[] { EnumBad }) { BadEnum = true, NoFaceStart = true },
            // The whole rows: the eye receiver starts (0.0.0.0).
            new Row("1: T T babble_osc, both start", true, true, true, true, (true, true), new[] { EyeListening, FaceListening }),
            new Row("2: T T, face_source absent (0.5.3 behaviour)", true, true, true, false, (true, false), new[] { EyeListening, FaceOff }) { NoFaceStart = true },
            new Row("3: T T babble_osc, face port in use", true, true, true, true, (true, false), new[] { EyeListening, FaceError, FaceErrorEnd }) { BlockFace = true },
            new Row("5: expressions not offered", true, false, true, true, (true, false), new[] { EyeListening, ExpressionsTaken }) { NoFaceStart = true },
            new Row("7: T T babble_osc, eye port in use", true, true, true, true, (false, true), new[] { EyeError, FaceListening }) { BlockEye = true },
            new Row("8: T T babble_osc, enum check failed", true, true, true, true, (true, false), new[] { EyeListening, EnumBad }) { BadEnum = true, NoFaceStart = true },
        };
        foreach (var row in rows)
        {
            if (row.EyeStarts && !rig.Wildcard)
            {
                rig.Skip($"§8.1 row {row.Name}");
                continue;
            }
            RunRow(rig, row);
        }
    }

    private static void RunRow(Rig rig, Row row)
    {
        var facePort = Rig.FreeUdpPort();
        var eyePort = Rig.GuardedEyePort;
        while (row.EyeStarts && (eyePort == Rig.GuardedEyePort || eyePort == facePort))
        {
            eyePort = Rig.FreeUdpPort();
        }
        var settings = new List<(string, object)> { ("port", eyePort), ("face_port", facePort) };
        if (!row.EyeEnabled)
        {
            settings.Add(("eye_enabled", false));
        }
        if (row.Face)
        {
            settings.Add(("face_source", "babble_osc"));
        }
        rig.Settings(settings);
        var blockers = new List<Socket>();
        if (row.BlockFace || row.NoFaceStart)
        {
            blockers.Add(Rig.Block(facePort));
        }
        if (row.BlockEye)
        {
            blockers.Add(Rig.Block(eyePort, Rig.EyeAddress));
        }
        Module.ExpressionNameOverride = row.BadEnum ? i => i == 22 ? "JawOpenRenamed" : Enum.GetName(typeof(UnifiedExpressions), i) : null;
        var (m, log) = Rig.NewModule();
        string Fill(string text) => text.Replace("{F}", facePort.ToString()).Replace("{E}", eyePort.ToString());
        try
        {
            var watch = Stopwatch.StartNew();
            var result = m.Initialize(row.EyeAvailable, row.ExpressionAvailable);
            watch.Stop();
            var claims = $"claims eye={row.Expected.Eye} expression={row.Expected.Expression} (eye UDP {eyePort}, face 127.0.0.1:{facePort}, face_source={(row.Face ? "babble_osc" : "off")})";
            var missing = row.Logged.Select(Fill).Append(claims).Where(text => !log.Has(text)).ToArray();
            var faceAttempted = log.Has(Fill(FaceError)) || log.Has(Fill(FaceListening));
            var faceHeld = !row.BlockFace && !row.NoFaceStart && !Rig.PortFree(facePort);
            var eyeHeld = row.EyeStarts && !row.BlockEye && !Rig.PortFree(eyePort, Rig.EyeAddress);
            var binds = (row.BlockFace || row.NoFaceStart || faceHeld == row.Expected.Expression) && (!row.NoFaceStart || !faceAttempted)
                        && eyeHeld == (row.Expected.Eye && row.EyeStarts);
            rig.Check(result == row.Expected && missing.Length == 0 && binds && watch.ElapsedMilliseconds < 1000,
                $"§8.1 row {row.Name}: Initialize({row.EyeAvailable}, {row.ExpressionAvailable}) -> {result} in {watch.ElapsedMilliseconds} ms; " +
                $"face {(row.NoFaceStart ? (faceAttempted ? "start ATTEMPTED" : "start not attempted") : row.BlockFace ? "port held elsewhere" : faceHeld ? "listening" : "not listening")}, " +
                $"eye {(row.EyeStarts ? (eyeHeld ? "listening" : "not listening") : "start not attempted")}" +
                (missing.Length == 0 ? "" : $"; not logged: {string.Join(" | ", missing)}"), log);
        }
        catch (Exception e)
        {
            rig.Check(false, $"§8.1 row {row.Name}: Initialize threw {e.GetType().Name}: {e.Message}", log);
        }
        finally
        {
            m.Teardown();
            Module.ExpressionNameOverride = null;
            foreach (var blocker in blockers)
            {
                blocker.Dispose();
            }
        }
        var released = Rig.PortFree(facePort) && (!row.EyeStarts || Rig.PortFree(eyePort, Rig.EyeAddress));
        rig.Check(released, $"§8.1 row {row.Name}: after Teardown the face{(row.EyeStarts ? " and eye ports are" : " port is")} free", log);
    }
}
