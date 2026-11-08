// SPDX-License-Identifier: MIT
// Optional end-to-end check of SteamFrameEyeModule against the vendored VRCFaceTracking 5.4.5 references (spec §11.2,
// §11.3 "tools/ModuleHarness"; CI job with continue-on-error). It drives the module's Initialize/Update/Teardown in
// this process and reads UnifiedTracking.Data; it never constructs OscSendService, the OSCQuery service,
// MulticastDnsService or any other VRCFT network service. Every port it binds or sends to comes from the
// environment and is checked against the forbidden set first.
//
//   SFEYE_CONFIG_DIR  a temporary directory (settings, calibration; never %APPDATA%\VRCFaceTracking)
//   SFEYE_PORT        a free UDP port for SFEY
//   SFFACE_PORT       a free UDP port for the Babble OSC receiver
//
// Exit codes: 0 pass, 1 fail, 77 skip (environment missing or invalid, or the VRCFT core failed to load).
//
// With only lib/vrcft-5.4.5 the VRCFT core cannot load (it needs CommunityToolkit.Mvvm and other assemblies that are
// not vendored), so this ends in SKIP (77) everywhere. tools/ModuleChecks runs the same module sources against
// stand-ins for the VRCFT types and gates CI.
using System.Buffers.Binary;
using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text.Json;
using Microsoft.Extensions.Logging;
using SteamFrameEyeModule;
using VRCFaceTracking;
using VRCFaceTracking.Core.Library;
using VRCFaceTracking.Core.Params.Data;
using VRCFaceTracking.Core.Params.Expressions;
using Module = SteamFrameEyeModule.SteamFrameEyeModule;

TestPorts.Forbidden = new HashSet<int> { 8888, 8889, 9000, 9001, 9015, 9020, 9021 };

const int Skip = 77;
var configDir = Environment.GetEnvironmentVariable("SFEYE_CONFIG_DIR");
var eyePortText = Environment.GetEnvironmentVariable("SFEYE_PORT");
var facePortText = Environment.GetEnvironmentVariable("SFFACE_PORT");
if (string.IsNullOrWhiteSpace(configDir) || string.IsNullOrWhiteSpace(eyePortText) || string.IsNullOrWhiteSpace(facePortText))
{
    Console.WriteLine("SKIP: ModuleHarness needs SFEYE_CONFIG_DIR (a temporary directory), SFEYE_PORT and SFFACE_PORT (two free UDP ports)");
    return Skip;
}
if (!int.TryParse(eyePortText, out var eyePort) || !int.TryParse(facePortText, out var facePort)
    || eyePort is < 1024 or > 65535 || !FaceSettings.ValidFacePort(facePort) || eyePort == facePort
    || TestPorts.Forbidden.Contains(eyePort) || TestPorts.Forbidden.Contains(facePort))
{
    Console.WriteLine($"SKIP: SFEYE_PORT={eyePortText} and SFFACE_PORT={facePortText} must be two different free ports (1024-65535, " +
                      "not 8888, 8889, 9000, 9001, 9015, 9020 or 9021)");
    return Skip;
}
configDir = Path.GetFullPath(configDir);
var realConfigDir = Path.GetFullPath(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "VRCFaceTracking"));
if (string.Equals(configDir.TrimEnd(Path.DirectorySeparatorChar), realConfigDir.TrimEnd(Path.DirectorySeparatorChar), StringComparison.OrdinalIgnoreCase)
    || Directory.Exists(Path.Combine(configDir, "CustomLibs")))
{
    Console.WriteLine($"SKIP: SFEYE_CONFIG_DIR={configDir} looks like a real VRCFaceTracking folder; use an empty temporary directory");
    return Skip;
}
Directory.CreateDirectory(configDir);

string? loadError;
try
{
    loadError = Vrcft.Probe();
}
catch (Exception e)
{
    loadError = $"{e.GetType().Name}: {e.Message}";
}
if (loadError is not null)
{
    Console.WriteLine($"SKIP: the VRCFaceTracking core failed to load ({loadError})");
    return Skip;
}
try
{
    return Vrcft.Run(configDir, eyePort, facePort);
}
catch (Exception e)
{
    Console.WriteLine($"FAIL: unexpected exception {e}");
    return 1;
}

