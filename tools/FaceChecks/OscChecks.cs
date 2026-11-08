// SPDX-License-Identifier: MIT
// tools/FaceChecks, OSC: FaceOsc's parser (messages, bundles, malformed input, seeded fuzz), FaceReceiver's
// counters and its Received event, eye and unknown addresses, and FaceOsc.Bool.
using System.Text;
using SteamFrameEyeModule;
using static Checks;

static class OscChecks
{
    public static void Run()
    {
        var jaw = FaceOsc.Float("/jawOpen", 0.5f);
        Check(Convert.ToHexString(jaw) == "2F6A61774F70656E000000002C6600003F000000",
            "FaceOsc.Float(\"/jawOpen\", 0.5) = 2f 6a 61 77 4f 70 65 6e 00 00 00 00 2c 66 00 00 3f 00 00 00");
        long now = 1000;
        var rx = new FaceReceiver(0, NoLog, NoLog, () => now);
        Check(rx.Feed(jaw) && rx.Snapshot(now) is { Any: true, Messages: 1 } s1 && s1.W[4] == 0.5f && s1.W.Count(w => w != 0) == 1,
            "/jawOpen ,f 0.5 sets row 4 to 0.5 and nothing else");
        var funnel = new FaceReceiver(0, NoLog, NoLog, () => now);
        funnel.Feed(FaceOsc.Float("/mouthFunnel", 0.4f));
        var idx = new int[57];
        var val = new float[57];
        var n = FaceMapping.Fill(funnel.Snapshot(now).W, 1f, false, idx, val);
        var funnelTargets = Enumerable.Range(0, n).Where(k => val[k] != 0).Select(k => (idx[k], val[k])).OrderBy(t => t.Item1).ToArray();
        Check(funnel.Snapshot(now).W[10] == 0.4f && funnelTargets.SequenceEqual(new[] { (36, 0.4f), (37, 0.4f), (38, 0.4f), (39, 0.4f) }),
            "/mouthFunnel ,f 0.4 sets row 10, which drives LipFunnel 36..39 with 0.4");
        var prefixed = new FaceReceiver(0, NoLog, NoLog, () => now);
        Check(prefixed.Feed(Util.Message("/x/y/jawOpen", 0.25f)) && prefixed.Snapshot(now).W[4] == 0.25f,
            "prefix /x/y/jawOpen matches by the segment after the last '/'");
        Check(prefixed.Feed(Util.Message("/JAWOPEN", 0.75f)) && prefixed.Snapshot(now).W[4] == 0.75f,
            "address match is ordinal case-insensitive (/JAWOPEN)");

        var sink = new RecordingSink();
        Check(FaceOsc.TryParse(Util.Bundle(FaceOsc.Float("/jawOpen", 0.5f), FaceOsc.Float("/mouthClose", 0.1f)), sink)
              && sink.Seen.SequenceEqual(new[] { ("/jawOpen", 0.5f), ("/mouthClose", 0.1f) }),
            "a bundle of 2 delivers both messages in order");
        var depth2 = Util.Bundle(Util.Bundle(FaceOsc.Float("/jawOpen", 0.3f)));
        var depth3 = Util.Bundle(Util.Bundle(Util.Bundle(FaceOsc.Float("/jawOpen", 0.3f))));
        sink = new RecordingSink();
        Check(FaceOsc.TryParse(depth2, sink) && sink.Seen.Count == 1, "bundles nested 2 deep are accepted");
        sink = new RecordingSink();
        Check(!FaceOsc.TryParse(depth3, sink) && sink.Seen.Count == 0, "bundles nested 3 deep are rejected");
        Check(!FaceOsc.TryParse(depth2, new RecordingSink(), maxDepth: 1), "maxDepth 1 rejects a nested bundle");
        Check(FaceOsc.TryParse(Util.Bundle(), new RecordingSink()), "an empty bundle is valid and delivers nothing");

        var malformed = new (string Name, byte[] Data)[]
        {
            ("empty datagram", Array.Empty<byte>()),
            ("truncated value", jaw[..^1]),
            ("truncated before the value", jaw[..16]),
            ("truncated address without NUL", Encoding.ASCII.GetBytes("/jawOpen")),
            ("bad padding (non-zero pad byte)", Util.Concat(Encoding.ASCII.GetBytes("/jawOpen\0\0\0x"), Util.Tag(",f"), Util.Be(0.5f))),
            ("bad padding (unpadded address)", Util.Concat(Encoding.ASCII.GetBytes("/jawOpen\0"), Util.Tag(",f"), Util.Be(0.5f))),
            ("bad padding (unpadded type tag)", Util.Concat(Util.Str("/jawOpen"), Encoding.ASCII.GetBytes(",f\0"), Util.Be(0.5f))),
            ("no comma", Util.Concat(Util.Str("/jawOpen"), Util.Be(0.5f))),
            ("type tag without comma", Util.Concat(Util.Str("/jawOpen"), Util.Str("f"), Util.Be(0.5f))),
            (",i", Util.Concat(Util.Str("/jawOpen"), Util.Tag(",i"), Util.Be(1))),
            (",ff", Util.Concat(Util.Str("/jawOpen"), Util.Tag(",ff"), Util.Be(0.5f), Util.Be(0.5f))),
            (",f with an extra argument", Util.Concat(jaw, Util.Be(0.5f))),
            ("NaN", Util.Message("/jawOpen", float.NaN)),
            ("+Inf", Util.Message("/jawOpen", float.PositiveInfinity)),
            ("-Inf", Util.Message("/jawOpen", float.NegativeInfinity)),
            ("address not starting with '/'", Util.Concat(Util.Str("jawOpen"), Util.Tag(",f"), Util.Be(0.5f))),
            ("control character in the address", Util.Concat(Util.Str("/jaw\u0001Open"), Util.Tag(",f"), Util.Be(0.5f))),
            ("bundle without time tag", Encoding.ASCII.GetBytes("#bundle\0")),
            ("bundle element size not a multiple of 4", Util.Concat(Util.Bundle(), Util.Be(18), jaw[..18])),
            ("bundle element size beyond the end", Util.Concat(Util.Bundle(), Util.Be(24), jaw)),
            ("bundle element size negative", Util.Concat(Util.Bundle(), Util.Be(-20), jaw)),
            ("bundle element size 0", Util.Concat(Util.Bundle(), Util.Be(0))),
            ("bundle with a truncated size", Util.Concat(Util.Bundle(), new byte[] { 0, 0 })),
            ("bundle with one bad element", Util.Bundle(jaw, Util.Message("/mouthClose", float.NaN))),
        };
        foreach (var (name, data) in malformed)
        {
            sink = new RecordingSink();
            Check(!FaceOsc.TryParse(data, sink) && sink.Seen.Count == 0, $"rejected, nothing delivered: {name}");
        }
        var counting = new FaceReceiver(0, NoLog, NoLog, () => now);
        foreach (var (_, data) in malformed) counting.Feed(data);
        counting.Feed(jaw);
        var cs = counting.Snapshot(now);
        Check(cs is { Packets: 25, Rejected: 24, Messages: 1 } && cs.W[4] == 0.5f,
            $"the receiver counts every datagram and every rejected one ({cs.Packets} packets, {cs.Rejected} rejected)");

        // Received (wakes the module's Update): only for a datagram with face data, at most once per 5 ms.
        long t = 5000;
        var throttled = new FaceReceiver(0, NoLog, NoLog, () => t);
        var raisedAt = new List<long>();
        throttled.Received += () => raisedAt.Add(t);
        throttled.Feed(jaw);                                         // 5000: raised
        throttled.Feed(jaw);                                         // same ms: not raised
        t = 5004;
        throttled.Feed(jaw);                                         // 4 ms later: not raised
        t = 5005;
        throttled.Feed(Util.Message("/jawOpen", float.NaN));         // rejected: never raised
        throttled.Feed(FaceOsc.Float("/LeftEyeX", 0.1f));            // eye address only: not raised
        throttled.Feed(FaceOsc.Float("/unknownShape", 0.1f));        // unknown address only: not raised
        throttled.Feed(jaw);                                         // 5 ms after the last raise: raised
        t = 5009;
        throttled.Feed(jaw);
        t = 5010;
        throttled.Feed(Util.Bundle(jaw, FaceOsc.Float("/mouthClose", 0.1f))); // raised once for the whole bundle
        Check(raisedAt.SequenceEqual(new long[] { 5000, 5005, 5010 }),
            $"Received is raised at most once per 5 ms for other rows and only for datagrams with face data (raised at {string.Join(", ", raisedAt)})");

        // ... and always for the datagram with Baballonia's last row (/tongueTwistRight), so the module's Update sees the
        // whole inference: a 45-datagram burst within one ms, a Snapshot taken in every Received handler.
        long tb = 7000;
        var burstRx = new FaceReceiver(0, NoLog, NoLog, () => tb);
        var seen = new List<float[]>();
        burstRx.Received += () => seen.Add(burstRx.Snapshot(tb).W);
        Util.Burst(burstRx, 0.25f);
        tb = 7016;
        Util.Burst(burstRx, 0.75f);
        var sawWhole = new Func<float, bool>(v => seen.Any(w => w.All(x => x == v)));
        Check(seen.Count == 4 && sawWhole(0.25f) && sawWhole(0.75f) && seen[^1].All(x => x == 0.75f),
            $"Received for a burst's last row: the last handler call of each burst sees all 45 rows of that inference ({seen.Count} raises, last saw {seen.LastOrDefault()?.Count(x => x == 0.75f) ?? 0}/45 new rows)");
        tb = 7017;
        burstRx.Feed(FaceOsc.Float("/tongueTwistRight", 0.5f));
        burstRx.Feed(FaceOsc.Float("/x/TONGUETWISTRIGHT", 0.5f));
        burstRx.Feed(Util.Message("/tongueTwistRight", float.NaN));
        Check(seen.Count == 6, $"every datagram with the last row raises Received, whatever the throttle and the prefix; a rejected one does not ({seen.Count - 4} of 3)");

        // Fuzz: random bytes and mutations of valid messages and bundles; never an exception, only finite values.
        var random = new Random(20261106);
        var valid = new[] { jaw, FaceOsc.Float("/mouthFunnel", 0.4f), Util.Bundle(jaw, FaceOsc.Float("/tongueOut", 1f)), depth2,
            FaceOsc.Bool(StatusKeeper.LipAddress, true) };
        var fuzzRx = new FaceReceiver(0, NoLog, NoLog, () => now);
        var fuzzSink = new RecordingSink();
        int accepted = 0, rejected = 0;
        var threw = false;
        for (var k = 0; k < 10000; k++)
        {
            byte[] data;
            switch (k % 3)
            {
                case 0:
                    data = new byte[random.Next(0, 97)];
                    random.NextBytes(data);
                    if (data.Length > 0 && random.Next(2) == 0) data[0] = (byte)'/';
                    break;
                case 1:
                    data = (byte[])valid[random.Next(valid.Length)].Clone();
                    for (var f = random.Next(1, 4); f > 0; f--) data[random.Next(data.Length)] = (byte)random.Next(256);
                    break;
                default:
                    var src = valid[random.Next(valid.Length)];
                    data = random.Next(2) == 0 ? src[..random.Next(src.Length + 1)] : Util.Concat(src, new byte[random.Next(1, 9)]);
                    break;
            }
            try
            {
                if (FaceOsc.TryParse(data, fuzzSink)) accepted++; else rejected++;
                fuzzRx.Feed(data);
            }
            catch (Exception e)
            {
                threw = true;
                Console.WriteLine($"  fuzz case {k} threw {e.GetType().Name}: {Convert.ToHexString(data)}");
            }
        }
        var fs = fuzzRx.Snapshot(now);
        Check(!threw && accepted + rejected == 10000 && fs.Packets == 10000 && fs.Rejected == rejected
              && fuzzSink.Seen.All(m => float.IsFinite(m.Value) && m.Address.StartsWith('/') && m.Address.All(c => c > ' ' && c < 0x7f)),
            $"10000 seeded random datagrams: no exception, {accepted} accepted with finite values only, {rejected} rejected");

        // Eye and unknown addresses are counted and ignored; the first eye message and the first 5 unknown ones are logged.
        var warns = new List<string>();
        var infos = new List<string>();
        var eyeRx = new FaceReceiver(0, infos.Add, warns.Add, () => now);
        eyeRx.Feed(FaceOsc.Float("/LeftEyeX", 0.1f));
        eyeRx.Feed(FaceOsc.Float("/RightEyeLid", 0.9f));
        for (var u = 0; u < 7; u++) eyeRx.Feed(FaceOsc.Float($"/unknownShape{u}", 0.5f));
        var es = eyeRx.Snapshot(now);
        Check(es is { EyeMessages: 2, Unknown: 7, Messages: 0, Any: false } && es.W.All(w => w == 0),
            "eye addresses and unknown names are counted, not applied");
        Check(warns.SequenceEqual(new[] { "Baballonia sends eye addresses; turn its eye cameras off, eyes come from the Frame" }),
            "the first eye address is logged once with the hint to turn Baballonia's eye cameras off");
        Check(infos.Count == 5 && infos.All(m => m.StartsWith("ignoring OSC address /unknownShape")), "only the first 5 unknown addresses are logged");

        Check(Convert.ToHexString(FaceOsc.Bool(StatusKeeper.LipAddress, true))
              == Convert.ToHexString(Encoding.ASCII.GetBytes("/avatar/parameters/LipTrackingActive\0\0\0\0,T\0\0")),
            "FaceOsc.Bool builds \"/avatar/parameters/LipTrackingActive\" ,T exactly");
    }
}
