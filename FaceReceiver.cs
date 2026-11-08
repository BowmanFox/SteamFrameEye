// SPDX-License-Identifier: MIT
using System.Net;
using System.Net.Sockets;
using System.Text;

namespace SteamFrameEyeModule;

/// <summary>What the face receiver has seen so far (a copy; times in ms of the receiver's clock).</summary>
public sealed class FaceSnapshot
{
    /// <summary>Last received value per Babble45 row (finite, not clamped); 0 until the first message.</summary>
    public float[] W { get; init; } = new float[FaceMapping.Count];
    /// <summary>Clock time of the last face message.</summary>
    public long LastRx { get; init; }
    /// <summary>
    /// Messages of the most frequent address in the trailing second: its arrivals newer than now - 1000 ms, counted up
    /// to <see cref="FaceReceiver.RateCap"/>.
    /// </summary>
    public int RateHz { get; init; }
    /// <summary>At least one face message has arrived.</summary>
    public bool Any { get; init; }
    public long Packets { get; init; }
    public long Messages { get; init; }
    public long Rejected { get; init; }
    public long Unknown { get; init; }
    public long EyeMessages { get; init; }
    /// <summary>UDP source port of the last datagram.</summary>
    public int SourcePort { get; init; }
}

/// <summary>
/// Babble OSC receiver (VRCFT-free; spec §8.2): UDP 127.0.0.1:port with ExclusiveAddressUse (so a second
/// listener such as the VRCFT-Babble module cannot share it), one background thread "SteamFrameFaceRx".
/// </summary>
public sealed class FaceReceiver
{
    /// <summary>RateHz counts the arrivals of the trailing <see cref="RateWindowMs"/> ms.</summary>
    public const int RateWindowMs = 1000;
    /// <summary>Arrival times kept per row, so RateHz stops at this count (well above face_min_rate_hz's maximum of 60).</summary>
    public const int RateCap = 128;

    private readonly int _port;
    private readonly Action<string> _info, _warn;
    private readonly Func<long> _clock;
    private readonly Sink _sink;
    private readonly object _lock = new();
    private readonly float[] _w = new float[FaceMapping.Count];
    // Per row a ring of its last RateCap arrival times (row i at [i * RateCap, (i + 1) * RateCap); long.MinValue = none).
    private readonly long[] _arrivals = new long[FaceMapping.Count * RateCap];
    private readonly int[] _nextArrival = new int[FaceMapping.Count];
    private long _lastRx, _packets, _messages, _rejected, _unknown, _eyeMessages, _now;
    private long? _lastRaised;
    private bool _any, _matchedLastRow;
    private int _sourcePort, _matched;
    private List<(bool Warn, string Text)>? _logs;
    private UdpClient? _udp;
    private Thread? _thread;
    private volatile bool _running;

    /// <summary>
    /// Raised (on the receive thread) after a datagram with face data: always for one that holds the last row of
    /// Baballonia's send order (/tongueTwistRight, so the module's Update sees the whole inference at once), otherwise
    /// at most once per 5 ms (other senders).
    /// </summary>
    public event Action? Received;

    /// <param name="clock">milliseconds (default Environment.TickCount64); tests inject a fake clock</param>
    public FaceReceiver(int port, Action<string> info, Action<string> warn, Func<long>? clock = null)
    {
        _port = port;
        _info = info;
        _warn = warn;
        _clock = clock ?? (() => Environment.TickCount64);
        _sink = new Sink(this);
        Array.Fill(_arrivals, long.MinValue);
    }

    public int Port => _port;

    /// <summary>A socket is open (between a successful <see cref="Start"/> and <see cref="CloseSocket"/>).</summary>
    public bool IsListening => Volatile.Read(ref _udp) is not null;

