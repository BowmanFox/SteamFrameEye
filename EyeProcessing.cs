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
    public float GazeMinCutoff = 1.0f, GazeBeta = 8.0f, GazeDCutoff = 1.0f;
    public float LidMinCutoff = 4.0f, LidBeta = 1.0f, LidDCutoff = 1.0f;
    public string Calibration = "auto";    // "auto" (learn per eye), "fixed" (use the levels below), "off" (raw values)
    public float LeftClosed = 0.0f, LeftOpen = 0.9f, RightClosed = 0.0f, RightOpen = 0.9f;
    public bool LidGazeCompensation = true; // learn the open level per vertical gaze angle (lids drop when looking down)
    public float ClosedSnap = 0.15f, OpenSnap = 0.9f;
    public bool BlinkSync = true;          // one eye fully closed and the other mostly closed: close both
    public float BlinkSyncBelow = 0.35f;
    public int BlinkHoldMs = 80;
    public float GazeHoldBelow = 0.4f;     // freeze gaze while the mean eyelid openness is below this (blinks)
    // Gaze covariance above this marks a sample as unreliable (good tracking ~0.0004, blinks up to ~0.02,
    // headset not properly on the face ~0.05). 0 disables the check.
    public float QualityLimit = 0.03f;
    public int NeutralAfterMs = 1000;      // unreliable this long: show neutral eyes (open, looking ahead)

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
            s.BlinkHoldMs = Math.Clamp(Int("blink_hold_ms", s.BlinkHoldMs), 0, 500);
            s.GazeHoldBelow = Math.Clamp(Float("gaze_hold_below", s.GazeHoldBelow), 0f, 1f);
            s.QualityLimit = MathF.Max(0f, Float("quality_limit", s.QualityLimit));
            s.NeutralAfterMs = Math.Clamp(Int("neutral_after_ms", s.NeutralAfterMs), 100, 10000);
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
            if (_dipSamples >= 3)
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

/// <summary>Turns SFEY samples into VRCFaceTracking eye values.</summary>
public sealed class EyeProcessor
{
    private readonly EyeSettings _s;
    private readonly OneEuro _lx, _ly, _rx, _ry;
    private double _lastTime = double.NaN;
    private EyeOutput _out = new() { OpenL = 1f, OpenR = 1f };
    private bool _haveGaze;

    public LidPipeline Left { get; }
    public LidPipeline Right { get; }
    public long Samples { get; private set; }
    public long GazeHeldSamples { get; private set; }
    public long UnreliableSamples { get; private set; }
    public long ReliableSamples { get; private set; }
    public bool Neutral { get; private set; }
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
        _lx = new OneEuro(s.GazeMinCutoff, s.GazeBeta, s.GazeDCutoff);
        _ly = new OneEuro(s.GazeMinCutoff, s.GazeBeta, s.GazeDCutoff);
        _rx = new OneEuro(s.GazeMinCutoff, s.GazeBeta, s.GazeDCutoff);
        _ry = new OneEuro(s.GazeMinCutoff, s.GazeBeta, s.GazeDCutoff);
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
            _lx.Reset(); _ly.Reset(); _rx.Reset(); _ry.Reset();
            dt = 1 / 90.0;
        }
        var fdt = (float)Math.Clamp(dt, 0.004, 0.05);
        Samples++;

        var cov = MathF.Max(x.CovL, x.CovR);
        var reliable = _s.QualityLimit <= 0f || (float.IsFinite(cov) && cov < _s.QualityLimit);
        if (reliable)
        {
            ReliableSamples++;
            _unreliableSince = double.NaN;
            Neutral = false;
        }
        else
        {
            UnreliableSamples++;
            if (double.IsNaN(_unreliableSince))
            {
                _unreliableSince = x.SampleTime;
            }
            if ((x.SampleTime - _unreliableSince) * 1000 >= _s.NeutralAfterMs)
            {
                // Tracking has been unusable for a while (headset not properly on the face): neutral eyes.
                Neutral = true;
                Left.Calibration.Interrupt();
                Right.Calibration.Interrupt();
                Left.Reset(); Right.Reset();
                _lx.Reset(); _ly.Reset(); _rx.Reset(); _ry.Reset();
                _out = new EyeOutput { OpenL = 1f, OpenR = 1f };
                return _out;
            }
        }

        var (cx, cy) = Angles(x.LX + x.RX, x.LY + x.RY, x.LZ + x.RZ);
        var pitchDeg = cy * 180f / MathF.PI;
        _reliability += fdt / (ReliabilitySeconds + fdt) * ((reliable ? 1f : 0f) - _reliability);
        var trusted = _reliability > LearnAboveReliability;
        var sustainedBad = !reliable && (x.SampleTime - _unreliableSince) * 1000 >= DropDipAfterUnreliableMs;
        _out.OpenL = Left.Process(x.OpenL, x.SampleTime, fdt, pitchDeg, _s, trusted, sustainedBad);
        _out.OpenR = Right.Process(x.OpenR, x.SampleTime, fdt, pitchDeg, _s, trusted, sustainedBad);
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

        // During blinks the eye server's gaze estimate jumps by several degrees per sample; hold it.
        var lids = (Left.Normalized + Right.Normalized) / 2f;
        if ((lids < _s.GazeHoldBelow || !reliable) && _haveGaze)
        {
            GazeHeldSamples++;
            return _out;
        }

        float lx, ly, rx, ry;
        if (_s.CombinedGaze)
        {
            (lx, ly) = (cx, cy);
            (rx, ry) = (cx, cy);
        }
        else
        {
            (lx, ly) = Angles(x.LX, x.LY, x.LZ);
            (rx, ry) = Angles(x.RX, x.RY, x.RZ);
        }
        var ox = _s.GazeOffsetXDeg * MathF.PI / 180f;
        var oy = _s.GazeOffsetYDeg * MathF.PI / 180f;
        lx += ox; rx += ox; ly += oy; ry += oy;
        if (_s.Smoothing)
        {
            lx = _lx.Filter(lx, fdt);
            ly = _ly.Filter(ly, fdt);
            if (_s.CombinedGaze)
            {
                (rx, ry) = (lx, ly);
            }
            else
            {
                rx = _rx.Filter(rx, fdt);
                ry = _ry.Filter(ry, fdt);
            }
        }
        _out.LeftX = lx * _s.GazeScale;
        _out.LeftY = ly * _s.GazeScale;
        _out.RightX = rx * _s.GazeScale;
        _out.RightY = ry * _s.GazeScale;
        _haveGaze = true;
        return _out;
    }

    /// <summary>Yaw and pitch in radians, atan2(x, -z) and atan2(y, -z), the Steam Link module's mapping.</summary>
    private static (float, float) Angles(float x, float y, float z)
    {
        var ax = MathF.Atan2(x, -z);
        var ay = MathF.Atan2(y, -z);
        return (float.IsFinite(ax) ? ax : 0f, float.IsFinite(ay) ? ay : 0f);
    }
}
