// SPDX-License-Identifier: MIT
// Helpers of tools/ModuleChecks: PASS/FAIL/SKIP bookkeeping, the settings file in the temporary config folder, modules
// with a recording logger, loopback ports, Baballonia-style bursts and SFEY packets.
using System.Buffers.Binary;
using System.Net;
using System.Net.Sockets;
using System.Text.Json;
using Microsoft.Extensions.Logging;
using SteamFrameEyeModule;
using VRCFaceTracking;
using Module = SteamFrameEyeModule.SteamFrameEyeModule;

/// <summary>Check bookkeeping and the shared test environment.</summary>
sealed class Rig
{
    /// <summary>The eye receiver's address (the module binds 0.0.0.0); the eye-port blocker and probe use it too.</summary>
    public static readonly IPAddress EyeAddress = IPAddress.Any;
    /// <summary>
    /// The SFEY default port, forbidden in TestPorts: the eye port of every run that must not start the eye path, so
    /// an eye start there throws before any socket exists (no 0.0.0.0 bind without MODULECHECKS_ALLOW_WILDCARD).
    /// </summary>
    public const int GuardedEyePort = 9021;
    public const float Marker = 0.123f;

    private uint _seq;
    private double _sampleTime;

    public Rig(string configDir, bool wildcard) => (ConfigDir, Wildcard) = (configDir, wildcard);

    public string ConfigDir { get; }
    /// <summary>MODULECHECKS_ALLOW_WILDCARD=1: the rows that start the eye receiver (0.0.0.0) run too.</summary>
    public bool Wildcard { get; }
    public int Count { get; private set; }
    public int Failures { get; private set; }
    public int Skips { get; private set; }
    public string SettingsPath => Path.Combine(ConfigDir, "SteamFrameEyeModule.json");
    public string CalibrationPath => Path.Combine(ConfigDir, "SteamFrameEyeModule.calibration.json");

    /// <summary>One check; on failure the module's log lines are printed too.</summary>
    public void Check(bool condition, string message, ModuleLog? log = null)
    {
        Count++;
        Console.WriteLine($"{(condition ? "PASS" : "FAIL")}: {message}");
        if (condition)
        {
            return;
        }
        Failures++;
        foreach (var line in log?.Lines ?? Array.Empty<string>())
        {
            Console.WriteLine("  log " + line);
        }
    }

    public void Skip(string message)
    {
        Skips++;
        Console.WriteLine($"SKIP: {message} (starts the eye receiver, which binds 0.0.0.0; runs with MODULECHECKS_ALLOW_WILDCARD=1, as on CI)");
    }

    /// <summary>Writes SteamFrameEyeModule.json; the eye port is the guarded 9021 unless an entry sets "port".</summary>
    public void Settings(IEnumerable<(string Key, object Value)> entries)
    {
        var json = new Dictionary<string, object> { ["port"] = GuardedEyePort };
        foreach (var (key, value) in entries)
        {
            json[key] = value;
        }
        File.WriteAllText(SettingsPath, JsonSerializer.Serialize(json));
    }

    public void Settings(params (string Key, object Value)[] entries) => Settings((IEnumerable<(string, object)>)entries);

    /// <summary>A module as VRCFT creates it: a logger and default metadata, Status Uninitialized.</summary>
    public static (Module Module, ModuleLog Log) NewModule(Action<string>? onLine = null)
    {
        var log = new ModuleLog(onLine);
        return (new Module { Logger = log, ModuleInformation = new ModuleMetadata() }, log);
    }

    /// <summary>VRCFT's status update: Status and (6.0) the claims it gave the module.</summary>
    public static void Status(Module m, bool active, bool usingEye, bool usingExpression)
    {
        var info = m.ModuleInformation;
        info.UsingEye = usingEye;
        info.UsingExpression = usingExpression;
        m.ModuleInformation = info;
        m.Status = active ? VRCFaceTracking.Core.Library.ModuleState.Active : VRCFaceTracking.Core.Library.ModuleState.Idle;
    }

    /// <summary>Sets every shape, the eye data and the head to markers (VRCFT's data is one static object).</summary>
    public static void Markers()
    {
        var d = UnifiedTracking.Data;
        for (var i = 0; i < d.Shapes.Length; i++)
        {
            d.Shapes[i].Weight = Marker;
        }
        d.Eye.Left.Gaze.x = 0.31f;
        d.Eye.Left.Gaze.y = 0.32f;
        d.Eye.Right.Gaze.x = 0.33f;
        d.Eye.Right.Gaze.y = 0.34f;
        d.Eye.Left.Openness = 0.41f;
        d.Eye.Right.Openness = 0.42f;
        d.Eye.Left.PupilDiameter_MM = 0.43f;
        d.Eye.Right.PupilDiameter_MM = 0.44f;
        d.Eye._minDilation = 0.45f;
        d.Eye._maxDilation = 0.46f;
        d.Head.HeadYaw = 0.53f;
        d.Head.HeadPitch = 0.54f;
        d.Head.HeadRoll = 0.55f;
    }

