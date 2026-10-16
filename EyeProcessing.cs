using System.Text.Json;
using System.Text.Json.Nodes;

namespace SteamFrameEyeModule;

/// <summary>One eye-server sample as carried by an SFEY packet.</summary>
public struct EyeSample
{
    public double SampleTime;      // headset clock, seconds
    public bool Valid;
    public float LX, LY, LZ, RX, RY, RZ;   // fused per-eye gaze directions (x right, y up, -z forward)
    public float FX, FY, FZ;               // fixation point
    public float OpenL, OpenR;             // raw eyelid openness from the eye server, 0..1
    public float CovL, CovR;               // largest gaze covariance component per eye (tracking quality)
}

/// <summary>Processed values in VRCFaceTracking units: gaze in radians (the scale the Steam Link module
/// writes for a Quest Pro), openness 0 = closed .. 1 = open.</summary>
public struct EyeOutput
{
    public float LeftX, LeftY, RightX, RightY;
    public float OpenL, OpenR;
}

/// <summary>Settings from %APPDATA%\VRCFaceTracking\SteamFrameEyeModule.json (all optional).</summary>
public sealed class EyeSettings
{
    public int Port = 9021;
    public bool CombinedGaze = true;       // "gaze": "combined" (both eyes the same, like Quest Pro via Steam Link) or "per_eye"
    public float GazeScale = 1f;           // multiplier on the gaze angle in radians
    public float GazeOffsetXDeg, GazeOffsetYDeg;
    public bool Smoothing = true;
    public bool GazeMedianFilter = true;  // reject isolated gaze spikes before the adaptive filter
    public float GazeMinCutoff = 0.8f, GazeBeta = 5.0f, GazeDCutoff = 1.0f;
    public float LidMinCutoff = 4.0f, LidBeta = 1.0f, LidDCutoff = 1.0f;
    public string Calibration = "auto";    // "auto" (learn per eye), "fixed" (use the levels below), "off" (raw values)
    public float LeftClosed = 0.0f, LeftOpen = 0.9f, RightClosed = 0.0f, RightOpen = 0.9f;
    public bool LidGazeCompensation = true; // learn the open level per vertical gaze angle (lids drop when looking down)
    public float ClosedSnap = 0.15f, OpenSnap = 0.9f;
    public bool BlinkSync = true;          // one eye fully closed and the other mostly closed: close both
    public float BlinkSyncBelow = 0.35f;
    // "per_eye" (winks), "linked" (both eyes show the more open one: a lid the eye server misreads as closed on
    // one side no longer shuts that eye, blinks still close both, winks are lost), "left"/"right" (both follow one eye).
    public string Eyelids = "per_eye";
    public int BlinkHoldMs = 80;
    public float GazeHoldBelow = 0.6f;     // an eye's gaze is only used while its openness is at least this
    public float GazeSingleEyeAbove = 0.8f; // ...and at least this when it is the only eye steering (a wink)
    // An eye's gaze covariance above this marks its gaze as not tracked (good tracking ~0.0004; a closed eye,
    // the bottom of a blink or a headset that does not sit properly reach 0.02-0.05). 0 disables the check.
    public float QualityLimit = 0.03f;

