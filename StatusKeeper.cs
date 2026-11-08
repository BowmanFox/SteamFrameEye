// SPDX-License-Identifier: MIT
using System.Net;
using System.Net.Sockets;

namespace SteamFrameEyeModule;

/// <summary>
/// Optional status keeper (VRCFT-free; spec §8.5, off by default). VRCFaceTracking sends EyeTrackingActive and
/// Lip/ExpressionTrackingActive at most once per run and never "false" (G1), so this sends them to VRChat's OSC
/// input on 127.0.0.1 itself: per capability nothing before it was live once, then ",T" every 2 s while live, one
/// ",F" after 5 s not live and ",T" again on resume. Only while the module's Status is Active and it holds that
/// claim: a host that reports the claims (VRCFT 6.0, hostReportsClaims) is authoritative, so with both reported
/// false (another module won them) nothing is sent; a 5.4.x host never reports them (both stay false), so there the
/// claims returned by Initialize count. When Status leaves Active and at teardown: one ",F" per bool whose last
/// value sent was ",T". "face"/"all": one ",F" 30 s after Initialize for a held capability that never went live
/// (undoes VRCFT's one-shot "true"). "face" sends Lip/ExpressionTrackingActive, "all" adds EyeTrackingActive.
/// </summary>
public sealed class StatusKeeper
{
    public const string LipAddress = "/avatar/parameters/LipTrackingActive";
    public const string ExpressionAddress = "/avatar/parameters/ExpressionTrackingActive";
    public const string EyeAddress = "/avatar/parameters/EyeTrackingActive";
    public const long RepeatMs = 2000;
    public const long OffAfterMs = 5000;
    public const long NeverLiveOffMs = 30000;

    private readonly int _port;
    private readonly Func<long> _clock;
    private readonly Action<byte[]> _send;
    private readonly long _start;
    private readonly Capability? _expression, _eye;
    // VRCFT may run a last Update() (Tick) while Teardown() runs: no ",T" may follow the final ",F".
    private readonly object _lock = new();
    private bool _wasActive, _tornDown;
    private UdpClient? _udp;

    /// <param name="port">VRChat's OSC input port (status_keeper_port) for the default sender</param>
    /// <param name="mode">"off", "face" or "all"</param>
    /// <param name="clock">milliseconds</param>
    /// <param name="send">datagram sender; null = a UdpClient to 127.0.0.1:port (guarded by TestPorts). Tests always inject one.</param>
    /// <param name="hostReportsClaims">the host reports the claims it gave (VRCFT 6.0: the module detects it once from
    /// the SDK version); then usingEye/usingExpression alone decide while Active</param>
    public StatusKeeper(int port, string mode, Func<long> clock, Action<byte[]>? send = null, bool hostReportsClaims = false)
    {
        if (mode is not ("off" or "face" or "all"))
        {
            throw new ArgumentException($"status keeper mode must be off, face or all, not {mode}", nameof(mode));
        }
        Mode = mode;
        HostReportsClaims = hostReportsClaims;
        _port = port;
        _clock = clock;
        if (send is null)
        {
            TestPorts.ThrowIfForbidden(port);
            send = SendLoopback;
        }
        _send = send;
        _start = clock();
        if (mode != "off")
        {
            _expression = new Capability(LipAddress, ExpressionAddress);
            _eye = mode == "all" ? new Capability(EyeAddress) : null;
        }
    }

    public string Mode { get; }

    /// <summary>usingEye/usingExpression are authoritative while Active (VRCFT 6.0); false: 5.4.x, Initialize's claims count.</summary>
    public bool HostReportsClaims { get; }

