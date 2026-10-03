using System.Buffers.Binary;
using System.Net;
using System.Net.Sockets;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using VRCFaceTracking;

namespace SteamFrameEyeModule;

/// <summary>
/// VRCFaceTracking module for the Valve Steam Frame's built-in eye tracking.
///
/// FrameEye Bridge on the headset reads the eye-tracking server's shared memory
/// (/dev/shm/eye-server.mmap: per-eye gaze direction, fixation point, eyelid openness) and
/// sends one "SFEY" UDP packet per sample to this PC. This module receives those packets,
/// processes them (EyeProcessor: dropout removal, per-eye eyelid calibration, One Euro smoothing,
/// gaze held during blinks) and feeds gaze and openness into VRCFT, so VRCFT stays the single
/// hub next to a mouth tracker such as Project Babble.
///
/// Values are written the way the Steam Link module writes a Quest Pro's: one combined gaze for
/// both eyes as atan2(x, -z), atan2(y, -z) in radians, openness 0 (closed) .. 1 (open), pupil pinned.
/// </summary>
public class SteamFrameEyeModule : ExtTrackingModule
{
    private const int PacketLength = 96;
    private const ushort PacketVersion = 1;
    private const double StaleSeconds = 1.5;
    private static readonly TimeSpan CalibrationSaveInterval = TimeSpan.FromSeconds(60);
    private static readonly TimeSpan CalibrationLogInterval = TimeSpan.FromMinutes(5);