    public static EyeSettings Load(string path, Action<string> warn)
    {
        var s = new EyeSettings();
        if (!File.Exists(path))
        {
            return s;
        }
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            var root = doc.RootElement;
            int Int(string name, int current) =>
                root.TryGetProperty(name, out var p) && p.TryGetInt32(out var v) ? v : current;
            float Float(string name, float current) =>
                root.TryGetProperty(name, out var p) && p.TryGetDouble(out var v) && double.IsFinite(v) ? (float)v : current;
            bool Bool(string name, bool current) =>
                root.TryGetProperty(name, out var p) && (p.ValueKind == JsonValueKind.True || p.ValueKind == JsonValueKind.False)
                    ? p.GetBoolean() : current;
            string Str(string name, string current) =>
                root.TryGetProperty(name, out var p) && p.ValueKind == JsonValueKind.String ? p.GetString() ?? current : current;

            var port = Int("port", s.Port);
            s.Port = port is > 0 and < 65536 ? port : s.Port;
            s.CombinedGaze = !Str("gaze", "combined").Equals("per_eye", StringComparison.OrdinalIgnoreCase);
            s.GazeScale = Float("gaze_scale", s.GazeScale);
            s.GazeOffsetXDeg = Float("gaze_offset_x_deg", s.GazeOffsetXDeg);
            s.GazeOffsetYDeg = Float("gaze_offset_y_deg", s.GazeOffsetYDeg);
            s.Smoothing = Bool("smoothing", s.Smoothing);
            s.GazeMedianFilter = Bool("gaze_median_filter", s.GazeMedianFilter);
            s.GazeMinCutoff = MathF.Max(0.01f, Float("gaze_min_cutoff", s.GazeMinCutoff));
            s.GazeBeta = MathF.Max(0f, Float("gaze_beta", s.GazeBeta));
            s.LidMinCutoff = MathF.Max(0.01f, Float("lid_min_cutoff", s.LidMinCutoff));
            s.LidBeta = MathF.Max(0f, Float("lid_beta", s.LidBeta));
            var cal = Str("calibration", s.Calibration).ToLowerInvariant();
            s.Calibration = cal is "auto" or "fixed" or "off" ? cal : "auto";
            s.LeftClosed = Float("left_closed", s.LeftClosed);
            s.LeftOpen = Float("left_open", s.LeftOpen);
            s.RightClosed = Float("right_closed", s.RightClosed);
            s.RightOpen = Float("right_open", s.RightOpen);
            s.LidGazeCompensation = Bool("lid_gaze_compensation", s.LidGazeCompensation);
            s.ClosedSnap = Math.Clamp(Float("closed_snap", s.ClosedSnap), 0f, 0.45f);
            s.OpenSnap = Math.Clamp(Float("open_snap", s.OpenSnap), 0.55f, 1f);
            s.BlinkSync = Bool("blink_sync", s.BlinkSync);
            s.BlinkSyncBelow = Math.Clamp(Float("blink_sync_below", s.BlinkSyncBelow), 0f, 0.9f);
            var lids = Str("eyelids", s.Eyelids).ToLowerInvariant();
            s.Eyelids = lids is "per_eye" or "linked" or "left" or "right" ? lids : "per_eye";
            s.BlinkHoldMs = Math.Clamp(Int("blink_hold_ms", s.BlinkHoldMs), 0, 500);
            s.GazeHoldBelow = Math.Clamp(Float("gaze_hold_below", s.GazeHoldBelow), 0f, 1f);
            s.GazeSingleEyeAbove = Math.Clamp(Float("gaze_single_eye_above", s.GazeSingleEyeAbove), 0f, 1f);
            s.QualityLimit = MathF.Max(0f, Float("quality_limit", s.QualityLimit));
        }
        catch (Exception e)
        {
            warn($"could not read {Path.GetFileName(path)}: {e.Message}; using defaults");
        }
        return s;
    }
}

/// <summary>One Euro filter (Casiez et al. 2012) with the real sample interval.</summary>
public sealed class OneEuro
{
    private readonly float _minCutoff, _beta, _dCutoff;
    private bool _init;
    private float _x, _dx;

    public OneEuro(float minCutoff, float beta, float dCutoff)
    {
        _minCutoff = minCutoff;
        _beta = beta;
        _dCutoff = dCutoff;
    }

    public void Reset() => _init = false;

    public float Filter(float x, float dt)
    {
        if (!_init)
        {
            _init = true;
            _x = x;
            _dx = 0f;
            return x;
        }
        var dx = (x - _x) / dt;
        _dx += Alpha(_dCutoff, dt) * (dx - _dx);
        var cutoff = _minCutoff + _beta * MathF.Abs(_dx);
        _x += Alpha(cutoff, dt) * (x - _x);
        return _x;
    }

