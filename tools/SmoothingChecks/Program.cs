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

// One lid misread as closed for seconds: linked eyelids keep both open, a real blink still closes both.
EyeOutput Lids(string mode, float openL, float openR, int samples = 270)
{
    var p = new EyeProcessor(new EyeSettings { Calibration = "fixed", Eyelids = mode });
    for (var i = 0; i < 45; i++) p.Process(Sample(i / 90.0));
    for (var i = 45; i < 45 + samples; i++) p.Process(Sample(i / 90.0, 0, 0, openL, openR));
    return p.Output;
}
var perEyeSag = Lids("per_eye", 1f, 0.1f);
var linkedSag = Lids("linked", 1f, 0.1f);
var linkedBlink = Lids("linked", 0.05f, 0.05f, 30);
var leftOnly = Lids("left", 1f, 0.1f);
Check(perEyeSag.OpenR == 0f && linkedSag.OpenL == 1f && linkedSag.OpenR == 1f && leftOnly.OpenR == 1f,
    "linked/left eyelids keep an eye open that the headset misreads as closed");
Check(linkedBlink.OpenL == 0f && linkedBlink.OpenR == 0f, "linked eyelids still close both eyes in a blink");

// Linked eyelids: a real wink hides that pupil (high covariance) and passes through even though the open eye narrows;
// a one-sided lid misread keeps the pupil tracked and stays linked.
(float minR, float minL, float endR, float endL) LinkedRight(Func<int, (float l, float r, float covR)> eyes, int samples)
{
    var p = new EyeProcessor(new EyeSettings { Calibration = "fixed", Eyelids = "linked" });
    for (var i = 0; i < 90; i++) p.Process(Sample(i / 90.0));
    float minR = 1, minL = 1;
    for (var i = 0; i < samples; i++)
    {
        var (l, r, covR) = eyes(i);
        var s = Sample(1 + i / 90.0, 0, 0, l, r);
        s.CovR = covR;
        var o = p.Process(s);
        minR = MathF.Min(minR, o.OpenR);
        minL = MathF.Min(minL, o.OpenL);
    }
    return (minR, minL, p.Output.OpenR, p.Output.OpenL);
}
var heldWink = LinkedRight(i => i < 2 ? (0.9f, 0.5f, 0.0004f) : (0.55f, 0.03f, i % 7 == 0 ? 0.008f : 0.05f), 90);
var misread = LinkedRight(i => (1f, i < 30 ? 1f - i / 30f : 0.03f, 0.004f), 180);
var bothLidsRead = LinkedRight(i => i < 2 ? (0.9f, 0.5f, 0.0004f) : (0f, 0.03f, 0.05f), 90);
var staggered = LinkedRight(i => i < 23 ? (1f, 0f, 0.012f) : i < 45 ? (0f, 0f, 0.004f) : (1f, 1f, 0.0004f), 90);
Check(heldWink.endR == 0f && heldWink.endL > 0.4f, $"linked eyelids pass a wink through while the open eye narrows (open eye {heldWink.endL:F2})");
Check(bothLidsRead.endR == 0f && bothLidsRead.endL == 1f,
    "linked eyelids show a wink when the open eye's lid also reads closed but its pupil is tracked");
Check(misread.minR > 0.9f, $"linked eyelids keep an eye open whose lid is misread with its pupil tracked ({misread.minR:F2})");
Check(MathF.Abs(staggered.minR - staggered.minL) < 0.01f, "linked eyelids keep a staggered blink (one eye first by 250 ms) together");

// Lids stuck near closed: open and closed come from gaze and pupil tracking until the lids recover.
EyeOutput Stuck(params (int samples, float lid, float covL, float covR, Func<int, float> yaw)[] phases)
{
    var p = new EyeProcessor(new EyeSettings { Calibration = "fixed", Eyelids = "linked" });
    var n = 0;
    foreach (var (samples, lid, covL, covR, yaw) in phases)
    {
        for (var i = 0; i < samples; i++, n++)
        {
            var s = Sample(n / 90.0, yaw(i), 0, lid, lid);
            s.CovL = covL;
            s.CovR = covR;
            p.Process(s);
        }
    }
    return p.Output;
}
static float Looking(int i) => i / 30 % 2 == 0 ? -6f : 6f;
static float Still(int i) => -25f;
var stuckOpen = Stuck((270, 0f, 0.001f, 0.001f, Looking));
var stuckMoving = Stuck((270, 0f, 0.006f, 0.006f, Looking));
var closedEyes = Stuck((90, 1f, 0.001f, 0.001f, Looking), (540, 0f, 0.02f, 0.02f, Still));
var stuckThenClosed = Stuck((270, 0f, 0.001f, 0.001f, Looking), (270, 0f, 0.0045f, 0.0045f, Still));
var stuckThenOpen = Stuck((270, 0f, 0.001f, 0.001f, Looking), (270, 0f, 0.0045f, 0.0045f, Still), (45, 0f, 0.001f, 0.001f, Looking));
var notLocked = Stuck((180, 0f, 0.07f, 0.07f, Still));
var recovered = Stuck((270, 0f, 0.001f, 0.001f, Looking), (90, 1f, 0.001f, 0.001f, Looking), (15, 0.03f, 0.001f, 0.001f, Looking));
Check(stuckOpen.OpenL == 1f && stuckOpen.OpenR == 1f, "lids stuck on tracked pupils show open");
Check(stuckMoving.OpenL == 1f, "lids stuck with raised covariance but a moving gaze show open");
Check(MathF.Abs(stuckOpen.LeftX) > Rad(1), "gaze keeps following while the lids are stuck");
Check(closedEyes.OpenL == 0f && closedEyes.OpenR == 0f, "really closed eyes stay closed for seconds");
Check(stuckThenClosed.OpenL == 0f, "closing the eyes while the lids are stuck shows closed");
Check(stuckThenOpen.OpenL == 1f, "and opening them again shows open");
Check(notLocked.OpenL == 1f && notLocked.OpenR == 1f, "eyes show open while the tracker has no lock");
Check(recovered.OpenL == 0f, "recovered lids drive blinks again");
var cappedWink = new EyeProcessor(new EyeSettings { Calibration = "fixed", Eyelids = "linked" });
var capStart = 0f;
for (var i = 0; i < 720; i++)
{
    var s = Sample(i / 90.0, 0, 0, i < 90 ? 1f : 0f, 1f);
    s.CovL = i < 90 ? 0.001f : 0.06f;
    s.CovR = 0.001f;
    var o = cappedWink.Process(s);
    if (i == 270) capStart = o.OpenL;
}
Check(capStart == 0f && cappedWink.Output.OpenL == 1f, "one eye lost for seconds stops counting as a wink");

var path = Path.Combine(Path.GetTempPath(), $"frameeye-settings-{Guid.NewGuid():N}.json");
try
{
    File.WriteAllText(path, """{"smoothing":true,"gaze_median_filter":false,"gaze_min_cutoff":0.6,"gaze_beta":4,"eyelids":"Linked","lid_rescue":false}""");
    var loaded = EyeSettings.Load(path, Console.WriteLine);
    Check(loaded.Smoothing && !loaded.GazeMedianFilter && loaded.GazeMinCutoff == 0.6f && loaded.GazeBeta == 4,
        "JSON settings control gaze smoothing");
    Check(loaded.Eyelids == "linked" && !loaded.LidRescue, "JSON settings select linked eyelids and the lid rescue");
}
finally { File.Delete(path); }

Console.WriteLine(failures == 0 ? "All smoothing checks passed." : $"{failures} smoothing checks failed.");
return failures == 0 ? 0 : 1;
