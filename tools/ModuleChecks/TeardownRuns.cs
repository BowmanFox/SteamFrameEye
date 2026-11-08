// SPDX-License-Identifier: MIT
// Teardown as VRCFT calls it (from another thread, without waiting for the update loop's last Update): the §8.1
// order (face socket closed and the keeper's ",F" sent first, then the eye teardown, then the face thread joined and
// its counters logged) and its time (< 200 ms). With the eye path (MODULECHECKS_ALLOW_WILDCARD=1): SFEY packets drive
// gaze and openness, and with both paths running the calibration is saved during the Teardown call.
using System.Diagnostics;
using System.Reflection;
using System.Net.Sockets;
using System.Text.Json;
using SteamFrameEyeModule;
using VRCFaceTracking;
using Module = SteamFrameEyeModule.SteamFrameEyeModule;

static class TeardownRuns
{
    public static void FaceOnly(Rig rig)
    {
        var facePort = Rig.FreeUdpPort();
        rig.Settings(("face_source", "babble_osc"), ("eye_enabled", false), ("face_port", facePort), ("status_keeper", "face"),
            ("status_keeper_port", Rig.FreeUdpPort()));
        var events = new List<string>();
        void Event(string text)
        {
            lock (events)
            {
                events.Add(text);
            }
        }
        string[] Events()
        {
            lock (events)
            {
                return events.ToArray();
            }
        }
        bool? faceClosedAtEyeTeardown = null;
        Module.HostReportsClaimsOverride = false;
        Module.StatusKeeperSendOverride = d => Event("keeper " + KeeperRuns.Describe(d));
        var (m, log) = Rig.NewModule(line =>
        {
            Event("log " + line);
            if (line.Contains("SteamFrameEye: stopped after ", StringComparison.Ordinal))
            {
                faceClosedAtEyeTeardown = Rig.PortFree(facePort); // the eye teardown runs: is the face socket closed?
            }
        });
        using var sender = Rig.Sender();
        bool stopUpdates = false, stopSending = false, tornDown = false;
        Thread? update = null, baballonia = null;
        long teardownMs = -1;
        var ready = false;
        try
        {
            m.Initialize(true, true);
            Rig.Status(m, true, false, false);
            update = new Thread(() =>
            {
                while (!Volatile.Read(ref stopUpdates))
                {
                    m.Update();
                }
            }) { IsBackground = true, Name = "VRCFT update loop" };
            update.Start();
            baballonia = new Thread(() =>
            {
                try
                {
                    while (!Volatile.Read(ref stopSending))
                    {
                        Rig.Burst(sender, facePort, _ => 0.6f);
                        Thread.Sleep(16);
                    }
                }
                catch (SocketException)
                {
                    // the module closed its port: Baballonia's sends just go nowhere
                }
            }) { IsBackground = true, Name = "Baballonia" };
            baballonia.Start();
            var wait = Stopwatch.StartNew();
            while (!ready && wait.ElapsedMilliseconds < 3000)
            {
                Thread.Sleep(10);
                ready = UnifiedTracking.Data.Shapes[22].Weight == 0.6f && Events().Contains("keeper Lip ,T");
            }
            // VRCFT stops the update loop and calls Teardown at once; the last Update may still be running.
            Volatile.Write(ref stopUpdates, true);
            var watch = Stopwatch.StartNew();
            m.Teardown();
            teardownMs = watch.ElapsedMilliseconds;
            tornDown = true;
        }
        finally
        {
            Volatile.Write(ref stopUpdates, true);
            update?.Join(1000);
            if (!tornDown)
            {
                m.Teardown();
            }
            Volatile.Write(ref stopSending, true);
            baballonia?.Join(1000);
            Module.HostReportsClaimsOverride = null;
            Module.StatusKeeperSendOverride = null;
        }
        var keeperBeforeLate = Events().Count(e => e.StartsWith("keeper ", StringComparison.Ordinal));
        m.Update(); // a late Update after Teardown must not make the keeper send again
        var all = Events();
        int Index(Func<string, bool> match) => Array.FindIndex(all, e => match(e));
        var lipOff = Index(e => e == "keeper Lip ,F");
        var expressionOff = Index(e => e == "keeper Expression ,F");
        var eyeTeardown = Index(e => e.Contains("SteamFrameEye: stopped after ", StringComparison.Ordinal));
        var faceSummary = Index(e => e.Contains("SteamFrameEye: face messages ", StringComparison.Ordinal));
        var lastKeeper = Array.FindLastIndex(all, e => e.StartsWith("keeper ", StringComparison.Ordinal));
        var rx = (FaceReceiver?)typeof(Module).GetField("_faceRx", BindingFlags.NonPublic | BindingFlags.Instance)?.GetValue(m);
        rig.Check(ready, "face-only module with status_keeper face: the face is live and the keeper sent ,T before Teardown", log);
        rig.Check(lipOff >= 0 && expressionOff >= 0 && eyeTeardown > Math.Max(lipOff, expressionOff) && faceSummary > eyeTeardown
                  && faceClosedAtEyeTeardown == true,
            $"Teardown order: face socket closed and the keeper's ,F sent (events {lipOff}, {expressionOff}), then the eye teardown " +
            $"(event {eyeTeardown}; face port already free: {faceClosedAtEyeTeardown}), then the face counters (event {faceSummary})", log);
        rig.Check(teardownMs is >= 0 and < 200, $"Teardown with the face path live and the update loop running takes {teardownMs} ms (< 200 ms)", log);
        rig.Check(lastKeeper == Math.Max(lipOff, expressionOff) && all.Count(e => e.StartsWith("keeper ", StringComparison.Ordinal)) == keeperBeforeLate,
            "nothing is sent after the keeper's final ,F, not even by an Update during or after Teardown", log);
        // Windows ends a pending receive when its socket closes (Teardown's 100 ms join suffices); elsewhere the 500 ms
        // receive timeout bounds it.
        var joinMs = OperatingSystem.IsWindows() ? 0 : 1000;
        rig.Check(rx is { IsListening: false } && rx.Join(joinMs) && Rig.PortFree(facePort),
            $"after Teardown the face receive thread has ended (within {joinMs} ms more) and the face port is free", log);
    }