    private static float Alpha(float cutoff, float dt)
    {
        var tau = 1f / (2f * MathF.PI * cutoff);
        return 1f / (1f + tau / dt);
    }
}

/// <summary>Three-sample gaze median followed by adaptive smoothing. A sustained movement passes
/// the median after one sample; an isolated spike never reaches the adaptive filter.</summary>
public sealed class GazeSmoother
{
    private readonly OneEuro _x, _y;
    private readonly bool _median;
    private bool _haveHistory;
    private (float X, float Y) _previous, _older;

    public GazeSmoother(EyeSettings s)
    {
        _median = s.GazeMedianFilter;
        _x = new OneEuro(s.GazeMinCutoff, s.GazeBeta, s.GazeDCutoff);
        _y = new OneEuro(s.GazeMinCutoff, s.GazeBeta, s.GazeDCutoff);
    }

    // On a blink or a change of contributing eye, forget the old median window while keeping
    // the filtered output continuous. A clock reset/reconnect also resets the adaptive filters.
    public void ClearHistory() => _haveHistory = false;

    public void Reset()
    {
        ClearHistory();
        _x.Reset();
        _y.Reset();
    }

    public (float X, float Y) Filter(float x, float y, float dt)
    {
        var input = (X: x, Y: y);
        if (_median && _haveHistory)
        {
            x = Median3(x, _previous.X, _older.X);
            y = Median3(y, _previous.Y, _older.Y);
        }
        if (!_haveHistory) { _previous = input; }
        _older = _previous;
        _previous = input;
        _haveHistory = true;
        return (_x.Filter(x, dt), _y.Filter(y, dt));
    }

    private static float Median3(float a, float b, float c) =>
        MathF.Max(MathF.Min(a, b), MathF.Min(MathF.Max(a, b), c));
}

/// <summary>A histogram that forgets old samples with a fixed half-life, for running percentiles.</summary>
public sealed class DecayingHistogram
{
    private const int Bins = 101;
    private readonly double[] _h = new double[Bins];
    private readonly double _halfLife;

    public double Mass { get; private set; }

    public DecayingHistogram(double halfLifeSeconds) => _halfLife = halfLifeSeconds;

    public void Add(float value, float dt)
    {
        var decay = Math.Exp(-dt * Math.Log(2) / _halfLife);
        for (var i = 0; i < Bins; i++)
        {
            _h[i] *= decay;
        }
        Mass = Mass * decay + 1;
        _h[Math.Clamp((int)MathF.Round(value * (Bins - 1)), 0, Bins - 1)] += 1;
    }

    public float Percentile(double p)
    {
        var target = p * Mass;
        double acc = 0;
        for (var i = 0; i < Bins; i++)
        {
            acc += _h[i];
            if (acc >= target)
            {
                return i / (float)(Bins - 1);
            }
        }
        return 1f;
    }
}

/// <summary>Learns where one eye's raw openness sits when the eye is open and when it is closed.
/// The Frame's raw range differs per eye and per wearer (seen: left eye open ~0.85-0.95 and closing to ~0.2,
/// right eye saturating at 1.0 and bottoming out around 0.35-0.4 in blinks), and the open level drops by up
/// to ~0.15 when looking down, so the open level is also learned per vertical-gaze band.</summary>
public sealed class LidCalibration
{
    public static readonly float[] PitchEdges = { -20f, -12f, -6f, 0f, 6f, 12f };           // degrees, 7 bands
    private static readonly float[] PitchCenters = { -26f, -16f, -9f, -3f, 3f, 9f, 18f };
    private const double OpenPercentile = 0.85;
    private const int MinimaKept = 30;
    // A blink must take the lid at least 40 % below the open level. Deriving this from the learned closed
    // level instead feeds back on itself (shallower dips count, the closed level rises, and so on).
    private const float DipBelowFractionOfOpen = 0.6f;
    // Only a dip whose bottom reaches 65 % below the open level teaches the closed level. Shallower dips are
    // lids hovering around the threshold: looking down, squints, and one eye sagging to ~0.5 with the
    // SteamOS 0.4.3 eye server. Counted, they lifted the closed level to ~0.5 and relaxed eyes showed half shut.
    private const float BlinkBottomFractionOfOpen = 0.35f;

