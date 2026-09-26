using System.Globalization;
using SteamFrameEyeModule;

// usage: ReplayProbe <recording.csv> [settings.json] [out.csv]
if (args.Length < 1)
{
    Console.Error.WriteLine("usage: ReplayProbe <recording.csv> [settings.json] [out.csv]");
    return 2;
}
var inv = CultureInfo.InvariantCulture;
var settings = args.Length > 1 && args[1] != "-" ? EyeSettings.Load(args[1], Console.Error.WriteLine) : new EyeSettings();

var lines = File.ReadAllLines(args[0]);
var header = lines[0].Split(',');
int Col(string name) => Array.IndexOf(header, name);
var c = new Dictionary<string, int>();
foreach (var name in new[] { "t", "state", "gLx", "gLy", "gLz", "gRx", "gRy", "gRz", "fx", "fy", "fz", "oL", "oR", "cLx", "cLy", "cLz", "cRx", "cRy", "cRz" })
{
    c[name] = Col(name);
}

var samples = new List<EyeSample>();
foreach (var line in lines.Skip(1))
{
    var f = line.Split(',');
    if (f.Length != header.Length)
    {
        continue;
    }
    float F(string n) => float.Parse(f[c[n]], inv);
    samples.Add(new EyeSample
    {
        SampleTime = double.Parse(f[c["t"]], inv),
        Valid = F("state") == 1,
        LX = F("gLx"), LY = F("gLy"), LZ = F("gLz"),
        RX = F("gRx"), RY = F("gRy"), RZ = F("gRz"),
        FX = F("fx"), FY = F("fy"), FZ = F("fz"),
        OpenL = F("oL"), OpenR = F("oR"),
        CovL = MathF.Max(F("cLx"), MathF.Max(F("cLy"), F("cLz"))),
        CovR = MathF.Max(F("cRx"), MathF.Max(F("cRy"), F("cRz"))),
    });
}

var saved = Environment.GetEnvironmentVariable("LOAD_CALIBRATION") is { Length: > 0 } loadPath
    ? System.Text.Json.Nodes.JsonNode.Parse(File.ReadAllText(loadPath)) : null;
var proc = new EyeProcessor(settings, saved);
Console.WriteLine($"start: left closed {proc.Left.Calibration.Closed:F2} open {proc.Left.Calibration.Open:F2} | right closed {proc.Right.Calibration.Closed:F2} open {proc.Right.Calibration.Open:F2}" + (saved is null ? " (defaults)" : " (loaded)"));
var n = samples.Count;
var rawX = new float[n]; var rawY = new float[n];
var outX = new float[n]; var outY = new float[n];
var outL = new float[n]; var outR = new float[n];
var normL = new float[n]; var normR = new float[n];
for (var i = 0; i < n; i++)
{
    var s = samples[i];
    rawX[i] = MathF.Atan2(s.LX + s.RX, -(s.LZ + s.RZ));
    rawY[i] = MathF.Atan2(s.LY + s.RY, -(s.LZ + s.RZ));
    var o = proc.Process(s);
    if (i > 0 && (int)(s.SampleTime - samples[0].SampleTime) / 60 != (int)(samples[i - 1].SampleTime - samples[0].SampleTime) / 60)
        Console.WriteLine($"  t={s.SampleTime - samples[0].SampleTime,4:F0}s  left closed {proc.Left.Calibration.Closed:F2} open {proc.Left.Calibration.Open:F2} | right closed {proc.Right.Calibration.Closed:F2} open {proc.Right.Calibration.Open:F2}");
    outX[i] = o.LeftX; outY[i] = o.LeftY;
    outL[i] = o.OpenL; outR[i] = o.OpenR;
    normL[i] = proc.Left.Normalized; normR[i] = proc.Right.Normalized;
}

static float Deg(float r) => r * 180f / MathF.PI;
static string P(IEnumerable<float> values, params double[] ps)
{
    var s = values.OrderBy(v => v).ToArray();
    return s.Length == 0 ? "-" : string.Join(" ", ps.Select(p => $"p{p}={s[Math.Min(s.Length - 1, (int)(p / 100 * s.Length))]:F3}"));
}