    /// <summary>The eye data and the head still hold <see cref="Markers"/>.</summary>
    public static bool EyeAndHeadUntouched()
    {
        var d = UnifiedTracking.Data;
        return d.Eye.Left.Gaze.x == 0.31f && d.Eye.Left.Gaze.y == 0.32f && d.Eye.Right.Gaze.x == 0.33f && d.Eye.Right.Gaze.y == 0.34f
               && d.Eye.Left.Openness == 0.41f && d.Eye.Right.Openness == 0.42f && d.Eye.Left.PupilDiameter_MM == 0.43f
               && d.Eye.Right.PupilDiameter_MM == 0.44f && d.Eye._minDilation == 0.45f && d.Eye._maxDilation == 0.46f
               && d.Head.HeadYaw == 0.53f && d.Head.HeadPitch == 0.54f && d.Head.HeadRoll == 0.55f;
    }

    public static int FreeUdpPort()
    {
        for (var k = 0; k < 64; k++)
        {
            using var probe = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp);
            probe.Bind(new IPEndPoint(IPAddress.Loopback, 0));
            var port = ((IPEndPoint)probe.LocalEndPoint!).Port;
            if (TestPorts.Forbidden?.Contains(port) != true)
            {
                return port;
            }
        }
        throw new InvalidOperationException("the OS offered only forbidden ports");
    }

    /// <summary>Holds address:port exclusively, like another program on the face or eye port.</summary>
    public static Socket Block(int port, IPAddress? address = null)
    {
        TestPorts.ThrowIfForbidden(port);
        var socket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp);
        try
        {
            socket.ExclusiveAddressUse = true;
        }
        catch (Exception e) when ((e is SocketException or PlatformNotSupportedException) && !OperatingSystem.IsWindows())
        {
            // Without SO_REUSEADDR/SO_REUSEPORT a Unix bind is exclusive anyway.
        }
        try
        {
            socket.Bind(new IPEndPoint(address ?? IPAddress.Loopback, port));
            return socket;
        }
        catch
        {
            socket.Dispose();
            throw;
        }
    }

    /// <summary>True if nothing holds address:port (an exclusive bind succeeds); the probe is closed again.</summary>
    public static bool PortFree(int port, IPAddress? address = null)
    {
        try
        {
            using var probe = Block(port, address);
            return true;
        }
        catch (SocketException)
        {
            return false;
        }
    }

    public static Socket Sender()
    {
        var socket = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp);
        socket.Bind(new IPEndPoint(IPAddress.Loopback, 0));
        return socket;
    }

    public static void Send(Socket sender, int port, byte[] datagram)
    {
        TestPorts.ThrowIfForbidden(port);
        sender.SendTo(datagram, new IPEndPoint(IPAddress.Loopback, port));
    }

    /// <summary>One Baballonia inference: 45 datagrams, one "/name ,f" message each, in its send order.</summary>
    public static void Burst(Socket sender, int port, Func<int, float> value)
    {
        for (var i = 0; i < FaceMapping.Count; i++)
        {
            Send(sender, port, FaceOsc.Float(FaceMapping.Map[i].Address, value(i)));
        }
    }

    /// <summary>One SFEY packet (96 bytes, little-endian) as FrameEye Bridge sends it, 1/90 s after the previous one.</summary>
    public byte[] Sfey(float x, float y, float open)
    {
        var b = new byte[96];
        "SFEY"u8.CopyTo(b);
        BinaryPrimitives.WriteUInt16LittleEndian(b.AsSpan(4), 1);
        BinaryPrimitives.WriteUInt16LittleEndian(b.AsSpan(6), 1); // valid
        BinaryPrimitives.WriteUInt32LittleEndian(b.AsSpan(8), _seq++);
        _sampleTime += 1 / 90.0;
        BinaryPrimitives.WriteDoubleLittleEndian(b.AsSpan(12), _sampleTime);
        float[] f = { x, y, -1, x, y, -1, 10 * x, 10 * y, -10, open, open, 0.0004f, 0.0004f, 0.0004f, 0.0004f, 0.0004f, 0.0004f };
        for (var i = 0; i < f.Length; i++)
        {
            BinaryPrimitives.WriteSingleLittleEndian(b.AsSpan(20 + 4 * i), f[i]);
        }
        BinaryPrimitives.WriteDoubleLittleEndian(b.AsSpan(88), _sampleTime);
        return b;
    }

    /// <summary>The repository file at a path relative to the repository root, searched upwards from the binary.</summary>
    public static string? RepoFile(string relative)
    {
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
        {
            var path = Path.Combine(dir.FullName, relative);
            if (File.Exists(path) && File.Exists(Path.Combine(dir.FullName, "SteamFrameEyeModule.csproj")))
            {
                return path;
            }
        }
        return null;
    }
}

/// <summary>Collects the module's log lines ("Level: text"); an optional callback sees each line as it is logged.</summary>
sealed class ModuleLog : ILogger
{
    private readonly List<string> _lines = new();
    private readonly Action<string>? _onLine;

    public ModuleLog(Action<string>? onLine = null) => _onLine = onLine;

    public string[] Lines
    {
        get
        {
            lock (_lines)
            {
                return _lines.ToArray();
            }
        }
    }

    public int Count(string text) => Lines.Count(l => l.Contains(text, StringComparison.Ordinal));

    public bool Has(string text) => Count(text) > 0;

    public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;

    public bool IsEnabled(LogLevel logLevel) => true;

    public void Log<TState>(LogLevel logLevel, EventId eventId, TState state, Exception? exception, Func<TState, Exception?, string> formatter)
    {
        var line = $"{logLevel}: {formatter(state, exception)}";
        lock (_lines)
        {
            _lines.Add(line);
        }
        _onLine?.Invoke(line);
    }
}
