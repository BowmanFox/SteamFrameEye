using SteamFrameEyeModule;

// Deterministic behavioral checks, without a headset, VRCFT or external test dependencies.
var failures = 0;
void Check(bool condition, string message)
{
    Console.WriteLine($"{(condition ? "PASS" : "FAIL")}: {message}");
    if (!condition) failures++;
}
static float Rad(float degrees) => degrees * MathF.PI / 180;
static float Deg(float radians) => radians * 180 / MathF.PI;
static EyeSettings Settings() => new() { Calibration = "off", BlinkHoldMs = 0 };
static EyeSample Sample(double t, float x = 0, float y = 0, float openL = 1, float openR = 1) => new()
{
    Valid = true, SampleTime = t,
    LX = MathF.Tan(Rad(x)), LY = MathF.Tan(Rad(y)), LZ = -1,
    RX = MathF.Tan(Rad(x)), RY = MathF.Tan(Rad(y)), RZ = -1,
    OpenL = openL, OpenR = openR, CovL = 0.0004f, CovR = 0.0004f
};

foreach (var rate in new[] { 60, 90, 120 })
{
    var current = new EyeProcessor(Settings());
    var oldSettings = Settings();
    oldSettings.GazeMedianFilter = false;
    oldSettings.GazeMinCutoff = 1;
    oldSettings.GazeBeta = 8;
    var old = new EyeProcessor(oldSettings);
    var noise = new Random(193);
    double rawEnergy = 0, oldEnergy = 0, newEnergy = 0;
    for (var i = 0; i < rate * 20; i++)
    {
        var jitter = (float)(noise.NextDouble() - 0.5) * 1.5f;
        if (i % 83 == 0) jitter += 8; // occasional eye-server outlier
        var sample = Sample(i / (double)rate, jitter);
        var a = old.Process(sample);
        var b = current.Process(sample);
        if (i < rate * 2) continue; // exclude startup settling
        rawEnergy += jitter * jitter;
        oldEnergy += Math.Pow(Deg(a.LeftX), 2);
        newEnergy += Math.Pow(Deg(b.LeftX), 2);
    }
    var n = rate * 18;
    Console.WriteLine($"{rate} Hz fixation RMS deg: raw {Math.Sqrt(rawEnergy / n):F3}, previous {Math.Sqrt(oldEnergy / n):F3}, new {Math.Sqrt(newEnergy / n):F3}");
    Check(newEnergy < oldEnergy * 0.5, $"{rate} Hz: reduce synthetic jitter/spike energy by at least half");

    // A real sustained movement must not get mistaken for an isolated spike.
    foreach (var angle in new[] { 5f, 15f, -15f })
    {
        var p = new EyeProcessor(Settings());
        for (var i = 0; i < rate; i++) p.Process(Sample(i / (double)rate));
        var reachMs = double.PositiveInfinity;
        var overshoot = false;
        for (var i = 0; i < rate; i++)
        {
            var o = p.Process(Sample(1 + i / (double)rate, angle));
            if (Math.Abs(Deg(o.LeftX) - angle) <= Math.Abs(angle) * 0.1 && double.IsPositiveInfinity(reachMs))
                reachMs = i * 1000.0 / rate;
            overshoot |= Math.Abs(Deg(o.LeftX)) > Math.Abs(angle) + 0.001;
        }
        Check(reachMs <= 120 && !overshoot, $"{rate} Hz: {angle} degree step reaches 90% in {reachMs:F1} ms without overshoot");
    }
}

var spikeProcessor = new EyeProcessor(Settings());
for (var i = 0; i < 90; i++) spikeProcessor.Process(Sample(i / 90.0));
float maxSpike = 0;
for (var i = 90; i < 120; i++)
{
    var o = spikeProcessor.Process(Sample(i / 90.0, i == 90 ? 12 : 0, i == 90 ? -10 : 0));
    maxSpike = MathF.Max(maxSpike, MathF.Max(Math.Abs(o.LeftX), Math.Abs(o.LeftY)));
}
Check(maxSpike < 0.000001, "single-sample gaze spike rejected on both axes");

var both = new EyeProcessor(Settings());
EyeOutput held = default;
for (var i = 0; i < 90; i++) held = both.Process(Sample(i / 90.0, 5));
var closed = Sample(1, -30, 20, 0, 0);
closed.CovL = closed.CovR = 0.1f;
for (var i = 0; i < 30; i++)
{
    closed.SampleTime = 1 + i / 90.0;
    both.Process(closed);
}
Check(both.Output.LeftX == held.LeftX && both.Output.LeftY == held.LeftY && both.Output.OpenL == 0 && both.Output.OpenR == 0,
    "blink holds gaze and still closes both eyelids");