    /// <summary>Binds 127.0.0.1:port exclusively and starts the receive thread; false with the reason if the bind fails.</summary>
    public bool Start(out string? error)
    {
        if (_thread is not null)
        {
            throw new InvalidOperationException("FaceReceiver.Start was already called");
        }
        TestPorts.ThrowIfForbidden(_port);
        UdpClient? udp = null;
        try
        {
            udp = new UdpClient(AddressFamily.InterNetwork);
            try
            {
                udp.ExclusiveAddressUse = true;
            }
            catch (Exception e) when ((e is SocketException or PlatformNotSupportedException) && !OperatingSystem.IsWindows())
            {
                // Without SO_REUSEADDR/SO_REUSEPORT a Unix bind is exclusive anyway.
            }
            udp.Client.Bind(new IPEndPoint(IPAddress.Loopback, _port));
            udp.Client.ReceiveTimeout = 500;
            try
            {
                udp.Client.ReceiveBufferSize = 1 << 18; // Baballonia sends 45 datagrams per frame in one burst
            }
            catch (SocketException)
            {
            }
        }
        catch (Exception e)
        {
            udp?.Dispose();
            error = $"cannot bind 127.0.0.1:{_port} ({e.Message}); is the VRCFT-Babble module or another Baballonia listener running? Face tracking is off";
            return false;
        }
        Volatile.Write(ref _udp, udp);
        _running = true;
        _thread = new Thread(() => Loop(udp)) { IsBackground = true, Name = "SteamFrameFaceRx" };
        _thread.Start();
        error = null;
        return true;
    }

    /// <summary>Stops the receive thread without waiting: closes the socket, so the pending receive ends.</summary>
    public void CloseSocket()
    {
        _running = false;
        var udp = Interlocked.Exchange(ref _udp, null);
        try
        {
            udp?.Close();
        }
        catch (Exception)
        {
            // closing is best effort
        }
    }

    /// <summary>Waits up to <paramref name="milliseconds"/> for the receive thread to end.</summary>
    public bool Join(int milliseconds) => _thread?.Join(milliseconds) ?? true;

    /// <summary>Closes the socket and waits at most 100 ms for the thread.</summary>
    public void Stop()
    {
        CloseSocket();
        Join(100);
    }

    /// <summary>
    /// One received datagram (the receive thread calls this; tests call it directly with a fake clock).
    /// Returns false if the datagram was malformed (counted as rejected; nothing of it is used).
    /// </summary>
    public bool Feed(ReadOnlySpan<byte> datagram, int sourcePort = 0)
    {
        bool ok, raise = false;
        List<(bool Warn, string Text)>? logs;
        lock (_lock)
        {
            _now = _clock();
            _packets++;
            _sourcePort = sourcePort;
            _matched = 0;
            _matchedLastRow = false;
            ok = FaceOsc.TryParse(datagram, _sink);
            if (!ok)
            {
                _rejected++;
            }
            else if (_matched > 0 && (_matchedLastRow || _lastRaised is not { } last || _now - last >= 5))
            {
                // The last row ends Baballonia's 45-datagram burst: wake the module now, whatever the throttle, so it
                // writes the whole inference instead of waiting for the next wake with the rows after the first one.
                _lastRaised = _now;
                raise = true;
            }
            logs = _logs;
            _logs = null;
        }
        if (logs is not null)
        {
            foreach (var (warn, text) in logs)
            {
                (warn ? _warn : _info)(text);
            }
        }
        if (raise)
        {
            Received?.Invoke();
        }
        return ok;
    }

    public FaceSnapshot Snapshot(long now)
    {
        lock (_lock)
        {
            // The trailing second (now - 1000, now]; an arrival stamped just after `now` (the receive thread won the
            // lock) counts too.
            var from = now - RateWindowMs;
            var rate = 0;
            for (var i = 0; i < FaceMapping.Count; i++)
            {
                var n = 0;
                foreach (var t in _arrivals.AsSpan(i * RateCap, RateCap))
                {
                    if (t > from)
                    {
                        n++;
                    }
                }
                rate = Math.Max(rate, n);
            }
            return new FaceSnapshot
            {
                W = (float[])_w.Clone(),
                LastRx = _lastRx,
                RateHz = rate,
                Any = _any,
                Packets = _packets,
                Messages = _messages,
                Rejected = _rejected,
                Unknown = _unknown,
                EyeMessages = _eyeMessages,
                SourcePort = _sourcePort,
            };
        }
    }

