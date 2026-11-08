// SPDX-License-Identifier: MIT
// tools/FaceChecks: the PASS/FAIL bookkeeping that every section uses through "using static Checks", a sink that
// records what FaceOsc delivers, and helpers for OSC bytes, Baballonia bursts and free loopback ports.
using System.Buffers.Binary;
using System.Net;
using System.Net.Sockets;
using System.Text;
using SteamFrameEyeModule;

/// <summary>Counts the checks and prints PASS or FAIL for each.</summary>
static class Checks
{
    public static int Total { get; private set; }
    public static int Failures { get; private set; }

    /// <summary>A log callback that drops the line.</summary>
    public static readonly Action<string> NoLog = _ => { };

    public static void Check(bool condition, string message)
    {
        Total++;
        Console.WriteLine($"{(condition ? "PASS" : "FAIL")}: {message}");
        if (!condition) Failures++;
    }

    public static bool Throws<T>(Action action, Func<T, bool>? test = null) where T : Exception
    {
        try { action(); }
        catch (T e) { return test?.Invoke(e) ?? true; }
        catch (Exception) { return false; }
        return false;
    }
}

/// <summary>Collects what FaceOsc delivers.</summary>
sealed class RecordingSink : IFloatSink
{
    public List<(string Address, float Value)> Seen { get; } = new();

    public void OnFloat(ReadOnlySpan<byte> address, float value) => Seen.Add((Encoding.ASCII.GetString(address), value));
}

static class Util
{
    public static byte[] Concat(params byte[][] parts) => parts.SelectMany(p => p).ToArray();

    /// <summary>OSC string: text, NUL, NUL padding to a multiple of 4.</summary>
    public static byte[] Str(string s)
    {
        var b = new byte[(Encoding.ASCII.GetByteCount(s) + 4) & ~3];
        Encoding.ASCII.GetBytes(s, b);
        return b;
    }

    public static byte[] Tag(string tags) => Str(tags);

    public static byte[] Be(float v)
    {
        var b = new byte[4];
        BinaryPrimitives.WriteSingleBigEndian(b, v);
        return b;
    }

    public static byte[] Be(int v)
    {
        var b = new byte[4];
        BinaryPrimitives.WriteInt32BigEndian(b, v);
        return b;
    }

    /// <summary>A ",f" message without FaceOsc's checks (for NaN, odd addresses).</summary>
    public static byte[] Message(string address, float v) => Concat(Str(address), Tag(",f"), Be(v));

    /// <summary>#bundle, time tag 1 (immediately), then [size][element] per element.</summary>
    public static byte[] Bundle(params byte[][] elements) =>
        Concat(new[] { Encoding.ASCII.GetBytes("#bundle\0"), new byte[] { 0, 0, 0, 0, 0, 0, 0, 1 } }
            .Concat(elements.SelectMany(e => new[] { Be(e.Length), e })).ToArray());

    /// <summary>One Baballonia inference: the 45 messages, one datagram each, all with value v.</summary>
    public static void Burst(FaceReceiver rx, float v, int sourcePort = 0)
    {
        foreach (var row in FaceMapping.Map) rx.Feed(FaceOsc.Float(row.Address, v), sourcePort);
    }

    public static int FreeUdpPort()
    {
        for (var k = 0; k < 64; k++)
        {
            using var probe = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp);
            probe.Bind(new IPEndPoint(IPAddress.Loopback, 0));
            var port = ((IPEndPoint)probe.LocalEndPoint!).Port;
            if (TestPorts.Forbidden?.Contains(port) != true) return port;
        }
        throw new InvalidOperationException("the OS offered only forbidden ports");
    }

    /// <summary>True if an exclusive bind of 127.0.0.1:port succeeds (nothing holds it); the probe socket is closed again.</summary>
    public static bool PortFree(int port)
    {
        TestPorts.ThrowIfForbidden(port);
        try
        {
            using var probe = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp) { ExclusiveAddressUse = true };
            probe.Bind(new IPEndPoint(IPAddress.Loopback, port));
            return true;
        }
        catch (SocketException)
        {
            return false;
        }
    }

    public static void SendTo(UdpClient client, int port, byte[] datagram)
    {
        TestPorts.ThrowIfForbidden(port);
        client.Send(datagram, datagram.Length, new IPEndPoint(IPAddress.Loopback, port));
    }

    public static bool WaitFor(Func<bool> condition, int milliseconds)
    {
        var until = Environment.TickCount64 + milliseconds;
        while (!condition())
        {
            if (Environment.TickCount64 > until) return false;
            Thread.Sleep(5);
        }
        return true;
    }
}