    private readonly DecayingHistogram _all = new(180);
    private readonly DecayingHistogram[] _bands = Enumerable.Range(0, 7).Select(_ => new DecayingHistogram(300)).ToArray();
    private readonly float?[] _bandPrior = new float?[7];
    private readonly Queue<float> _minima = new();
    private bool _inDip;
    private float _dipMin;
    private int _dipSamples;

    public float Closed { get; private set; }
    public float Open { get; private set; }
    public int BlinksSeen { get; private set; }

    public LidCalibration(float closed, float open, float?[]? openByPitch = null)
    {
        Closed = closed;
        Open = open;
        if (openByPitch is { Length: 7 })
        {
            Array.Copy(openByPitch, _bandPrior, 7);
        }
        // Seed the blink minima so a saved level only moves as new blinks come in.
        for (var i = 0; i < 3; i++)
        {
            _minima.Enqueue(closed);
        }
        Sanitize();
    }

    /// <param name="trusted">recent tracking is reliable: the open level may learn and a new blink dip may start.
    /// A dip already in progress continues either way (its bottom has a hidden pupil and a high covariance).</param>
    public void Add(float value, float dt, float pitchDeg, bool trusted = true)
    {
        var dipBelow = DipBelowFractionOfOpen * Open;
        if (trusted)
        {
            _all.Add(value, dt);
            if (value >= dipBelow)
            {
                _bands[Band(pitchDeg)].Add(value, dt);
            }
        }

        // Closed level: median of the minima of recent blinks (dips below dipBelow lasting >= 3 samples).
        if (value < dipBelow && (trusted || _inDip))
        {
            if (!_inDip)
            {
                _inDip = true;
                _dipMin = value;
                _dipSamples = 0;
            }
            _dipMin = MathF.Min(_dipMin, value);
            _dipSamples++;
        }
        else if (_inDip && value >= dipBelow)
        {
            _inDip = false;
            if (_dipSamples >= 3 && _dipMin <= BlinkBottomFractionOfOpen * Open)
            {
                _minima.Enqueue(_dipMin);
                while (_minima.Count > MinimaKept)
                {
                    _minima.Dequeue();
                }
                BlinksSeen++;
            }
        }

        if (_all.Mass > 900)   // ~10 s of data before the open level follows the histogram
        {
            Open = _all.Percentile(OpenPercentile);
        }
        var sorted = _minima.OrderBy(v => v).ToArray();
        Closed = sorted[sorted.Length / 2];
        Sanitize();
    }

    /// <summary>Open level for a band: its own percentile once it has ~3 s of open-eye data, else the saved
    /// value, else the overall open level.</summary>
    public float BandOpen(int band)
    {
        var h = _bands[band];
        var v = h.Mass > 270 ? h.Percentile(OpenPercentile) : _bandPrior[band] ?? Open;
        return Math.Clamp(v, Closed + 0.25f, 1f);
    }

    public float OpenAt(float pitchDeg)
    {
        if (pitchDeg <= PitchCenters[0])
        {
            return BandOpen(0);
        }
        for (var i = 0; i < PitchCenters.Length - 1; i++)
        {
            if (pitchDeg <= PitchCenters[i + 1])
            {
                var f = (pitchDeg - PitchCenters[i]) / (PitchCenters[i + 1] - PitchCenters[i]);
                return BandOpen(i) + f * (BandOpen(i + 1) - BandOpen(i));
            }
        }
        return BandOpen(PitchCenters.Length - 1);
    }

