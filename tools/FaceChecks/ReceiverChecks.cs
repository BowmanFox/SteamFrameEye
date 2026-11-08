// SPDX-License-Identifier: MIT
// tools/FaceChecks, FaceReceiver on a free loopback port (frames, counters, the exclusive bind, CloseSocket) and
// the module's wake pattern over loopback (the last row of each inference reaches an Update-like loop at once).
using System.Net;
using System.Net.Sockets;
using SteamFrameEyeModule;
using static Checks;

static class ReceiverChecks
{
    /// <summary>Receiver on a free loopback port.</summary>
    public static void Loopback()
    {
        var port = Util.FreeUdpPort();
        var infos = new List<string>();
        var rx = new FaceReceiver(port, m => { lock (infos) infos.Add(m); }, m => { lock (infos) infos.Add(m); });
        var raised = 0;
        rx.Received += () => Interlocked.Increment(ref raised);
        Check(rx.Start(out var error) && error is null && rx.IsListening, $"FaceReceiver listens on 127.0.0.1:{port} (a free port)");
        using var client = new UdpClient(AddressFamily.InterNetwork);
        client.Client.Bind(new IPEndPoint(IPAddress.Loopback, 0));
        var clientPort = ((IPEndPoint)client.Client.LocalEndPoint!).Port;
        var sent = Enumerable.Range(0, 45).Select(i => i == 4 ? 0.6f : 0.01f * i).ToArray();
        for (var frame = 0; frame < 3; frame++)
        {
            for (var i = 0; i < 45; i++) Util.SendTo(client, port, FaceOsc.Float(FaceMapping.Map[i].Address, sent[i]));
            Thread.Sleep(20);
        }
        Util.SendTo(client, port, Util.Message("/jawOpen", float.NaN));
        var got = Util.WaitFor(() => rx.Snapshot(Environment.TickCount64).Packets >= 136, 3000);
        var s = rx.Snapshot(Environment.TickCount64);
        Check(got && s is { Any: true, Messages: 135, Packets: 136, Rejected: 1 } && s.W.SequenceEqual(sent) && s.SourcePort == clientPort,
            $"loopback: 3 frames of 45 messages and one bad datagram received ({s.Messages} messages, {s.Rejected} rejected, source port {s.SourcePort})");
        Check(raised is >= 3 and <= 136 && s.RateHz == 3,
            $"Received was raised ({raised} times: once per frame for its last row, otherwise at most once per 5 ms) and RateHz counts frames ({s.RateHz})");

        var second = new FaceReceiver(port, NoLog, NoLog);
        var secondOk = second.Start(out var conflict);
        Check(!secondOk && !second.IsListening && conflict is not null
              && conflict.StartsWith($"cannot bind 127.0.0.1:{port} (")
              && conflict.EndsWith("); is the VRCFT-Babble module or another Baballonia listener running? Face tracking is off"),
            $"a second listener on the same port fails with the §8.2 error ({conflict})");
        using (var thief = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp))
        {
            thief.SetSocketOption(SocketOptionLevel.Socket, SocketOptionName.ReuseAddress, true);
            TestPorts.ThrowIfForbidden(port);
            var stolen = !Throws<SocketException>(() => thief.Bind(new IPEndPoint(IPAddress.Loopback, port)));
            Check(!stolen, "even a socket with SO_REUSEADDR cannot bind the receiver's port (exclusive bind)");
        }

        rx.CloseSocket();
        // Windows ends a pending receive when its socket closes; elsewhere the 500 ms receive timeout bounds it.
        var joinMs = OperatingSystem.IsWindows() ? 100 : 1000;
        var joined = rx.Join(joinMs);
        Check(!rx.IsListening && joined, $"CloseSocket ends the receive thread (joined within {joinMs} ms)");
        var again = new FaceReceiver(port, NoLog, NoLog);
        Check(again.Start(out _) && again.IsListening, "the port is free again after the socket was closed");
        again.Stop();
        Check(!again.IsListening, "Stop closes the socket");
    }

    /// <summary>Loopback: the module's wake pattern sees whole inferences.</summary>
    public static void WakePattern()
    {
        // Like the module's Update: Received sets an AutoResetEvent, a loop waits at most 20 ms on it and takes a Snapshot.
        // The last datagram of an inference (/tongueTwistRight, row 44) must be visible right after the burst was sent,
        // not only when the 20 ms wait times out.
        var port = Util.FreeUdpPort();
        var rx = new FaceReceiver(port, NoLog, NoLog);
        using var wake = new AutoResetEvent(false);
        rx.Received += () => wake.Set();
        var started = rx.Start(out _);
        const int Bursts = 20;
        var sentAt = new long[Bursts];
        var seenAt = new long[Bursts];
        var stop = false;
        var loop = new Thread(() =>
        {
            var last = -1f;
            while (!Volatile.Read(ref stop))
            {
                wake.WaitOne(20);
                var w = rx.Snapshot(Environment.TickCount64).W[44];
                var k = (int)MathF.Round(w * 100) - 1;
                if (w != last && k is >= 0 and < Bursts && seenAt[k] == 0) seenAt[k] = System.Diagnostics.Stopwatch.GetTimestamp();
                last = w;
            }
        }) { IsBackground = true, Name = "update-like" };
        loop.Start();
        using var client = new UdpClient(AddressFamily.InterNetwork);
        client.Client.Bind(new IPEndPoint(IPAddress.Loopback, 0));
        for (var k = 0; k < Bursts && started; k++)
        {
            var v = (k + 1) / 100f;
            foreach (var row in FaceMapping.Map) Util.SendTo(client, port, FaceOsc.Float(row.Address, v));
            sentAt[k] = System.Diagnostics.Stopwatch.GetTimestamp();
            Thread.Sleep(45);
        }
        Thread.Sleep(50);
        Volatile.Write(ref stop, true);
        loop.Join(1000);
        rx.Stop();
        var lags = Enumerable.Range(0, Bursts).Where(k => seenAt[k] != 0)
            .Select(k => (seenAt[k] - sentAt[k]) * 1000.0 / System.Diagnostics.Stopwatch.Frequency).OrderBy(x => x).ToArray();
        var median = lags.Length > 0 ? lags[lags.Length / 2] : double.NaN;
        Check(started && lags.Length == Bursts && median < 8,
            $"loopback: row 44 of each inference reaches an Update-like wait-and-Snapshot loop {median:F1} ms (median; slowest {(lags.Length > 0 ? lags[^1] : double.NaN):F1} ms) after the burst's last send ({lags.Length} of {Bursts} bursts seen)");
    }
}
