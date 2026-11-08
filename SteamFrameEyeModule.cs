// SPDX-License-Identifier: MIT
using System.Buffers.Binary;
using System.Net;
using System.Net.Sockets;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using VRCFaceTracking;
using VRCFaceTracking.Core.Library;
using VRCFaceTracking.Core.Params.Expressions;

namespace SteamFrameEyeModule;

/// <summary>
/// VRCFaceTracking module for the Valve Steam Frame: its built-in eye tracking, plus mouth and face expressions
/// from Baballonia for a VIVE Facial Tracker on the Frame (FrameFace).
///
/// Eyes: FrameEye Bridge on the headset reads the eye-tracking server's shared memory
/// (/dev/shm/eye-server.mmap: per-eye gaze direction, fixation point, eyelid openness) and
/// sends one "SFEY" UDP packet per sample to this PC. This module receives those packets,
/// processes them (EyeProcessor: dropout removal, per-eye eyelid calibration, One Euro smoothing,
/// gaze held during blinks) and feeds gaze and openness into VRCFT, so VRCFT stays the single
/// hub for eyes and face.
///
/// Values are written the way the Steam Link module writes a Quest Pro's: one combined gaze for
/// both eyes as atan2(x, -z), atan2(y, -z) in radians, openness 0 (closed) .. 1 (open), pupil pinned.
///
/// Face (only with "face_source": "babble_osc"): Baballonia sends 45 Babble OSC messages per inference to
/// 127.0.0.1:8888; FaceReceiver takes them, FaceGate fades them in and out, and FaceMapping writes the 55
/// UnifiedExpressions shapes of spec §8.3. The face path never writes Eye, Head or Shapes[0..11].
/// One module claims both capabilities (same ModuleId as 0.5.3, which it replaces), because VRCFT keeps the first
/// claim of each and the second module's init would reset the other capability's status (G1).
/// </summary>
public class SteamFrameEyeModule : ExtTrackingModule
{
    private const int PacketLength = 96;
    private const ushort PacketVersion = 1;
    private const double StaleSeconds = 1.5;
    private static readonly TimeSpan CalibrationSaveInterval = TimeSpan.FromSeconds(60);
    private static readonly TimeSpan CalibrationLogInterval = TimeSpan.FromMinutes(5);
    private const long KeeperTickMs = 100;