    /// <summary>Unreliable data: forget a blink dip in progress so junk minima are not learned.</summary>
    public void Interrupt() => _inDip = false;

    public float Normalize(float value, float pitchDeg, bool gazeCompensation)
    {
        var open = gazeCompensation ? OpenAt(pitchDeg) : Open;
        return (value - Closed) / MathF.Max(0.2f, open - Closed);
    }

    public JsonObject ToJson() => new()
    {
        ["closed"] = Math.Round(Closed, 4),
        ["open"] = Math.Round(Open, 4),
        ["open_by_pitch"] = new JsonArray(Enumerable.Range(0, 7).Select(b => (JsonNode)Math.Round(BandOpen(b), 4)).ToArray()),
        ["blinks"] = BlinksSeen,
    };

    public static LidCalibration? FromJson(JsonNode? node)
    {
        if (node is not JsonObject o || o["closed"] is null || o["open"] is null)
        {
            return null;
        }
        var closed = o["closed"]!.GetValue<double>();
        var open = o["open"]!.GetValue<double>();
        var blinks = o["blinks"]?.GetValue<int>() ?? 0;
        if (!(open >= 0.6 && open <= 1.0 && closed >= 0 && closed <= open - 0.25 && blinks >= 5))
        {
            return null;   // implausible (learned from bad data): start from the defaults instead
        }
        var bands = new float?[7];
        if (o["open_by_pitch"] is JsonArray a && a.Count == 7)
        {
            for (var i = 0; i < 7; i++)
            {
                bands[i] = a[i]?.GetValue<double>() is double d && double.IsFinite(d) ? (float)d : null;
            }
        }
        return new LidCalibration((float)o["closed"]!.GetValue<double>(), (float)o["open"]!.GetValue<double>(), bands);
    }

    private static int Band(float pitchDeg)
    {
        var i = 0;
        while (i < PitchEdges.Length && pitchDeg >= PitchEdges[i])
        {
            i++;
        }
        return i;
    }

    private void Sanitize()
    {
        Open = Math.Clamp(Open, 0.6f, 1f);
        Closed = Math.Clamp(Closed, 0f, 0.6f);
        if (Open - Closed < 0.25f)
        {
            Closed = MathF.Max(0f, Open - 0.25f);
        }
    }
}

/// <summary>Per-eye eyelid pipeline: dropout removal, calibration, smoothing, snap zones, blink hold.</summary>
public sealed class LidPipeline
{
    private readonly float[] _last = new float[3];
    private int _count;
    private readonly OneEuro _filter;
    private double _holdUntil = double.NegativeInfinity;

    public LidCalibration Calibration { get; }
    public float Normalized { get; private set; } = 1f;   // calibrated, before smoothing and snapping

    public LidPipeline(EyeSettings s, LidCalibration calibration)
    {
        Calibration = calibration;
        _filter = new OneEuro(s.LidMinCutoff, s.LidBeta, s.LidDCutoff);
    }

    public void Reset()
    {
        _count = 0;
        _holdUntil = double.NegativeInfinity;
        _filter.Reset();
    }

    public void HoldClosed(double t, EyeSettings s) => _holdUntil = Math.Max(_holdUntil, t + s.BlinkHoldMs / 1000.0);

    public float Process(float raw, double t, float dt, float pitchDeg, EyeSettings s, bool trusted = true, bool sustainedBad = false)
    {
        raw = Math.Clamp(raw, 0f, 1f);
        // Median of the last three samples: the Frame drops single samples to ~0 now and then.
        _last[_count % 3] = raw;
        _count++;
        var median = _count < 3 ? raw : Median3(_last[0], _last[1], _last[2]);

        if (s.Calibration == "auto")
        {
            if (sustainedBad)
            {
                Calibration.Interrupt();
            }
            else
            {
                Calibration.Add(median, dt, pitchDeg, trusted);
            }
        }
        Normalized = s.Calibration == "off" ? median : Calibration.Normalize(median, pitchDeg, s.LidGazeCompensation);

        var value = s.Smoothing ? _filter.Filter(Normalized, dt) : Normalized;
        var snapped = Math.Clamp((value - s.ClosedSnap) / (s.OpenSnap - s.ClosedSnap), 0f, 1f);

        // Keep a closed eye closed for a moment so VRChat's network sync shows short blinks to others.
        if (snapped <= 0f)
        {
            HoldClosed(t, s);
        }
        return t < _holdUntil ? 0f : snapped;
    }