/// <summary>Everything that touches VRCFT types lives here, so a skip never loads them.</summary>
static class Vrcft
{
    [MethodImpl(MethodImplOptions.NoInlining)]
    public static string? Probe()
    {
        try
        {
            var shapes = UnifiedTracking.Data.Shapes.Length; // runs the core's static initialisers
            return shapes == (int)UnifiedExpressions.Max + 1 && typeof(Module).BaseType == typeof(ExtTrackingModule)
                ? null
                : $"unexpected SDK: {shapes} shapes";
        }
        catch (Exception e)
        {
            return $"{e.GetType().Name}: {e.Message}";
        }
    }

    [MethodImpl(MethodImplOptions.NoInlining)]
    public static int Run(string configDir, int eyePort, int facePort)
    {
        var h = new Checks(configDir, eyePort, facePort);
        h.ClaimsTable();
        h.BabbleModuleWarning();
        h.EyeAndFaceData();
        h.FacePathTouchesOnlyItsShapes();
        h.CalibrationAndTeardown();
        Console.WriteLine(h.Failures == 0 ? $"All {h.Count} module harness checks passed." : $"{h.Failures} of {h.Count} module harness checks FAILED.");
        return h.Failures == 0 ? 0 : 1;
    }
}

sealed class Checks
{
    private const float Marker = 0.123f;
    private readonly string _dir;
    private readonly int _eyePort, _facePort;
    private uint _seq;
    private double _sampleTime;

    public Checks(string dir, int eyePort, int facePort) => (_dir, _eyePort, _facePort) = (dir, eyePort, facePort);

    public int Failures { get; private set; }
    public int Count { get; private set; }
    private string SettingsPath => Path.Combine(_dir, "SteamFrameEyeModule.json");
    private string CalibrationPath => Path.Combine(_dir, "SteamFrameEyeModule.calibration.json");
    private static UnifiedExpressionShape[] Shapes => UnifiedTracking.Data.Shapes;

    private void Check(bool condition, string message)
    {
        Count++;
        Console.WriteLine($"{(condition ? "PASS" : "FAIL")}: {message}");
        if (!condition) Failures++;
    }

    /// <summary>Settings with the harness ports always set, so neither path can fall back to a default port.</summary>
    private void Settings(params (string Key, object Value)[] extra)
    {
        var json = new Dictionary<string, object> { ["port"] = _eyePort, ["face_port"] = _facePort };
        foreach (var (key, value) in extra) json[key] = value;
        File.WriteAllText(SettingsPath, JsonSerializer.Serialize(json));
    }

    private static (Module Module, List<string> Log) NewModule()
    {
        var log = new List<string>();
        return (new Module { Logger = new ListLogger(log), ModuleInformation = new ModuleMetadata() }, log);
    }

    private static bool Logged(List<string> log, string text)
    {
        lock (log) return log.Any(l => l.Contains(text));
    }

    private static UdpClient Bind(IPAddress address, int port)
    {
        TestPorts.ThrowIfForbidden(port);
        var udp = new UdpClient(AddressFamily.InterNetwork) { ExclusiveAddressUse = true };
        udp.Client.Bind(new IPEndPoint(address, port));
        return udp;
    }

    private bool PortsFree()
    {
        try
        {
            using var eye = Bind(IPAddress.Any, _eyePort);
            using var face = Bind(IPAddress.Loopback, _facePort);
            return true;
        }
        catch (SocketException)
        {
            return false;
        }
    }

    private void Send(UdpClient sender, int port, byte[] datagram)
    {
        TestPorts.ThrowIfForbidden(port);
        sender.Send(datagram, datagram.Length, new IPEndPoint(IPAddress.Loopback, port));
    }

    /// <summary>One SFEY packet (96 bytes, little-endian) as FrameEye Bridge sends it.</summary>
    private byte[] Sfey(float x, float y, float open, double sampleTime)
    {
        var b = new byte[96];
        "SFEY"u8.CopyTo(b);
        BinaryPrimitives.WriteUInt16LittleEndian(b.AsSpan(4), 1);
        BinaryPrimitives.WriteUInt16LittleEndian(b.AsSpan(6), 1); // valid
        BinaryPrimitives.WriteUInt32LittleEndian(b.AsSpan(8), _seq++);
        BinaryPrimitives.WriteDoubleLittleEndian(b.AsSpan(12), sampleTime);
        float[] f = { x, y, -1, x, y, -1, 10 * x, 10 * y, -10, open, open, 0.0004f, 0.0004f, 0.0004f, 0.0004f, 0.0004f, 0.0004f };
        for (var i = 0; i < f.Length; i++) BinaryPrimitives.WriteSingleLittleEndian(b.AsSpan(20 + 4 * i), f[i]);
        BinaryPrimitives.WriteDoubleLittleEndian(b.AsSpan(88), sampleTime);
        return b;
    }