    /// <summary>Called from the module's Update() at most every 100 ms.</summary>
    /// <param name="faceLive">the face gate is live</param>
    /// <param name="eyeFresh">an SFEY sample arrived within 1.5 s</param>
    /// <param name="statusActive">the module's Status is ModuleState.Active</param>
    /// <param name="usingEye">ModuleInformation.UsingEye (VRCFT 6.0)</param>
    /// <param name="usingExpression">ModuleInformation.UsingExpression (VRCFT 6.0)</param>
    /// <param name="claimedEye">Initialize returned eyeSuccess (used only on a host that does not report the claims)</param>
    /// <param name="claimedExpression">Initialize returned expressionSuccess (likewise)</param>
    public void Tick(bool faceLive, bool eyeFresh, bool statusActive, bool usingEye, bool usingExpression,
        bool claimedEye, bool claimedExpression)
    {
        lock (_lock)
        {
            if (_expression is null || _tornDown)
            {
                return;
            }
            if (!statusActive)
            {
                if (_wasActive)
                {
                    // Toggled off in VRCFT: the host now drops this module's data, so the bools must not stay true.
                    ReleaseTrue(_expression);
                    ReleaseTrue(_eye);
                }
                _wasActive = false;
                return;
            }
            _wasActive = true;
            // 6.0: both false means another module holds both claims, so nothing is sent (not even the 30 s ",F").
            var reported = HostReportsClaims || usingEye || usingExpression;
            var now = _clock();
            Step(_expression, reported ? usingExpression : claimedExpression, faceLive, now);
            if (_eye is not null)
            {
                Step(_eye, reported ? usingEye : claimedEye, eyeFresh, now);
            }
        }
    }

    /// <summary>One ",F" for every bool whose last value sent was ",T"; nothing is sent afterwards.</summary>
    public void OnTeardown()
    {
        lock (_lock)
        {
            if (_tornDown)
            {
                return;
            }
            _tornDown = true;
            if (_expression is not null)
            {
                ReleaseTrue(_expression);
                ReleaseTrue(_eye);
            }
            _udp?.Dispose();
            _udp = null;
        }
    }

    private void Step(Capability c, bool held, bool live, long now)
    {
        if (!held)
        {
            return; // never send for a capability another module holds
        }
        if (live)
        {
            c.EverLive = true;
            c.NotLiveSince = null;
            if (!c.SentTrue || now - c.LastTrue >= RepeatMs)
            {
                Send(c, true);
                c.LastTrue = now;
            }
            return;
        }
        if (c.EverLive)
        {
            c.NotLiveSince ??= now;
            if (c.SentTrue && now - c.NotLiveSince.Value >= OffAfterMs)
            {
                Send(c, false);
            }
        }
        else if (!c.NeverLiveOffSent && now - _start >= NeverLiveOffMs)
        {
            c.NeverLiveOffSent = true;
            Send(c, false);
        }
    }

    private void ReleaseTrue(Capability? c)
    {
        if (c is { SentTrue: true })
        {
            Send(c, false);
        }
    }

    private void Send(Capability c, bool value)
    {
        foreach (var message in value ? c.True : c.False)
        {
            _send((byte[])message.Clone());
        }
        c.SentTrue = value;
    }

    private void SendLoopback(byte[] datagram)
    {
        TestPorts.ThrowIfForbidden(_port);
        try
        {
            _udp ??= new UdpClient(AddressFamily.InterNetwork);
            _udp.Send(datagram, datagram.Length, new IPEndPoint(IPAddress.Loopback, _port));
        }
        catch (SocketException)
        {
            // VRChat is not running (nothing listens): a status bool is not worth an error
        }
        catch (ObjectDisposedException)
        {
        }
    }

    private sealed class Capability
    {
        public Capability(params string[] addresses)
        {
            True = addresses.Select(a => FaceOsc.Bool(a, true)).ToArray();
            False = addresses.Select(a => FaceOsc.Bool(a, false)).ToArray();
        }

        public byte[][] True { get; }
        public byte[][] False { get; }
        public bool EverLive { get; set; }
        public bool SentTrue { get; set; }
        public bool NeverLiveOffSent { get; set; }
        public long LastTrue { get; set; }
        public long? NotLiveSince { get; set; }
    }
}