    private void Loop(UdpClient udp)
    {
        var socket = udp.Client;
        var buffer = new byte[65536];
        EndPoint from = new IPEndPoint(IPAddress.Any, 0);
        while (_running)
        {
            int n;
            try
            {
                n = socket.ReceiveFrom(buffer, ref from);
            }
            catch (SocketException e)
            {
                if (!_running)
                {
                    break;
                }
                if (e.SocketErrorCode != SocketError.TimedOut)
                {
                    Thread.Sleep(10); // a transient error: do not spin
                }
                continue; // receive timeout (500 ms): check _running again
            }
            catch (ObjectDisposedException)
            {
                break;
            }
            Feed(buffer.AsSpan(0, n), (from as IPEndPoint)?.Port ?? 0);
        }
    }

    /// <summary>One accepted message, called by FaceOsc during <see cref="Feed"/> (under the lock).</summary>
    private void Accept(ReadOnlySpan<byte> address, float value)
    {
        var segment = address[(address.LastIndexOf((byte)'/') + 1)..];
        var i = FaceMapping.IndexOf(segment);
        if (i >= 0)
        {
            _w[i] = value;
            _messages++;
            _matched++;
            _matchedLastRow |= i == FaceMapping.Count - 1;
            _any = true;
            _lastRx = _now;
            _arrivals[i * RateCap + _nextArrival[i]] = _now;
            _nextArrival[i] = (_nextArrival[i] + 1) % RateCap;
            return;
        }
        if (address.IndexOf("Eye"u8) >= 0)
        {
            if (_eyeMessages++ == 0)
            {
                Log(true, "Baballonia sends eye addresses; turn its eye cameras off, eyes come from the Frame");
            }
            return;
        }
        if (_unknown++ < 5)
        {
            Log(false, $"ignoring OSC address {Encoding.ASCII.GetString(address)} (not one of the 45 Babble face shapes)");
        }
    }

    private void Log(bool warn, string text) => (_logs ??= new()).Add((warn, text));

    private sealed class Sink : IFloatSink
    {
        private readonly FaceReceiver _owner;

        public Sink(FaceReceiver owner) => _owner = owner;

        public void OnFloat(ReadOnlySpan<byte> address, float value) => _owner.Accept(address, value);
    }
}

/// <summary>
/// Rate and staleness gate with linear fades (VRCFT-free; spec §8.2). Live = some data, the most frequent
/// address at >= face_min_rate_hz and the last message at most face_stale_ms old. The fade factor rises by
/// dt/face_fade_in_ms while live and falls by dt/face_decay_ms otherwise (dt = ms since the last step, 0..100;
/// a 0 duration steps at once). A single burst (Baballonia's one inference of a placeholder frame) never opens it.
/// </summary>
public sealed class FaceGate
{
    private readonly int _minRateHz, _staleMs, _fadeInMs, _decayMs;
    private double _alpha;
    private long? _lastStep;

    public FaceGate(int minRateHz = 10, int staleMs = 500, int fadeInMs = 150, int decayMs = 300)
    {
        _minRateHz = minRateHz;
        _staleMs = staleMs;
        _fadeInMs = fadeInMs;
        _decayMs = decayMs;
    }

    public FaceGate(FaceSettings s) : this(s.MinRateHz, s.StaleMs, s.FadeInMs, s.DecayMs)
    {
    }

    /// <summary>Live at the last <see cref="Step"/>.</summary>
    public bool Live { get; private set; }

    /// <summary>The fade factor of the last <see cref="Step"/>, 0..1.</summary>
    public float Alpha => (float)_alpha;

    public bool IsLive(long now, FaceSnapshot s) => s.Any && s.RateHz >= _minRateHz && now - s.LastRx <= _staleMs;

    public float Step(long now, FaceSnapshot s)
    {
        Live = IsLive(now, s);
        var dt = _lastStep is { } last ? Math.Clamp(now - last, 0, 100) : 0;
        _lastStep = now;
        if (Live)
        {
            _alpha = _fadeInMs <= 0 ? 1 : Math.Min(1, _alpha + dt / (double)Math.Max(1, _fadeInMs));
        }
        else
        {
            _alpha = _decayMs <= 0 ? 0 : Math.Max(0, _alpha - dt / (double)Math.Max(1, _decayMs));
        }
        _alpha = _alpha > 1 - 1e-9 ? 1 : _alpha < 1e-9 ? 0 : _alpha; // no float dust at the ends of a fade
        return (float)_alpha;
    }