    /// <summary>One Baballonia inference: 45 datagrams, one "/name ,f" message each.</summary>
    private void Burst(UdpClient sender, Func<int, float> value)
    {
        for (var i = 0; i < FaceMapping.Count; i++) Send(sender, _facePort, FaceOsc.Float(FaceMapping.Map[i].Address, value(i)));
    }

    /// <summary>The §8.1 table, every row, with real binds on the harness ports.</summary>
    public void ClaimsTable()
    {
        Row("1: T T babble_osc, both bind", true, true, (true, true), null, face: true);
        Row("2: T T face_source absent", true, true, (true, false), "face_source is \"off\"", face: false);
        Row("3: T T babble_osc, face port in use", true, true, (true, false), $"cannot bind 127.0.0.1:{_facePort} (", face: true, blockFace: true);
        Row("4: eyes not offered", false, true, (false, true), "eye tracking was already claimed by another module", face: true);
        Row("5: expressions not offered", true, false, (true, false), "expressions were already claimed by another module", face: true);
        Row("6: nothing offered (babble_osc)", false, false, (false, false), null, face: true);
        Row("6: nothing offered (off)", false, false, (false, false), null, face: false);
        Row("7: T T babble_osc, eye port in use", true, true, (false, true), $"cannot bind UDP port {_eyePort}", face: true, blockEye: true);
        Row("8: T T babble_osc, enum check failed", true, true, (true, false), "UnifiedExpressions differ from the face mapping", face: true, badEnum: true);
    }

    private void Row(string name, bool eyeAvailable, bool expressionAvailable, (bool, bool) expected, string? logText,
        bool face, bool blockFace = false, bool blockEye = false, bool badEnum = false)
    {
        if (face) Settings(("face_source", "babble_osc")); else Settings();
        var blockers = new List<UdpClient>();
        if (blockFace) blockers.Add(Bind(IPAddress.Loopback, _facePort));
        if (blockEye) blockers.Add(Bind(IPAddress.Any, _eyePort));
        Module.ExpressionNameOverride = badEnum ? i => i == 22 ? "JawOpenRenamed" : Enum.GetName(typeof(UnifiedExpressions), i) : null;
        var (m, log) = NewModule();
        try
        {
            var watch = Stopwatch.StartNew();
            var result = m.Initialize(eyeAvailable, expressionAvailable);
            watch.Stop();
            var claims = $"claims eye={expected.Item1} expression={expected.Item2}";
            Check(result == expected && Logged(log, claims) && (logText is null || Logged(log, logText)) && watch.ElapsedMilliseconds < 1000,
                $"§8.1 row {name}: Initialize({eyeAvailable}, {expressionAvailable}) -> {result} in {watch.ElapsedMilliseconds} ms" +
                (logText is null ? "" : $", logged \"{logText}\""));
        }
        finally
        {
            m.Teardown();
            Module.ExpressionNameOverride = null;
            foreach (var b in blockers) b.Dispose();
        }
        Check(PortsFree(), $"§8.1 row {name}: Teardown released both ports");
    }

    /// <summary>The Babble check looks in SFEYE_CONFIG_DIR\CustomLibs, never in the real VRCFT folder.</summary>
    public void BabbleModuleWarning()
    {
        var folder = Path.Combine(_dir, "CustomLibs", FaceSettings.BabbleModuleId);
        Directory.CreateDirectory(folder);
        File.WriteAllText(Path.Combine(folder, "module.json"), "{}");
        Settings(("face_source", "babble_osc"));
        var (m, log) = NewModule();
        try
        {
            m.Initialize(true, true);
            Check(Logged(log, "the VRCFT-Babble module is installed"), "a VRCFT-Babble module.json under SFEYE_CONFIG_DIR\\CustomLibs is reported");
        }
        finally
        {
            m.Teardown();
            Directory.Delete(Path.Combine(_dir, "CustomLibs"), true);
        }
    }