    /// <summary>Settings, calibration and the CustomLibs folder: %APPDATA%\VRCFaceTracking, or SFEYE_CONFIG_DIR (tests).</summary>
    private static string ConfigDir =>
        Environment.GetEnvironmentVariable("SFEYE_CONFIG_DIR") is { Length: > 0 } dir
            ? dir
            : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "VRCFaceTracking");
    private static string SettingsPath => Path.Combine(ConfigDir, "SteamFrameEyeModule.json");
    private static string CalibrationPath => Path.Combine(ConfigDir, "SteamFrameEyeModule.calibration.json");

    private UdpClient? _udp;
    private Thread? _rxThread;
    private volatile bool _running;
    private readonly object _lock = new();
    // Wakes Update(): set by the SFEY receiver and by the face receiver (for the last row of each Baballonia inference,
    // otherwise at most every 5 ms).
    private readonly AutoResetEvent _newData = new(false);
    private EyeSettings _settings = new();
    private EyeProcessor? _processor;
    private EyeOutput _latest;
    private bool _have;
    private DateTime _lastRx = DateTime.MinValue;
    private long _rxCount, _badCount;
    private bool _loggedFirst, _loggedStale, _winksOff, _symmetryOff;
    private DateTime _nextSave, _nextCalibrationLog;

    private static readonly Func<long> Clock = () => Environment.TickCount64;
    /// <summary>A claim-reporting host's claims are judged from this long after Initialize on.</summary>
    private const long ClaimCheckMs = 1000;
    /// <summary>VRCFT 6.0 (SDK assembly 6.0.0.0) reports the claims it gave this module; 5.4.x (5.4.5.0) never does. Detected once.</summary>
    private static readonly bool ClaimReportingHost = ReportsClaims(SdkVersion());
    private FaceSettings _face = new();
    private FaceReceiver? _faceRx;
    private FaceGate? _faceGate;
    private FaceMonitor? _faceMonitor;
    private StatusKeeper? _keeper;
    private readonly int[] _faceIdx = new int[FaceMapping.TargetsWithSlant];
    private readonly float[] _faceVal = new float[FaceMapping.TargetsWithSlant];
    private volatile bool _eyeOk, _faceOk;
    private bool _usingEye, _usingExpression, _updated;
    private readonly LostClaims _lostClaims = new();
    private long _nextKeeperTick, _initializedAt;

    /// <summary>ModuleHarness and ModuleChecks: replaces Enum.GetName(typeof(UnifiedExpressions), i) in the name check.</summary>
    internal static Func<int, string?>? ExpressionNameOverride { get; set; }

    /// <summary>ModuleChecks only: replaces the detected host kind (true = reports the claims, as VRCFT 6.0).</summary>
    internal static bool? HostReportsClaimsOverride { get; set; }

    /// <summary>ModuleChecks only: the status keeper's sender instead of a UdpClient to 127.0.0.1:status_keeper_port.</summary>
    internal static Action<byte[]>? StatusKeeperSendOverride { get; set; }

    /// <summary>The host reports UsingEye/UsingExpression (VRCFT 6.0), so they are authoritative once Status is Active.</summary>
    internal static bool HostReportsClaims => HostReportsClaimsOverride ?? ClaimReportingHost;

    /// <summary>VRCFaceTracking.SDK 6.0.0.0 and later send the claims with each status update; 5.4.5.0 does not.</summary>
    internal static bool ReportsClaims(Version? sdkVersion) => sdkVersion is { Major: >= 6 };

    private static Version? SdkVersion()
    {
        try
        {
            return typeof(ExtTrackingModule).Assembly.GetName().Version; // the SDK the host loaded, not the one built against
        }
        catch (Exception)
        {
            return null;
        }
    }

    public override (bool SupportsEye, bool SupportsExpression) Supported => (true, true);

    public override (bool eyeSuccess, bool expressionSuccess) Initialize(bool eyeAvailable, bool expressionAvailable)
    {
        ModuleInformation.Name = "Steam Frame Eye + Face (network)";
        _initializedAt = Clock();
        _settings = EyeSettings.Load(SettingsPath, Warn);
        _face = FaceSettings.Load(SettingsPath, Warn);
        var enumOk = CheckUnifiedExpressionNames(out var bad);
        var i = new ModuleClaims.Inputs(eyeAvailable, expressionAvailable, _face.EyeEnabled, _face.FaceEnabled, enumOk);
        if (!eyeAvailable)
        {
            Logger.LogWarning("SteamFrameEye: eye tracking was already claimed by another module (Steam Link module?); remove it to use the Steam Frame's eyes");
        }
        else if (!_face.EyeEnabled)
        {
            Logger.LogInformation("SteamFrameEye: eye_enabled is false; eyes not claimed");
        }
        if (!expressionAvailable && _face.FaceEnabled)
        {
            Logger.LogWarning("SteamFrameEye: expressions were already claimed by another module (VRCFT-Babble?); remove it, this module includes face tracking");
        }
        if (_face.FaceEnabled && !enumOk)
        {
            Logger.LogError("SteamFrameEye: this VRCFaceTracking's UnifiedExpressions differ from the face mapping ({Bad}); face tracking is off", bad);
        }
        if (_face.FaceEnabled && FaceSettings.BabbleModuleInstalled(ConfigDir))
        {
            Logger.LogWarning("SteamFrameEye: the VRCFT-Babble module is installed (CustomLibs\\{Id}); remove it (port 8888 and claim conflict)", FaceSettings.BabbleModuleId);
        }

        var eyeStarted = ModuleClaims.ShouldStartEye(i) && StartEye();
        var faceStarted = ModuleClaims.ShouldStartFace(i) && StartFace();
        var (eyeOk, faceOk) = ModuleClaims.Decide(i, eyeStarted, faceStarted);
        if (!eyeOk)
        {
            StopEye();
        }
        if (!faceOk)
        {
            StopFace();
        }
        Logger.LogInformation("SteamFrameEye: claims eye={Eye} expression={Expression} (eye UDP {Port}, face 127.0.0.1:{FacePort}, face_source={Source})",
            eyeOk, faceOk, _settings.Port, _face.FacePort, _face.FaceSource);
        if (!_face.FaceEnabled)
        {
            Logger.LogInformation("SteamFrameEye: face_source is \"off\"; expressions not claimed. Run headset\\frameface\\install-frameface.ps1 or set \"face_source\": \"babble_osc\" in SteamFrameEyeModule.json");
        }
        if (_face.StatusKeeper != "off" && (eyeOk || faceOk))
        {
            _keeper = new StatusKeeper(_face.StatusKeeperPort, _face.StatusKeeper, Clock, StatusKeeperSendOverride, HostReportsClaims);
            Logger.LogInformation("SteamFrameEye: status keeper \"{Mode}\" sends {Bools} to 127.0.0.1:{Port} while this module is active",
                _face.StatusKeeper, _face.StatusKeeper == "all" ? "Lip/Expression/EyeTrackingActive" : "Lip/ExpressionTrackingActive", _face.StatusKeeperPort);
        }
        (_eyeOk, _faceOk) = (eyeOk, faceOk);
        return (eyeOk, faceOk); // never claims what was not offered (G1 §6.2)
    }

    public override void Update()
    {
        // Wait briefly for new eye or face data so this loop does not spin; VRCFT calls Update() repeatedly.
        _newData.WaitOne(20);
        LogClaimChanges();
        if (_eyeOk)
        {
            UpdateEye();
        }
        if (_faceOk)
        {
            UpdateFace();
        }
        TickKeeper();
    }

    public override void Teardown()
    {
        _faceRx?.CloseSocket(); // 1. stop the face path without waiting: the pending receive ends
        _keeper?.OnTeardown();  //    one ",F" per bool that was sent ",T" (loopback, microseconds)
        TeardownEye();          // 2. the eye teardown with the calibration save (VRCFT allows about 200 ms)
        if (_faceRx is { } rx)  // 3. only now wait for the face thread, then log its counters
        {
            rx.Join(100);
            Logger.LogInformation("SteamFrameEye: {Summary}", FaceMonitor.Summary(rx.Snapshot(Clock())));
        }
    }

    /// <summary>The 0.5.3 Initialize body after its claim check: SFEY on UDP Any:port and the receive thread.</summary>
    private bool StartEye()
    {
        if (int.TryParse(Environment.GetEnvironmentVariable("SFEYE_PORT"), out var envPort) && envPort is > 0 and < 65536)
        {
            _settings.Port = envPort;
        }
        _processor = new EyeProcessor(_settings, LoadCalibration());

        TestPorts.ThrowIfForbidden(_settings.Port);
        try
        {
            _udp = new UdpClient(new IPEndPoint(IPAddress.Any, _settings.Port));
            _udp.Client.ReceiveTimeout = 500;
        }
        catch (Exception e)
        {
            Logger.LogError("SteamFrameEye: cannot bind UDP port {Port}: {Message}", _settings.Port, e.Message);
            return false;
        }

        _nextSave = DateTime.UtcNow + CalibrationSaveInterval;
        _nextCalibrationLog = DateTime.UtcNow + TimeSpan.FromSeconds(60);
        _running = true;
        _rxThread = new Thread(ReceiveLoop) { IsBackground = true, Name = "SteamFrameEyeRx" };
        _rxThread.Start();
        Logger.LogInformation(
            "SteamFrameEye: listening on UDP {Port}; on the headset, enter this PC's address in FrameEye Bridge. " +
            "Gaze {Gaze}, eyelids {Eyelids}, smoothing {Smoothing}, calibration {Calibration} ({Lids})",
            _settings.Port, _settings.CombinedGaze ? "combined" : "per eye",
            _settings.Eyelids != "linked" ? _settings.Eyelids.Replace('_', ' ')
                : _settings.WinkDetection ? "linked, wink detection on" : "linked, wink detection off",
            _settings.Smoothing ? "on" : "off", _settings.Calibration, DescribeLids(_processor));
        if (_settings.Smoothing)
        {
            Logger.LogInformation("SteamFrameEye: gaze smoothing median {Median}, minimum cutoff {Cutoff:F2} Hz, beta {Beta:F2}",
                _settings.GazeMedianFilter ? "on" : "off", _settings.GazeMinCutoff, _settings.GazeBeta);
        }
        return true;
    }

    /// <summary>Releases what a failed <see cref="StartEye"/> left behind (the eye path is not claimed).</summary>
    private void StopEye()
    {
        _running = false;
        try { _udp?.Close(); } catch { /* ignore */ }
        _rxThread?.Join(1000);
        _udp = null;
        _rxThread = null;
    }

    /// <summary>The 0.5.3 Update body after the wait.</summary>
    private void UpdateEye()
    {
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

    /// <summary>The 0.5.3 Teardown body: close the SFEY socket, join, save the calibration, log.</summary>
    private void TeardownEye()
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

    /// <summary>FaceReceiver on 127.0.0.1:face_port (exclusive), the gate and the log lines.</summary>
    private bool StartFace()
    {
        var rx = new FaceReceiver(_face.FacePort, m => Logger.LogInformation("SteamFrameEye: {Message}", m), Warn, Clock);
        rx.Received += () => _newData.Set();
        if (!rx.Start(out var error))
        {
            Logger.LogError("SteamFrameEye: {Error}", error);
            return false;
        }
        _faceRx = rx;
        _faceGate = new FaceGate(_face);
        _faceMonitor = new FaceMonitor(_face.StaleMs, m => Logger.LogInformation("SteamFrameEye: {Message}", m), Warn);
        Logger.LogInformation(
            "SteamFrameEye: listening for Baballonia OSC on 127.0.0.1:{Port} (Babble45 -> {Targets} UnifiedExpressions shapes; live at >= {Rate} Hz, stale after {Stale} ms)",
            _face.FacePort, _face.SmileSlant ? FaceMapping.TargetsWithSlant : FaceMapping.Targets, _face.MinRateHz, _face.StaleMs);
        return true;
    }

    private void StopFace()
    {
        _faceRx?.Stop();
        _faceRx = null;
        _faceGate = null;
        _faceMonitor = null;
    }

    /// <summary>Writes the §8.3 shapes (only those): nothing before the first face message, then always, fading to 0.</summary>
    private void UpdateFace()
    {
        if (_faceRx is not { } rx || _faceGate is not { } gate)
        {
            return;
        }
        var now = Clock();
        var s = rx.Snapshot(now);
        var n = gate.Compose(now, s, _face.SmileSlant, _faceIdx, _faceVal);
        for (var k = 0; k < n; k++)
        {
            UnifiedTracking.Data.Shapes[_faceIdx[k]].Weight = _faceVal[k];
        }
        _faceMonitor?.Step(now, s, gate.Live);
    }

    /// <summary>
    /// VRCFT 6.0 reports which claims it gave this module; 5.4.x never does (both stay false): logged when they change.
    /// On a host that reports them, a claim Initialize returned that the host does not report while Active, from 1 s
    /// after Initialize on (never at the first Update: 6.0 sends Active with both false first and the real claims right
    /// after), went to another module, so the host drops this module's data for it: one warning per lost claim, or one
    /// naming all of them when the module holds none (LostClaims).
    /// </summary>
    private void LogClaimChanges()
    {
        var (eye, expression) = (ModuleInformation.UsingEye, ModuleInformation.UsingExpression);
        if (eye != _usingEye || expression != _usingExpression)
        {
            (_usingEye, _usingExpression) = (eye, expression);
            Logger.LogInformation("SteamFrameEye: VRCFaceTracking uses this module for eye={Eye} expression={Expression}", eye, expression);
        }
        var firstUpdate = !_updated;
        _updated = true;
        if (firstUpdate || !HostReportsClaims || Status != ModuleState.Active || Clock() - _initializedAt < ClaimCheckMs)
        {
            return;
        }
        if (_lostClaims.Check(_eyeOk, _faceOk, eye, expression) is { } warning)
        {
            Logger.LogWarning("SteamFrameEye: {Warning}", warning);
        }
    }

    private void TickKeeper()
    {
        if (_keeper is not { } keeper)
        {
            return;
        }
        var now = Clock();
        if (now < _nextKeeperTick)
        {
            return;
        }
        _nextKeeperTick = now + KeeperTickMs;
        bool eyeFresh;
        lock (_lock)
        {
            eyeFresh = _have && (DateTime.UtcNow - _lastRx).TotalSeconds <= StaleSeconds;
        }
        keeper.Tick(_faceGate?.Live ?? false, eyeFresh, Status == ModuleState.Active,
            ModuleInformation.UsingEye, ModuleInformation.UsingExpression, _eyeOk, _faceOk);
    }

    /// <summary>Every face mapping target must be the UnifiedExpressions member of the same name in this VRCFT.</summary>
    private static bool CheckUnifiedExpressionNames(out string bad)
    {
        var nameOf = ExpressionNameOverride ?? (i => Enum.GetName(typeof(UnifiedExpressions), i));
        var mismatches = new List<string>();
        var ok = FaceMapping.CheckNames(nameOf, mismatches);
        bad = string.Join("; ", mismatches);
        return ok;
    }

    private void Warn(string message) => Logger.LogWarning("SteamFrameEye: {Message}", message);

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
            if (s.WinksOff != _winksOff)
            {
                _winksOff = s.WinksOff;
                Logger.LogInformation("SteamFrameEye: wink detection switched {State} on the headset", _winksOff ? "off" : "on");
            }
            if (s.SymmetryOff != _symmetryOff)
            {
                _symmetryOff = s.SymmetryOff;
                Logger.LogInformation("SteamFrameEye: eyelid symmetry switched {State} on the headset", _symmetryOff ? "off" : "on");
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
                Logger.LogInformation("SteamFrameEye: {Lids}; gaze held {Held:F1} %, low-confidence samples (closed eyes or poor fit) {Bad:F1} %, stuck lids rescued {Rescued:F1} %",
                    DescribeLids(processor), 100.0 * processor.GazeHeldSamples / Math.Max(1, processor.Samples),
                    100.0 * processor.UnreliableSamples / Math.Max(1, processor.Samples),
                    100.0 * processor.RescueSamples / Math.Max(1, processor.Samples));
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
    /// Packet (little-endian, 96 bytes): "SFEY", u16 version, u16 flags (bit0 = valid, bit1 = wink detection off,
    /// bit2 = eyelid symmetry off), u32 seq,
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
        s.WinksOff = (flags & 2) != 0;
        s.SymmetryOff = (flags & 4) != 0;
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