    public static void EyeAndFace(Rig rig)
    {
        if (!rig.Wildcard)
        {
            rig.Skip("eye path: SFEY packets drive gaze and openness; with both paths running Teardown takes < 200 ms and saves the calibration during the call");
            return;
        }
        Gaze(rig);
        Calibration(rig);
    }

    private static (int Eye, int Face) TwoPorts()
    {
        var face = Rig.FreeUdpPort();
        var eye = Rig.FreeUdpPort();
        while (eye == face)
        {
            eye = Rig.FreeUdpPort();
        }
        return (eye, face);
    }

    /// <summary>SFEY drives gaze and openness, Babble drives the face, and Shapes[0..11] stay untouched (§8.1, §11.3).</summary>
    private static void Gaze(Rig rig)
    {
        var (eyePort, facePort) = TwoPorts();
        rig.Settings(("face_source", "babble_osc"), ("port", eyePort), ("face_port", facePort), ("smoothing", false), ("calibration", "off"));
        Rig.Markers();
        var shapes = UnifiedTracking.Data.Shapes;
        var (m, log) = Rig.NewModule();
        using var sender = Rig.Sender();
        try
        {
            var result = m.Initialize(true, true);
            Rig.Status(m, true, true, true); // the stand-in host reports the claims, as VRCFT 6.0 does
            rig.Check(result == (true, true), $"eye path: Initialize(True, True) with both receivers -> {result}", log);
            float yaw = 10 * MathF.PI / 180, pitch = 5 * MathF.PI / 180;
            for (var i = 0; i < 30; i++)
            {
                Rig.Send(sender, eyePort, rig.Sfey(MathF.Tan(yaw), MathF.Tan(pitch), 0.9f));
                Thread.Sleep(5);
                m.Update();
            }
            for (var i = 0; i < 5; i++)
            {
                m.Update();
            }
            var eye = UnifiedTracking.Data.Eye;
            rig.Check(MathF.Abs(eye.Left.Gaze.x - yaw) < 1e-3f && MathF.Abs(eye.Left.Gaze.y - pitch) < 1e-3f && MathF.Abs(eye.Right.Gaze.x - yaw) < 1e-3f
                      && MathF.Abs(eye.Right.Gaze.y - pitch) < 1e-3f && eye.Left.Openness == 1f && eye.Right.Openness == 1f,
                $"eye path: SFEY packets drive gaze ({eye.Left.Gaze.x:F4}, {eye.Left.Gaze.y:F4}) rad for ({yaw:F4}, {pitch:F4}) and openness ({eye.Left.Openness}, {eye.Right.Openness})", log);
            var watch = Stopwatch.StartNew();
            long next = 0;
            while (watch.ElapsedMilliseconds < 3000 && !(shapes[22].Weight == 0.6f && new[] { 36, 37, 38, 39 }.All(u => shapes[u].Weight == 0.4f)))
            {
                if (watch.ElapsedMilliseconds >= next)
                {
                    Rig.Burst(sender, facePort, i => i == 4 ? 0.6f : i == 10 ? 0.4f : 0f);
                    next += 16;
                }
                m.Update();
            }
            rig.Check(shapes[22].Weight == 0.6f && new[] { 36, 37, 38, 39 }.All(u => shapes[u].Weight == 0.4f),
                $"eye path: with both receivers Babble jawOpen 0.6 -> Shapes[22] {shapes[22].Weight}, mouthFunnel 0.4 -> Shapes[36..39]", log);
            rig.Check(Enumerable.Range(0, 12).All(i => shapes[i].Weight == Rig.Marker), "eye path: markers in Shapes[0..11] stay untouched with both paths running", log);
        }
        finally
        {
            m.Teardown();
        }
        rig.Check(Rig.PortFree(facePort) && Rig.PortFree(eyePort, Rig.EyeAddress), "eye path: Teardown released both ports", log);
    }