    public void EyeAndFaceData()
    {
        Settings(("face_source", "babble_osc"), ("smoothing", false), ("calibration", "off"));
        for (var i = 0; i < 12; i++) Shapes[i].Weight = Marker + i / 1000f;
        var (m, log) = NewModule();
        using var sender = new UdpClient(AddressFamily.InterNetwork);
        try
        {
            var result = m.Initialize(true, true);
            m.Status = ModuleState.Active;
            Check(result == (true, true), "eye and face claimed");

            float yaw = 10 * MathF.PI / 180, pitch = 5 * MathF.PI / 180;
            for (var i = 0; i < 30; i++)
            {
                Send(sender, _eyePort, Sfey(MathF.Tan(yaw), MathF.Tan(pitch), 0.9f, _sampleTime += 1 / 90.0));
                Thread.Sleep(5);
                m.Update();
            }
            for (var i = 0; i < 5; i++) m.Update();
            var eye = UnifiedTracking.Data.Eye;
            Check(MathF.Abs(eye.Left.Gaze.x - yaw) < 1e-3f && MathF.Abs(eye.Left.Gaze.y - pitch) < 1e-3f
                  && MathF.Abs(eye.Right.Gaze.x - yaw) < 1e-3f && eye.Left.Openness == 1f && eye.Right.Openness == 1f,
                $"SFEY packets drive gaze ({eye.Left.Gaze.x:F4}, {eye.Left.Gaze.y:F4}) rad and openness ({eye.Left.Openness}, {eye.Right.Openness})");

            var watch = Stopwatch.StartNew();
            long next = 0;
            while (watch.ElapsedMilliseconds < 700)
            {
                if (watch.ElapsedMilliseconds >= next)
                {
                    Burst(sender, i => i == 4 ? 0.6f : i == 10 ? 0.4f : 0f);
                    next += 16;
                }
                m.Update();
            }
            m.Update();
            Check(MathF.Abs(Shapes[22].Weight - 0.6f) < 1e-6f, $"Babble jawOpen 0.6 -> Shapes[22] {Shapes[22].Weight}");
            Check(new[] { 36, 37, 38, 39 }.All(u => MathF.Abs(Shapes[u].Weight - 0.4f) < 1e-6f), "Babble mouthFunnel 0.4 -> Shapes[36..39] 0.4");
            Check(Enumerable.Range(0, 12).All(i => Shapes[i].Weight == Marker + i / 1000f), "markers written into Shapes[0..11] stay untouched");

            var stop = Stopwatch.StartNew();
            long zeroAt = -1;
            while (stop.ElapsedMilliseconds < 1500)
            {
                m.Update();
                if (zeroAt < 0 && Shapes[22].Weight == 0f) zeroAt = stop.ElapsedMilliseconds;
            }
            Check(zeroAt is >= 700 and <= 900, $"after the last message the face fades to 0 within 0.8 s (stale 500 ms + decay 300 ms; 0 after {zeroAt} ms)");
            Check(Logged(log, "relaxing the face"), "the stop is logged");
        }
        finally
        {
            m.Teardown();
        }
    }

    /// <summary>With eye_enabled false the face path alone runs: it writes exactly the §8.3 targets, never Eye or Head.</summary>
    public void FacePathTouchesOnlyItsShapes()
    {
        Settings(("face_source", "babble_osc"), ("eye_enabled", false));
        for (var i = 0; i < Shapes.Length; i++) Shapes[i].Weight = Marker;
        var eye = UnifiedTracking.Data.Eye;
        eye.Left.Gaze.x = 0.31f;
        eye.Right.Openness = 0.42f;
        UnifiedTracking.Data.Head.HeadYaw = 0.53f;
        var (m, log) = NewModule();
        using var sender = new UdpClient(AddressFamily.InterNetwork);
        try
        {
            var result = m.Initialize(true, true);
            m.Status = ModuleState.Active;
            for (var i = 0; i < 10; i++) m.Update();
            Check(result == (false, true) && Shapes.All(s => s.Weight == Marker), "eye_enabled false: (F,T), and nothing is written before the first face message");
            var watch = Stopwatch.StartNew();
            long next = 0;
            while (watch.ElapsedMilliseconds < 600)
            {
                if (watch.ElapsedMilliseconds >= next)
                {
                    Burst(sender, _ => 0.7f);
                    next += 16;
                }
                m.Update();
            }
            m.Update();
            var targets = FaceMapping.Map.SelectMany(r => r.Ue).ToHashSet();
            var wrong = Enumerable.Range(0, Shapes.Length)
                .Where(i => targets.Contains(i) ? MathF.Abs(Shapes[i].Weight - 0.7f) > 1e-6f : Shapes[i].Weight != Marker).ToArray();
            Check(wrong.Length == 0 && targets.Count == 55, $"the face path wrote exactly the 55 §8.3 shapes (wrong: {string.Join(",", wrong)})");
            Check(UnifiedTracking.Data.Eye.Left.Gaze.x == 0.31f && UnifiedTracking.Data.Eye.Right.Openness == 0.42f
                  && UnifiedTracking.Data.Head.HeadYaw == 0.53f, "the face path never writes Eye or Head");
        }
        finally
        {
            m.Teardown();
        }
    }

