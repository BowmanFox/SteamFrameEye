using System.Buffers.Binary;
using System.Net;
using System.Net.Sockets;
using System.Text.Json;
using Microsoft.Extensions.Logging;
using VRCFaceTracking;

namespace SteamFrameEyeModule;

/// <summary>
/// VRCFaceTracking module for the Valve Steam Frame's built-in eye tracking.
///
/// The headset runs frameeye_osc.py, which reads the eye-tracking server's shared memory
/// (/dev/shm/eye-server.mmap: per-eye gaze direction, fixation point, eyelid openness) and
/// sends one "SFEY" UDP packet per sample to this PC. This module receives those packets and
/// feeds per-eye gaze and openness into VRCFT, so VRCFT stays the single hub next to a
/// mouth tracker such as Project Babble.
///
/// Gaze mapping follows konsti219/frameeyeosc: atan2(x, -z) * 4/pi, clamped to [-1, 1],
/// i.e. +-45 degrees map to +-1, which is what the VRCFT "v2" eye parameters expect.
/// </summary>
public class SteamFrameEyeModule : ExtTrackingModule
{
    // 9015 is taken by Steam Link's face-tracking OSC output on PCs that use it; stay clear of it.
    private const int DefaultPort = 9021;
    private const int PacketLength = 96;
    private const ushort PacketVersion = 1;
    private const float Scale = 4f / MathF.PI;
    private const double StaleSeconds = 1.5;

    private struct Sample
    {
        public uint Seq;
        public double SampleTime;
        public bool Valid;
        public float LX, LY, LZ, RX, RY, RZ, FX, FY, FZ;
        public float OpenL, OpenR;
    }

    private UdpClient? _udp;
    private Thread? _rxThread;
    private volatile bool _running;
    private readonly object _lock = new();
    private readonly AutoResetEvent _newData = new(false);
    private Sample _latest;
    private bool _have;
    private DateTime _lastRx = DateTime.MinValue;
    private long _rxCount, _badCount;
    private bool _loggedFirst, _loggedStale;
    private int _port = DefaultPort;

    public override (bool SupportsEye, bool SupportsExpression) Supported => (true, false);

    public override (bool eyeSuccess, bool expressionSuccess) Initialize(bool eyeAvailable, bool expressionAvailable)
    {
        ModuleInformation.Name = "Steam Frame Eye (network)";

        if (!eyeAvailable)
        {
            Logger.LogWarning("SteamFrameEye: another module already claimed eye tracking (Steam Link module?); disable it to use the Steam Frame's eyes");
            return (false, false);
        }

        _port = ReadPortSetting();
        try
        {
            _udp = new UdpClient(new IPEndPoint(IPAddress.Any, _port));
            _udp.Client.ReceiveTimeout = 500;
        }
        catch (Exception e)
        {
            Logger.LogError("SteamFrameEye: cannot bind UDP port {Port}: {Message}", _port, e.Message);
            return (false, false);
        }

        _running = true;
        _rxThread = new Thread(ReceiveLoop) { IsBackground = true, Name = "SteamFrameEyeRx" };
        _rxThread.Start();
        Logger.LogInformation(
            "SteamFrameEye: listening on UDP {Port}. On the headset run: frameeye_osc.py --raw <this-pc-ip>:{Port}",
            _port, _port);
        return (true, false);
    }