// The existing eyelid median takes two open samples to recognize reopening.
both.Process(Sample(1 + 30 / 90.0, 10));
var reopened = both.Process(Sample(1 + 31 / 90.0, 10));
Check(reopened.LeftX > held.LeftX && reopened.LeftX < Rad(10), "reopening follows the new gaze with no stale median delay or snap");

var perEyeSettings = Settings();
perEyeSettings.CombinedGaze = false;
var perEye = new EyeProcessor(perEyeSettings);
var stereo = Sample(0, -5);
stereo.RX = MathF.Tan(Rad(5));
var stereoOut = perEye.Process(stereo);
Check(stereoOut.LeftX < 0 && stereoOut.RightX > 0, "per-eye smoothing preserves convergence");
var wink = Sample(1, -40, 0, 0, 1);
wink.CovL = 0.1f;
wink.RX = MathF.Tan(Rad(7));
var winkOut = perEye.Process(wink);
Check(Math.Abs(Deg(winkOut.LeftX) - 7) < 0.001 && winkOut.LeftX == winkOut.RightX, "open eye steers both during a wink");

var rawSettings = Settings();
rawSettings.Smoothing = false;
var rawProcessor = new EyeProcessor(rawSettings);
rawProcessor.Process(Sample(0));
var rawOut = rawProcessor.Process(Sample(1 / 90.0, 12, -8));
Check(Math.Abs(Deg(rawOut.LeftX) - 12) < 0.001 && Math.Abs(Deg(rawOut.LeftY) + 8) < 0.001,
    "smoothing off bypasses both gaze filters");

var clockSettings = Settings();
clockSettings.BlinkHoldMs = 80;
var clock = new EyeProcessor(clockSettings);
for (var i = 0; i < 30; i++) clock.Process(Sample(100 + i / 90.0, 0, 0, 0, 0));
var reset = clock.Process(Sample(0, -12, 7));
Check(reset.OpenL == 1 && reset.OpenR == 1 && Math.Abs(Deg(reset.LeftX) + 12) < 0.001,
    "headset clock reset clears gaze history and previous blink hold");
var reconnect = clock.Process(Sample(5, 8, -3));
Check(Math.Abs(Deg(reconnect.LeftX) - 8) < 0.001, "reconnect starts at the current gaze");

// Many shallow dips (looking down, a sagging lid) around a few real blinks must not lift the closed level.
var lidSettings = new EyeSettings { BlinkHoldMs = 0 };
var lids = new EyeProcessor(lidSettings);
var lidTime = 0.0;
void Lid(float value, int samples)
{
    for (var i = 0; i < samples; i++, lidTime += 1 / 90.0)
    {
        lids.Process(Sample(lidTime, 0, 0, value, value));
    }
}
Lid(1f, 1000);
for (var round = 0; round < 10; round++)
{
    Lid(1f, 120); Lid(0.5f, 20);    // shallow dip to 0.5
    Lid(1f, 120); Lid(0.55f, 8);
    Lid(1f, 120); Lid(0.05f, 12);   // real blink
}
Lid(1f, 90);
Check(lids.Left.Calibration.Closed < 0.15f && lids.Left.Calibration.BlinksSeen == 10,
    $"shallow dips do not lift the closed level (learned {lids.Left.Calibration.Closed:F2}, {lids.Left.Calibration.BlinksSeen} blinks)");
Lid(0.8f, 30);
Check(lids.Output.OpenL > 0.75f, $"a raw 0.8 lid shows mostly open ({lids.Output.OpenL:F2})");

var path = Path.Combine(Path.GetTempPath(), $"frameeye-settings-{Guid.NewGuid():N}.json");
try
{
    File.WriteAllText(path, """{"smoothing":true,"gaze_median_filter":false,"gaze_min_cutoff":0.6,"gaze_beta":4}""");
    var loaded = EyeSettings.Load(path, Console.WriteLine);
    Check(loaded.Smoothing && !loaded.GazeMedianFilter && loaded.GazeMinCutoff == 0.6f && loaded.GazeBeta == 4,
        "JSON settings control gaze smoothing");
}
finally { File.Delete(path); }

Console.WriteLine(failures == 0 ? "All smoothing checks passed." : $"{failures} smoothing checks failed.");
return failures == 0 ? 0 : 1;