    /// <summary>
    /// One update of the face path: steps the gate and fills the (UE index, weight) pairs to write. Returns 0
    /// before the first face message (VRCFT's INVALID sentinel then keeps the shapes unapplied); afterwards
    /// always 55 (57 with smile_slant), fading to 0 when the data stops.
    /// </summary>
    public int Compose(long now, FaceSnapshot s, bool smileSlant, Span<int> idx, Span<float> val)
    {
        var alpha = Step(now, s);
        return s.Any ? FaceMapping.Fill(s.W, alpha, smileSlant, idx, val) : 0;
    }
}

/// <summary>
/// The face path's log lines (VRCFT-free; spec §8.2), driven by the module's Update with each snapshot. The stale
/// warning is logged only if the gate was live since the previous one (a face that was never shown is not relaxed) and
/// at most once per <see cref="StaleLogEveryMs"/> (a gap still going on when that time is over is reported then);
/// "face data resumed" follows only a logged warning. The 5-minute line counts every gap.
/// </summary>
public sealed class FaceMonitor
{
    public const long ReportEveryMs = 5 * 60 * 1000;
    public const long StaleLogEveryMs = 60 * 1000;
    private const long GapMs = 500;
    private readonly int _staleMs;
    private readonly Action<string> _info, _warn;
    private readonly long _reportEveryMs;
    private long? _firstSeen, _lastStep, _lastStaleLog;
    private bool _firstLogged, _staleLogged, _inGap, _shown;
    private long _windowStart, _windowMs, _liveMs, _gaps, _rejectedBase, _unknownBase;

    public FaceMonitor(int staleMs, Action<string> info, Action<string> warn, long reportEveryMs = ReportEveryMs)
    {
        _staleMs = staleMs;
        _info = info;
        _warn = warn;
        _reportEveryMs = reportEveryMs;
    }

    public void Step(long now, FaceSnapshot s, bool live)
    {
        if (!s.Any)
        {
            return;
        }
        if (_firstSeen is null)
        {
            _firstSeen = _lastStep = _windowStart = now;
            (_rejectedBase, _unknownBase) = (s.Rejected, s.Unknown);
        }
        var dt = Math.Clamp(now - _lastStep!.Value, 0, 1000);
        _lastStep = now;
        _windowMs += dt;
        _liveMs += live ? dt : 0;
        _shown |= live;

        if (!_firstLogged && now - _firstSeen.Value >= 1000)
        {
            _firstLogged = true;
            _info($"first face data from Baballonia (port {s.SourcePort}): {s.RateHz} Hz");
        }
        var age = now - s.LastRx;
        if (age > GapMs && !_inGap)
        {
            _inGap = true;
            _gaps++;
        }
        else if (age <= GapMs)
        {
            _inGap = false;
        }
        if (age > _staleMs)
        {
            if (!_staleLogged && _shown && (_lastStaleLog is not { } logged || now - logged >= StaleLogEveryMs))
            {
                _staleLogged = true;
                _shown = false;
                _lastStaleLog = now;
                _warn($"no face data for {age / 1000.0:F1} s (Baballonia stopped, tracker unplugged, or headset not worn); relaxing the face");
            }
        }
        else if (_staleLogged)
        {
            _staleLogged = false;
            _info("face data resumed");
        }
        if (now - _windowStart >= _reportEveryMs)
        {
            var livePercent = _windowMs > 0 ? 100.0 * _liveMs / _windowMs : 0.0;
            _info($"face {s.RateHz} Hz, live {livePercent:F0} %, gaps > 0.5 s: {_gaps}, rejected {s.Rejected - _rejectedBase}, unknown {s.Unknown - _unknownBase}");
            _windowStart = now;
            _windowMs = _liveMs = _gaps = 0;
            (_rejectedBase, _unknownBase) = (s.Rejected, s.Unknown);
        }
    }

    /// <summary>The Teardown line.</summary>
    public static string Summary(FaceSnapshot s) => $"face messages {s.Messages} ({s.Rejected} rejected)";
}