    private static string ConfigDir => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "VRCFaceTracking");
    private static string SettingsPath => Path.Combine(ConfigDir, "SteamFrameEyeModule.json");
    private static string CalibrationPath => Path.Combine(ConfigDir, "SteamFrameEyeModule.calibration.json");

    private UdpClient? _udp;
    private Thread? _rxThread;
    private volatile bool _running;
    private readonly object _lock = new();
    private readonly AutoResetEvent _newData = new(false);
    private EyeSettings _settings = new();
    private EyeProcessor? _processor;
    private EyeOutput _latest;
    private bool _have;
    private DateTime _lastRx = DateTime.MinValue;
    private long _rxCount, _badCount;
    private bool _loggedFirst, _loggedStale;
    private DateTime _nextSave, _nextCalibrationLog;

    public override (bool SupportsEye, bool SupportsExpression) Supported => (true, false);

    public override (bool eyeSuccess, bool expressionSuccess) Initialize(bool eyeAvailable, bool expressionAvailable)
    {
        ModuleInformation.Name = "Steam Frame Eye (network)";

        if (!eyeAvailable)
        {
            Logger.LogWarning("SteamFrameEye: another module already claimed eye tracking (Steam Link module?); disable it to use the Steam Frame's eyes");
            return (false, false);
        }

        _settings = EyeSettings.Load(SettingsPath, m => Logger.LogWarning("SteamFrameEye: {Message}", m));
        if (int.TryParse(Environment.GetEnvironmentVariable("SFEYE_PORT"), out var envPort) && envPort is > 0 and < 65536)
        {
            _settings.Port = envPort;
        }
        _processor = new EyeProcessor(_settings, LoadCalibration());

        try
        {
            _udp = new UdpClient(new IPEndPoint(IPAddress.Any, _settings.Port));
            _udp.Client.ReceiveTimeout = 500;
        }
        catch (Exception e)
        {
            Logger.LogError("SteamFrameEye: cannot bind UDP port {Port}: {Message}", _settings.Port, e.Message);
            return (false, false);
        }

        _nextSave = DateTime.UtcNow + CalibrationSaveInterval;
        _nextCalibrationLog = DateTime.UtcNow + TimeSpan.FromSeconds(60);
        _running = true;
        _rxThread = new Thread(ReceiveLoop) { IsBackground = true, Name = "SteamFrameEyeRx" };
        _rxThread.Start();
        Logger.LogInformation(
            "SteamFrameEye: listening on UDP {Port}; on the headset, enter this PC's address in FrameEye Bridge. " +
            "Gaze {Gaze}, smoothing {Smoothing}, calibration {Calibration} ({Lids})",
            _settings.Port, _settings.CombinedGaze ? "combined" : "per eye", _settings.Smoothing ? "on" : "off",
            _settings.Calibration, DescribeLids(_processor));
        if (_settings.Smoothing)
        {
            Logger.LogInformation("SteamFrameEye: gaze smoothing median {Median}, minimum cutoff {Cutoff:F2} Hz, beta {Beta:F2}",
                _settings.GazeMedianFilter ? "on" : "off", _settings.GazeMinCutoff, _settings.GazeBeta);
        }
        return (true, false);
    }

    public override void Update()
    {
        // Wait briefly for a new packet so this loop does not spin; VRCFT calls Update() repeatedly.
        _newData.WaitOne(20);

        EyeOutput o;
        bool have;
        DateTime last;
        lock (_lock)
        {
            o = _latest;
            have = _have;
            last = _lastRx;
        }
        if (!have)
        {
            return;
        }

        var age = (DateTime.UtcNow - last).TotalSeconds;
        if (age > StaleSeconds)
        {
            if (!_loggedStale)
            {
                Logger.LogWarning("SteamFrameEye: no packets for {Age:F1} s (headset asleep, not worn, or sender stopped); holding last values", age);
                _loggedStale = true;
            }
            return;
        }
        if (_loggedStale)
        {
            Logger.LogInformation("SteamFrameEye: packets resumed");
            _loggedStale = false;
        }

        UnifiedTracking.Data.Eye.Left.Gaze.x = o.LeftX;
        UnifiedTracking.Data.Eye.Left.Gaze.y = o.LeftY;
        UnifiedTracking.Data.Eye.Right.Gaze.x = o.RightX;
        UnifiedTracking.Data.Eye.Right.Gaze.y = o.RightY;
        UnifiedTracking.Data.Eye.Left.Openness = o.OpenL;
        UnifiedTracking.Data.Eye.Right.Openness = o.OpenR;
        // No pupil size in the headset data; pin a neutral value so dilation parameters stay sane
        // (the Steam Link module does the same for a Quest Pro).
        UnifiedTracking.Data.Eye.Left.PupilDiameter_MM = 5f;
        UnifiedTracking.Data.Eye.Right.PupilDiameter_MM = 5f;
        UnifiedTracking.Data.Eye._minDilation = 0f;
        UnifiedTracking.Data.Eye._maxDilation = 10f;

        if (!_loggedFirst)
        {
            Logger.LogInformation("SteamFrameEye: first sample gaze L=({Lx:F2},{Ly:F2}) R=({Rx:F2},{Ry:F2}) rad, lids=({Ol:F2},{Or:F2})",
                o.LeftX, o.LeftY, o.RightX, o.RightY, o.OpenL, o.OpenR);
            _loggedFirst = true;
        }
    }

    public override void Teardown()
    {
        _running = false;
        try { _udp?.Close(); } catch { /* ignore */ }
        _rxThread?.Join(1000);
        if (_processor is not null && _settings.Calibration == "auto" && CalibrationWorthSaving(_processor))
        {
            SaveCalibration(_processor);
        }
        Logger.LogInformation("SteamFrameEye: stopped after {Rx} packets ({Bad} rejected); {Lids}",
            _rxCount, _badCount, _processor is null ? "-" : DescribeLids(_processor));
    }

    private void ReceiveLoop()
    {
        var any = new IPEndPoint(IPAddress.Any, 0);
        var processor = _processor!;
        while (_running && _udp != null)
        {
            byte[] buf;
            try
            {
                buf = _udp.Receive(ref any);
            }
            catch (SocketException)
            {
                continue; // receive timeout or transient error
            }
            catch (ObjectDisposedException)
            {
                break;
            }

            if (!TryParse(buf, out var s))
            {
                _badCount++;
                continue;
            }
            _rxCount++;
            if (!s.Valid)
            {
                continue;
            }
            var o = processor.Process(s);
            lock (_lock)
            {
                _latest = o;
                _have = true;
                _lastRx = DateTime.UtcNow;
            }
            _newData.Set();

            var now = DateTime.UtcNow;
            if (now >= _nextSave && _settings.Calibration == "auto" && CalibrationWorthSaving(processor))
            {
                _nextSave = now + CalibrationSaveInterval;
                SaveCalibration(processor);
            }
            if (now >= _nextCalibrationLog)
            {
                _nextCalibrationLog = now + CalibrationLogInterval;
                Logger.LogInformation("SteamFrameEye: {Lids}; gaze held {Held:F1} %, low-confidence samples (closed eyes or poor fit) {Bad:F1} %",
                    DescribeLids(processor), 100.0 * processor.GazeHeldSamples / Math.Max(1, processor.Samples),
                    100.0 * processor.UnreliableSamples / Math.Max(1, processor.Samples));
            }
        }
    }

    private static string DescribeLids(EyeProcessor p) =>
        $"lids left closed {p.Left.Calibration.Closed:F2} open {p.Left.Calibration.Open:F2}, " +
        $"right closed {p.Right.Calibration.Closed:F2} open {p.Right.Calibration.Open:F2} " +
        $"({p.Left.Calibration.BlinksSeen}/{p.Right.Calibration.BlinksSeen} blinks)";

    /// <summary>Only keep what was learned from enough good data: ~30 s of reliable samples and a few blinks per eye.</summary>
    private static bool CalibrationWorthSaving(EyeProcessor p) =>
        p.ReliableSamples >= 2700 && p.Left.Calibration.BlinksSeen >= 5 && p.Right.Calibration.BlinksSeen >= 5;

    private JsonNode? LoadCalibration()
    {
        if (_settings.Calibration != "auto" || !File.Exists(CalibrationPath))
        {
            return null;
        }
        try
        {
            return JsonNode.Parse(File.ReadAllText(CalibrationPath));
        }
        catch (Exception e)
        {
            Logger.LogWarning("SteamFrameEye: ignoring {File}: {Message}", Path.GetFileName(CalibrationPath), e.Message);
            return null;
        }
    }

    private void SaveCalibration(EyeProcessor p)
    {
        try
        {
            Directory.CreateDirectory(ConfigDir);
            var tmp = CalibrationPath + ".tmp";
            File.WriteAllText(tmp, p.CalibrationJson().ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
            File.Move(tmp, CalibrationPath, true);
        }
        catch (Exception e)
        {
            Logger.LogWarning("SteamFrameEye: could not save the eyelid calibration: {Message}", e.Message);
        }
    }

    /// <summary>
    /// Packet (little-endian, 96 bytes): "SFEY", u16 version, u16 flags (bit0 = valid), u32 seq,
    /// f64 sample_time, f32 gazeL[3], gazeR[3], fixation[3], openness L, openness R,
    /// f32 covL[3], covR[3], f64 send_time.
    /// </summary>
    private static bool TryParse(ReadOnlySpan<byte> b, out EyeSample s)
    {
        s = default;
        if (b.Length < PacketLength || b[0] != (byte)'S' || b[1] != (byte)'F' || b[2] != (byte)'E' || b[3] != (byte)'Y')
        {
            return false;
        }
        if (BinaryPrimitives.ReadUInt16LittleEndian(b.Slice(4)) != PacketVersion)
        {
            return false;
        }
        var flags = BinaryPrimitives.ReadUInt16LittleEndian(b.Slice(6));
        s.SampleTime = BinaryPrimitives.ReadDoubleLittleEndian(b.Slice(12));
        s.LX = ReadF(b, 0); s.LY = ReadF(b, 1); s.LZ = ReadF(b, 2);
        s.RX = ReadF(b, 3); s.RY = ReadF(b, 4); s.RZ = ReadF(b, 5);
        s.FX = ReadF(b, 6); s.FY = ReadF(b, 7); s.FZ = ReadF(b, 8);
        s.OpenL = ReadF(b, 9); s.OpenR = ReadF(b, 10);
        s.CovL = MathF.Max(ReadF(b, 11), MathF.Max(ReadF(b, 12), ReadF(b, 13)));
        s.CovR = MathF.Max(ReadF(b, 14), MathF.Max(ReadF(b, 15), ReadF(b, 16)));
        s.Valid = (flags & 1) != 0
                  && double.IsFinite(s.SampleTime)
                  && float.IsFinite(s.LX) && float.IsFinite(s.LY) && float.IsFinite(s.LZ)
                  && float.IsFinite(s.RX) && float.IsFinite(s.RY) && float.IsFinite(s.RZ)
                  && float.IsFinite(s.OpenL) && float.IsFinite(s.OpenR);
        return true;
    }

    /// <summary>i-th float of the payload that starts at byte 20.</summary>
    private static float ReadF(ReadOnlySpan<byte> b, int i) =>
        BinaryPrimitives.ReadSingleLittleEndian(b.Slice(20 + i * 4));
}