Console.WriteLine($"samples {n}, {samples[^1].SampleTime - samples[0].SampleTime:F0} s");
Console.WriteLine($"calibration left  closed {proc.Left.Calibration.Closed:F3} open {proc.Left.Calibration.Open:F3} ({proc.Left.Calibration.BlinksSeen} blinks)");
Console.WriteLine($"calibration right closed {proc.Right.Calibration.Closed:F3} open {proc.Right.Calibration.Open:F3} ({proc.Right.Calibration.BlinksSeen} blinks)");
Console.WriteLine($"gaze held during blinks: {proc.GazeHeldSamples} samples ({100.0 * proc.GazeHeldSamples / n:F1} %)");
Console.WriteLine($"unreliable samples: {proc.UnreliableSamples} ({100.0 * proc.UnreliableSamples / n:F1} %), both eyes shown exactly open {outL.Zip(outR).Count(p => p.First == 1f && p.Second == 1f) * 100.0 / n:F1} % of samples");
Console.WriteLine("open level by pitch band: left  " + string.Join(" ", Enumerable.Range(0, 7).Select(b => proc.Left.Calibration.BandOpen(b).ToString("F2", inv))));
Console.WriteLine("                          right " + string.Join(" ", Enumerable.Range(0, 7).Select(b => proc.Right.Calibration.BandOpen(b).ToString("F2", inv))));
if (Environment.GetEnvironmentVariable("SAVE_CALIBRATION") is { Length: > 0 } calPath)
{
    File.WriteAllText(calPath, proc.CalibrationJson().ToJsonString(new System.Text.Json.JsonSerializerOptions { WriteIndented = true }));
}
for (var band = 0; band < 3; band++)
{
    var lo = band switch { 0 => -90f, 1 => -12f, _ => 6f };
    var hi = band switch { 0 => -12f, 1 => 6f, _ => 90f };
    var idx = Enumerable.Range(0, n).Where(i => Deg(rawY[i]) >= lo && Deg(rawY[i]) < hi && samples[i].OpenL > 0.6f && samples[i].OpenR > 0.6f).ToArray();
    if (idx.Length > 0)
    {
        Console.WriteLine($"eyes open (raw > 0.6), pitch {lo,4:F0}..{hi,3:F0} deg: out >= 0.95 left {100.0 * idx.Count(i => outL[i] >= 0.95f) / idx.Length:F0} % right {100.0 * idx.Count(i => outR[i] >= 0.95f) / idx.Length:F0} %, median out left {idx.Select(i => outL[i]).OrderBy(v => v).ElementAt(idx.Length / 2):F2} right {idx.Select(i => outR[i]).OrderBy(v => v).ElementAt(idx.Length / 2):F2}");
    }
}

// Gaze noise in fixation windows (15 samples, raw range < 2 deg), eyes open.
var rawStd = new List<float>(); var outStd = new List<float>();
for (var i = 0; i + 15 <= n; i += 15)
{
    var idx = Enumerable.Range(i, 15).ToArray();
    if (idx.Any(k => normL[k] < 0.6f || normR[k] < 0.6f)) continue;
    var rx = idx.Select(k => rawX[k]).ToArray(); var ry = idx.Select(k => rawY[k]).ToArray();
    if (Deg(rx.Max() - rx.Min()) > 2 || Deg(ry.Max() - ry.Min()) > 2) continue;
    static float Std(float[] v) { var m = v.Average(); return MathF.Sqrt(v.Select(x => (x - m) * (x - m)).Average()); }
    rawStd.Add(Deg(MathF.Sqrt(Std(rx) * Std(rx) + Std(ry) * Std(ry))));
    var ox = idx.Select(k => outX[k]).ToArray(); var oy = idx.Select(k => outY[k]).ToArray();
    outStd.Add(Deg(MathF.Sqrt(Std(ox) * Std(ox) + Std(oy) * Std(oy))) / settings.GazeScale);
}
Console.WriteLine($"fixation noise deg (std in 170 ms windows, n={rawStd.Count}): raw {P(rawStd, 50, 90)} | out {P(outStd, 50, 90)}");