    /// <summary>3000 SFEY samples with 6 blinks per eye, face data flowing, then Teardown: the calibration is saved
    /// during the Teardown call (eye teardown before the face join) and the whole Teardown takes &lt; 200 ms.</summary>
    public void CalibrationAndTeardown()
    {
        Settings(("face_source", "babble_osc"));
        File.Delete(CalibrationPath);
        var (m, log) = NewModule();
        using var sender = new UdpClient(AddressFamily.InterNetwork);
        var updating = true;
        var loop = new Thread(() => { while (Volatile.Read(ref updating)) m.Update(); }) { IsBackground = true };
        try
        {
            m.Initialize(true, true);
            m.Status = ModuleState.Active;
            loop.Start();
            for (var i = 0; i < 3000; i++)
            {
                var k = (i - 400) / 400;
                var blink = i >= 400 && k < 6 && (i - 400) % 400 < 12; // blinks 133 ms long at samples 400, 800, ... 2400
                Send(sender, _eyePort, Sfey(0f, 0f, blink ? 0.05f : 0.9f, _sampleTime += 1 / 90.0));
                if (i % 45 == 0) Burst(sender, j => j == 4 ? 0.3f : 0f);
                if (i % 30 == 29) Thread.Sleep(3);
            }
            var rxCount = typeof(Module).GetField("_rxCount", BindingFlags.NonPublic | BindingFlags.Instance)!;
            var until = Environment.TickCount64 + 10000;
            while ((long)rxCount.GetValue(m)! < 3000 && Environment.TickCount64 < until) Thread.Sleep(10);
            Check((long)rxCount.GetValue(m)! == 3000, $"all 3000 SFEY samples received ({rxCount.GetValue(m)})");
        }
        finally
        {
            Volatile.Write(ref updating, false);
            if (loop.IsAlive) loop.Join(1000);
        }
        Check(!File.Exists(CalibrationPath), "no calibration file before Teardown (the periodic save starts after 60 s)");
        var watch = Stopwatch.StartNew();
        m.Teardown();
        watch.Stop();
        Check(File.Exists(CalibrationPath), "SteamFrameEyeModule.calibration.json appears in SFEYE_CONFIG_DIR during the Teardown call");
        if (File.Exists(CalibrationPath))
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(CalibrationPath));
            Check(doc.RootElement.GetProperty("left").GetProperty("blinks").GetInt32() >= 5
                  && doc.RootElement.GetProperty("right").GetProperty("blinks").GetInt32() >= 5, "the saved calibration learned the blinks of both eyes");
        }
        Check(watch.ElapsedMilliseconds < 200, $"Teardown with both paths running takes {watch.ElapsedMilliseconds} ms (< 200 ms)");
        Check(Logged(log, "stopped after 3000 packets") && Logged(log, "face messages "), "Teardown logs the eye and face counters");
        Check(PortsFree(), "Teardown released both ports");
    }
}

/// <summary>Collects the module's log lines (and echoes them for the CI log).</summary>
sealed class ListLogger : ILogger
{
    private readonly List<string> _lines;

    public ListLogger(List<string> lines) => _lines = lines;

    public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;

    public bool IsEnabled(LogLevel logLevel) => true;

    public void Log<TState>(LogLevel logLevel, EventId eventId, TState state, Exception? exception, Func<TState, Exception?, string> formatter)
    {
        var line = $"{logLevel}: {formatter(state, exception)}";
        lock (_lines) _lines.Add(line);
        Console.WriteLine("  log " + line);
    }
}