    /// <summary>
    /// 3000 SFEY samples with 6 blinks per eye while face data flows, then Teardown: the calibration is saved during the
    /// call (eye teardown before the face join) and the whole Teardown takes &lt; 200 ms (§8.1, §11.3).
    /// </summary>
    private static void Calibration(Rig rig)
    {
        var (eyePort, facePort) = TwoPorts();
        rig.Settings(("face_source", "babble_osc"), ("port", eyePort), ("face_port", facePort));
        File.Delete(rig.CalibrationPath);
        var (m, log) = Rig.NewModule();
        using var sender = Rig.Sender();
        var stop = false;
        var tornDown = false;
        Thread? update = null;
        try
        {
            m.Initialize(true, true);
            Rig.Status(m, true, true, true);
            update = new Thread(() =>
            {
                while (!Volatile.Read(ref stop))
                {
                    m.Update();
                }
            }) { IsBackground = true, Name = "VRCFT update loop" };
            update.Start();
            for (var i = 0; i < 3000; i++)
            {
                var k = (i - 400) / 400;
                var blink = i >= 400 && k < 6 && (i - 400) % 400 < 12; // 133 ms blinks at samples 400, 800, ... 2400
                Rig.Send(sender, eyePort, rig.Sfey(0f, 0f, blink ? 0.05f : 0.9f));
                if (i % 45 == 0)
                {
                    Rig.Burst(sender, facePort, j => j == 4 ? 0.3f : 0f);
                }
                if (i % 30 == 29)
                {
                    Thread.Sleep(3);
                }
            }
            var rxCount = typeof(Module).GetField("_rxCount", BindingFlags.NonPublic | BindingFlags.Instance)!;
            var until = Stopwatch.StartNew();
            while ((long)rxCount.GetValue(m)! < 3000 && until.ElapsedMilliseconds < 10000)
            {
                Thread.Sleep(10);
            }
            rig.Check((long)rxCount.GetValue(m)! == 3000, $"eye path: all 3000 SFEY samples received ({rxCount.GetValue(m)})", log);
            rig.Check(!File.Exists(rig.CalibrationPath), "eye path: no calibration file before Teardown (the periodic save starts after 60 s)", log);
            Volatile.Write(ref stop, true);
            var watch = Stopwatch.StartNew();
            m.Teardown();
            watch.Stop();
            tornDown = true;
            var saved = File.Exists(rig.CalibrationPath);
            var blinks = false;
            if (saved)
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(rig.CalibrationPath));
                blinks = doc.RootElement.GetProperty("left").GetProperty("blinks").GetInt32() >= 5
                         && doc.RootElement.GetProperty("right").GetProperty("blinks").GetInt32() >= 5;
            }
            rig.Check(saved && blinks, "eye path: SteamFrameEyeModule.calibration.json with the blinks of both eyes appears during the Teardown call", log);
            rig.Check(watch.ElapsedMilliseconds < 200, $"eye path: Teardown with both paths running takes {watch.ElapsedMilliseconds} ms (< 200 ms)", log);
            rig.Check(log.Has("stopped after 3000 packets") && log.Has("face messages "), "eye path: Teardown logs the eye and face counters", log);
        }
        finally
        {
            Volatile.Write(ref stop, true);
            update?.Join(1000);
            if (!tornDown)
            {
                m.Teardown();
            }
        }
        rig.Check(Rig.PortFree(facePort) && Rig.PortFree(eyePort, Rig.EyeAddress), "eye path: Teardown released both ports", log);
    }
}