    private static float Median3(float a, float b, float c) =>
        MathF.Max(MathF.Min(a, b), MathF.Min(MathF.Max(a, b), c));
}

/// <summary>Recognises a deliberate wink for linked eyelids: one lid falls from open to closed within 200 ms and
/// stays closed for 300 ms while the other eye stays clearly open. The eye server's one-sided misreads either drift
/// down over a second or more, hover around 0.15-0.5, last only a few samples, or come with a dip of the other eye.
/// It also reports many blinks staggered: one eye at 0 while the other reads 1.0 for up to ~250 ms before it closes
/// too, which the 300 ms confirmation leaves out.</summary>
public sealed class WinkDetector
{
    private const float OpenAbove = 0.7f, ClosedBelow = 0.15f, StayClosedBelow = 0.3f, EndAbove = 0.5f;
    private const float OtherOpenAbove = 0.8f, OtherKeepAbove = 0.6f;
    private const double MaxFallSeconds = 0.2, ConfirmSeconds = 0.3;
    private double _lastOpen = double.NegativeInfinity;
    private double _candidateSince = double.NaN;

    public bool Active { get; private set; }

    public void Reset()
    {
        _lastOpen = double.NegativeInfinity;
        _candidateSince = double.NaN;
        Active = false;
    }

    /// <param name="self">this eye's calibrated openness (before smoothing), <paramref name="other"/> the other eye's</param>
    public bool Update(double t, float self, float other)
    {
        if (Active)
        {
            Active = self < EndAbove && other >= OtherKeepAbove;
        }
        else if (!double.IsNaN(_candidateSince))
        {
            if (self > StayClosedBelow || other < OtherOpenAbove)
            {
                _candidateSince = double.NaN;
            }
            else if (t - _candidateSince >= ConfirmSeconds)
            {
                Active = true;
                _candidateSince = double.NaN;
            }
        }
        else if (self <= ClosedBelow && other >= OtherOpenAbove && t - _lastOpen <= MaxFallSeconds)
        {
            _candidateSince = t;
        }
        if (self >= OpenAbove)
        {
            _lastOpen = t;
        }
        return Active;
    }
}

/// <summary>Turns SFEY samples into VRCFaceTracking eye values.</summary>
public sealed class EyeProcessor
{
    private readonly EyeSettings _s;
    private readonly GazeSmoother _leftGaze, _rightGaze;
    private readonly WinkDetector _winkL = new(), _winkR = new();
    private int _gazeSource;
    private double _lastTime = double.NaN;
    private EyeOutput _out = new() { OpenL = 1f, OpenR = 1f };
    private float _pitchDeg;

    public LidPipeline Left { get; }
    public LidPipeline Right { get; }
    public long Samples { get; private set; }
    public long GazeHeldSamples { get; private set; }
    /// <summary>Samples where at least one eye's gaze was not tracked (closed eye, blink bottom or poor fit).</summary>
    public long UnreliableSamples { get; private set; }
    public long ReliableSamples { get; private set; }
    private double _unreliableSince = double.NaN;
    // Share of reliable samples over the last ~2 s. The bottom of a deep blink also raises the covariance
    // (the pupil is hidden), so learning follows this ratio instead of single samples.
    private float _reliability = 0.5f;
    private const float ReliabilitySeconds = 2f, LearnAboveReliability = 0.8f;
    private const double DropDipAfterUnreliableMs = 400;