    public override void Update()
    {
        // Wait briefly for a new packet so this loop does not spin; VRCFT calls Update() repeatedly.
        _newData.WaitOne(20);

        Sample s;
        bool have;
        DateTime last;
        lock (_lock)
        {
            s = _latest;
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
        if (!s.Valid)
        {
            return;
        }

        var (lx, ly) = GazeAngles(s.LX, s.LY, s.LZ);
        var (rx, ry) = GazeAngles(s.RX, s.RY, s.RZ);

        UnifiedTracking.Data.Eye.Left.Gaze.x = lx;
        UnifiedTracking.Data.Eye.Left.Gaze.y = ly;
        UnifiedTracking.Data.Eye.Right.Gaze.x = rx;
        UnifiedTracking.Data.Eye.Right.Gaze.y = ry;
        UnifiedTracking.Data.Eye.Left.Openness = Math.Clamp(s.OpenL, 0f, 1f);
        UnifiedTracking.Data.Eye.Right.Openness = Math.Clamp(s.OpenR, 0f, 1f);
        // No pupil size in the headset data; pin a neutral value so dilation parameters stay sane.
        UnifiedTracking.Data.Eye.Left.PupilDiameter_MM = 5f;
        UnifiedTracking.Data.Eye.Right.PupilDiameter_MM = 5f;
        UnifiedTracking.Data.Eye._minDilation = 0f;
        UnifiedTracking.Data.Eye._maxDilation = 10f;

        if (!_loggedFirst)
        {
            Logger.LogInformation("SteamFrameEye: first sample seq={Seq} L=({Lx:F2},{Ly:F2}) R=({Rx:F2},{Ry:F2}) lids=({Ol:F2},{Or:F2})",
                s.Seq, lx, ly, rx, ry, s.OpenL, s.OpenR);
            _loggedFirst = true;
        }
    }

    public override void Teardown()
    {
        _running = false;
        try { _udp?.Close(); } catch { /* ignore */ }
        _rxThread?.Join(1000);
        Logger.LogInformation("SteamFrameEye: stopped after {Rx} packets ({Bad} rejected)", _rxCount, _badCount);
    }

    private void ReceiveLoop()
    {
        var any = new IPEndPoint(IPAddress.Any, 0);
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

            if (TryParse(buf, out var s))
            {
                lock (_lock)
                {
                    _latest = s;
                    _have = true;
                    _lastRx = DateTime.UtcNow;
                }
                _rxCount++;
                _newData.Set();
            }
            else
            {
                _badCount++;
            }
        }
    }

    /// <summary>
    /// Packet (little-endian, 96 bytes): "SFEY", u16 version, u16 flags (bit0 = valid), u32 seq,
    /// f64 sample_time, f32 gazeL[3], gazeR[3], fixation[3], openness L, openness R,
    /// f32 covL[3], covR[3], f64 send_time.
    /// </summary>
    private static bool TryParse(ReadOnlySpan<byte> b, out Sample s)
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
        s.Seq = BinaryPrimitives.ReadUInt32LittleEndian(b.Slice(8));
        s.SampleTime = BinaryPrimitives.ReadDoubleLittleEndian(b.Slice(12));
        s.LX = ReadF(b, 0); s.LY = ReadF(b, 1); s.LZ = ReadF(b, 2);
        s.RX = ReadF(b, 3); s.RY = ReadF(b, 4); s.RZ = ReadF(b, 5);
        s.FX = ReadF(b, 6); s.FY = ReadF(b, 7); s.FZ = ReadF(b, 8);
        s.OpenL = ReadF(b, 9); s.OpenR = ReadF(b, 10);
        s.Valid = (flags & 1) != 0
                  && float.IsFinite(s.LX) && float.IsFinite(s.LY) && float.IsFinite(s.LZ)
                  && float.IsFinite(s.RX) && float.IsFinite(s.RY) && float.IsFinite(s.RZ)
                  && float.IsFinite(s.OpenL) && float.IsFinite(s.OpenR);
        return true;
    }

    /// <summary>i-th float of the payload that starts at byte 20.</summary>
    private static float ReadF(ReadOnlySpan<byte> b, int i) =>
        BinaryPrimitives.ReadSingleLittleEndian(b.Slice(20 + i * 4));

    private static (float x, float y) GazeAngles(float x, float y, float z)
    {
        var ax = MathF.Atan2(x, -z) * Scale;
        var ay = MathF.Atan2(y, -z) * Scale;
        return (Math.Clamp(ax, -1f, 1f), Math.Clamp(ay, -1f, 1f));
    }

    /// <summary>Port from %APPDATA%\VRCFaceTracking\SteamFrameEyeModule.json {"port": 9015} or env SFEYE_PORT.</summary>
    private int ReadPortSetting()
    {
        var port = DefaultPort;
        try
        {
            var cfg = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                "VRCFaceTracking", "SteamFrameEyeModule.json");
            if (File.Exists(cfg))
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(cfg));
                if (doc.RootElement.TryGetProperty("port", out var pe) && pe.TryGetInt32(out var p) && p > 0 && p < 65536)
                {
                    port = p;
                }
            }
        }
        catch (Exception e)
        {
            Logger.LogWarning("SteamFrameEye: could not read SteamFrameEyeModule.json: {Message}", e.Message);
        }
        var env = Environment.GetEnvironmentVariable("SFEYE_PORT");
        if (int.TryParse(env, out var ep) && ep > 0 && ep < 65536)
        {
            port = ep;
        }
        return port;
    }
}