// Per-sample gaze steps with eyes open vs. during lid dips.
float Step(float[] x, float[] y, int i) => Deg(MathF.Sqrt((x[i] - x[i - 1]) * (x[i] - x[i - 1]) + (y[i] - y[i - 1]) * (y[i] - y[i - 1])));
var openIdx = Enumerable.Range(1, n - 1).Where(i => normL[i] > 0.6f && normR[i] > 0.6f).ToArray();
var lowIdx = Enumerable.Range(1, n - 1).Where(i => (normL[i] + normR[i]) / 2 < 0.4f).ToArray();
Console.WriteLine($"gaze step deg, eyes open: raw {P(openIdx.Select(i => Step(rawX, rawY, i)), 50, 90, 99)} | out {P(openIdx.Select(i => Step(outX, outY, i) / settings.GazeScale), 50, 90, 99)}");
Console.WriteLine($"gaze step deg, blinks:    raw {P(lowIdx.Select(i => Step(rawX, rawY, i)), 50, 90, 99)} | out {P(lowIdx.Select(i => Step(outX, outY, i) / settings.GazeScale), 50, 90, 99)}");

// Saccade response: raw moves > 5 deg within 3 samples and settles; time until output covers 90 %.
var lags = new List<float>();
for (var i = 5; i + 40 < n; i++)
{
    var before = new Vector(rawX[i - 3], rawY[i - 3]); var after = new Vector(rawX[i], rawY[i]);
    if (Deg(before.Dist(after)) < 5 || normL[i] < 0.6f || normR[i] < 0.6f) continue;
    var target = new Vector(rawX[i + 5], rawY[i + 5]);
    if (Deg(after.Dist(target)) > 1) continue;
    var start = new Vector(outX[i - 3], outY[i - 3]) / settings.GazeScale;
    var total = start.Dist(target);
    for (var k = i - 3; k < i + 40; k++)
    {
        var o = new Vector(outX[k], outY[k]) / settings.GazeScale;
        if (o.Dist(target) <= 0.1f * total)
        {
            lags.Add((float)((samples[k].SampleTime - samples[i].SampleTime) * 1000));
            break;
        }
    }
    i += 20;
}
Console.WriteLine($"saccades >5 deg: {lags.Count}, output within 10 % of target after ms: {P(lags, 50, 90)}");

// Eyelids.
foreach (var (name, raw, norm, outp) in new[] { ("left", samples.Select(s => s.OpenL).ToArray(), normL, outL), ("right", samples.Select(s => s.OpenR).ToArray(), normR, outR) })
{
    var fullOpen = outp.Count(v => v >= 1f) * 100.0 / n;
    var closed = outp.Count(v => v <= 0f) * 100.0 / n;
    // flicker: output drops below 0.5 for at most 2 samples between fully open neighbours
    var flicker = 0;
    for (var i = 1; i + 3 < n; i++)
    {
        if (outp[i - 1] >= 0.9f && outp[i] < 0.5f && (outp[i + 1] >= 0.9f || outp[i + 2] >= 0.9f)) flicker++;
    }
    // blinks where both eyes closed in the raw data: does the output reach 0?
    Console.WriteLine($"{name}: raw {P(raw, 1, 5, 50, 95)} | out {P(outp, 1, 5, 50, 95)} | fully open {fullOpen:F1} % closed {closed:F1} % | 1-2 sample flicker {flicker}");
}
var bilateral = 0; var reached = 0;
for (var i = 1; i < n; i++)
{
    if (normL[i] < 0.3f && normR[i] < 0.3f && !(normL[i - 1] < 0.3f && normR[i - 1] < 0.3f))
    {
        bilateral++;
        var j = i; var hit = false;
        while (j < n && j < i + 60 && (normL[j] < 0.5f || normR[j] < 0.5f)) { if (outL[j] <= 0f && outR[j] <= 0f) hit = true; j++; }
        if (hit) reached++;
    }
}
Console.WriteLine($"blinks with both eyes below 30 % (calibrated): {bilateral}, output fully closed on both eyes in {reached}");

if (args.Length > 2)
{
    using var w = new StreamWriter(args[2]);
    w.WriteLine("t,rawX,rawY,outX,outY,rawL,rawR,normL,normR,outL,outR");
    for (var i = 0; i < n; i++)
    {
        w.WriteLine(string.Join(",", new[] { samples[i].SampleTime, rawX[i], rawY[i], outX[i], outY[i], samples[i].OpenL, samples[i].OpenR, normL[i], normR[i], outL[i], outR[i] }
            .Select(v => Convert.ToDouble(v).ToString("G6", inv))));
    }
}
return 0;

readonly record struct Vector(float X, float Y)
{
    public float Dist(Vector o) => MathF.Sqrt((X - o.X) * (X - o.X) + (Y - o.Y) * (Y - o.Y));
    public static Vector operator /(Vector v, float d) => new(v.X / d, v.Y / d);
}