    /// <param name="saved">calibration saved by an earlier session (ToJson), used as the starting point in "auto"</param>
    public EyeProcessor(EyeSettings s, JsonNode? saved = null)
    {
        _s = s;
        LidCalibration Start(string eye, float closed, float open) =>
            s.Calibration == "auto" && LidCalibration.FromJson(saved?[eye]) is { } c ? c : new LidCalibration(closed, open);
        Left = new LidPipeline(s, Start("left", s.LeftClosed, s.LeftOpen));
        Right = new LidPipeline(s, Start("right", s.RightClosed, s.RightOpen));
        _leftGaze = new GazeSmoother(s);
        _rightGaze = new GazeSmoother(s);
    }

    public EyeOutput Output => _out;

    public JsonObject CalibrationJson() => new()
    {
        ["version"] = 1,
        ["saved"] = DateTime.UtcNow.ToString("u"),
        ["pitch_band_edges_deg"] = new JsonArray(LidCalibration.PitchEdges.Select(e => (JsonNode)e).ToArray()),
        ["left"] = Left.Calibration.ToJson(),
        ["right"] = Right.Calibration.ToJson(),
    };

    public EyeOutput Process(in EyeSample x)
    {
        if (!x.Valid)
        {
            return _out;
        }
        var dt = double.IsNaN(_lastTime) ? 1 / 90.0 : x.SampleTime - _lastTime;
        _lastTime = x.SampleTime;
        if (dt <= 0 || dt > 0.25)
        {
            // Headset clock reset or a gap (standby, reconnect): start the filters afresh.
            Left.Reset();
            Right.Reset();
            _winkL.Reset(); _winkR.Reset();
            _leftGaze.Reset(); _rightGaze.Reset();
            _gazeSource = 0;
            dt = 1 / 90.0;
        }
        var fdt = (float)Math.Clamp(dt, 0.004, 0.05);
        Samples++;

        // A closed eye hides its pupil and reports a high covariance, so low confidence is expected during blinks,
        // winks and closed eyes: it keeps that eye's gaze out and pauses calibration learning, nothing more.
        var trackedL = Tracked(x.CovL);
        var trackedR = Tracked(x.CovR);
        var reliable = trackedL && trackedR;
        if (reliable)
        {
            ReliableSamples++;
            _unreliableSince = double.NaN;
        }
        else
        {
            UnreliableSamples++;
            if (double.IsNaN(_unreliableSince))
            {
                _unreliableSince = x.SampleTime;
            }
        }

        var (glx, gly) = Angles(x.LX, x.LY, x.LZ);
        var (grx, gry) = Angles(x.RX, x.RY, x.RZ);
        var (cx, cy) = Angles(x.LX + x.RX, x.LY + x.RY, x.LZ + x.RZ);
        if (trackedL || trackedR)
        {
            _pitchDeg = (trackedL && trackedR ? cy : trackedL ? gly : gry) * 180f / MathF.PI;
        }
        _reliability += fdt / (ReliabilitySeconds + fdt) * ((reliable ? 1f : 0f) - _reliability);
        var trusted = _reliability > LearnAboveReliability;
        var sustainedBad = !reliable && (x.SampleTime - _unreliableSince) * 1000 >= DropDipAfterUnreliableMs;
        _out.OpenL = Left.Process(x.OpenL, x.SampleTime, fdt, _pitchDeg, _s, trusted, sustainedBad);
        _out.OpenR = Right.Process(x.OpenR, x.SampleTime, fdt, _pitchDeg, _s, trusted, sustainedBad);
        if (_s.BlinkSync)
        {
            // Blinks are bilateral; the Frame sometimes tracks one eye's closure less deep than the other's.
            if (_out.OpenL <= 0f && Right.Normalized < _s.BlinkSyncBelow)
            {
                Right.HoldClosed(x.SampleTime, _s);
                _out.OpenR = 0f;
            }
            if (_out.OpenR <= 0f && Left.Normalized < _s.BlinkSyncBelow)
            {
                Left.HoldClosed(x.SampleTime, _s);
                _out.OpenL = 0f;
            }
        }
        var normL = Left.Normalized;
        var normR = Right.Normalized;
        switch (_s.Eyelids)
        {
            case "linked":
                // The Frame's eye server can read one lid as closed for seconds while that pupil is still tracked;
                // real blinks close both lids, so the more open eye is the better estimate for both. A recognised
                // wink keeps each eye on its own value.
                var winkL = _winkL.Update(x.SampleTime, normL, normR);
                var winkR = _winkR.Update(x.SampleTime, normR, normL);
                if (!winkL && !winkR)
                {
                    _out.OpenL = _out.OpenR = MathF.Max(_out.OpenL, _out.OpenR);
                    normL = normR = MathF.Max(normL, normR);
                }
                break;
            case "left":
                _out.OpenR = _out.OpenL;
                normR = normL;
                break;
            case "right":
                _out.OpenL = _out.OpenR;
                normL = normR;
                break;
        }

        // Gaze only from eyes that are open and tracked: during a blink the eye server's estimate jumps by
        // several degrees per sample, and a closed eye's gaze is meaningless. One eye closed (a wink): the open
        // eye steers both. Both closed: hold.
        var useL = trackedL && normL >= _s.GazeHoldBelow;
        var useR = trackedR && normR >= _s.GazeHoldBelow;
        if (useL != useR)
        {
            // Only one eye qualifies: trust it only if clearly open (the open eye of a wink), not a lid that is
            // half-way through a blink while the other eye has already lost tracking.
            useL = useL && normL >= _s.GazeSingleEyeAbove;
            useR = useR && normR >= _s.GazeSingleEyeAbove;
        }
        var source = (useL ? 1 : 0) | (useR ? 2 : 0);
        if (source != _gazeSource)
        {
            _leftGaze.ClearHistory();
            _rightGaze.ClearHistory();
            _gazeSource = source;
        }
        if (!useL && !useR)
        {
            GazeHeldSamples++;
            return _out;
        }

        float lx, ly, rx, ry;
        if (useL && useR)
        {
            if (_s.CombinedGaze)
            {
                (lx, ly) = (cx, cy);
                (rx, ry) = (cx, cy);
            }
            else
            {
                (lx, ly) = (glx, gly);
                (rx, ry) = (grx, gry);
            }
        }
        else
        {
            (lx, ly) = useL ? (glx, gly) : (grx, gry);
            (rx, ry) = (lx, ly);
        }
        var ox = _s.GazeOffsetXDeg * MathF.PI / 180f;
        var oy = _s.GazeOffsetYDeg * MathF.PI / 180f;
        lx += ox; rx += ox; ly += oy; ry += oy;
        if (_s.Smoothing)
        {
            (lx, ly) = _leftGaze.Filter(lx, ly, fdt);
            if (_s.CombinedGaze)
            {
                (rx, ry) = (lx, ly);
            }
            else
            {
                (rx, ry) = _rightGaze.Filter(rx, ry, fdt);
            }
        }
        _out.LeftX = lx * _s.GazeScale;
        _out.LeftY = ly * _s.GazeScale;
        _out.RightX = rx * _s.GazeScale;
        _out.RightY = ry * _s.GazeScale;
        return _out;
    }

    private bool Tracked(float cov) => _s.QualityLimit <= 0f || (float.IsFinite(cov) && cov < _s.QualityLimit);

    /// <summary>Yaw and pitch in radians, atan2(x, -z) and atan2(y, -z), the Steam Link module's mapping.</summary>
    private static (float, float) Angles(float x, float y, float z)
    {
        var ax = MathF.Atan2(x, -z);
        var ay = MathF.Atan2(y, -z);
        return (float.IsFinite(ax) ? ax : 0f, float.IsFinite(ay) ? ay : 0f);
    }
}
