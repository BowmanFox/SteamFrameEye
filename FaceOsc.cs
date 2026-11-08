// SPDX-License-Identifier: MIT
using System.Buffers.Binary;
using System.Text;

namespace SteamFrameEyeModule;

/// <summary>Receives the messages of a datagram that <see cref="FaceOsc.TryParse"/> accepted.</summary>
public interface IFloatSink
{
    /// <summary>One "<c>,f</c>" message: its address (ASCII bytes without the terminating NUL) and its finite value.</summary>
    void OnFloat(ReadOnlySpan<byte> address, float value);
}

/// <summary>
/// The OSC subset Baballonia sends (VRCFT-free): one message per datagram, or a bundle
/// (<c>#bundle\0</c>, an 8-byte time tag, then elements of [int32 big-endian size][content]) nested at most
/// <c>maxDepth</c> levels. A message is an address (a NUL-terminated string starting with '/', padded with NULs
/// to a multiple of 4), the type tag string exactly "<c>,f</c>" and one big-endian float32 that must be finite.
/// A datagram is accepted only as a whole: if any part is malformed, the sink receives nothing.
/// </summary>
public static class FaceOsc
{
    private static ReadOnlySpan<byte> BundleTag => "#bundle\0"u8;

    /// <summary>Parses one datagram; false (and nothing delivered) if any part of it is malformed. Never throws.</summary>
    public static bool TryParse(ReadOnlySpan<byte> datagram, IFloatSink sink, int maxDepth = 2)
    {
        bool valid;
        try
        {
            valid = Walk(datagram, null, 0, maxDepth); // first pass: validate only, deliver nothing
        }
        catch (Exception)
        {
            valid = false; // defensive: malformed input must never escape as an exception
        }
        if (valid)
        {
            Walk(datagram, sink, 0, maxDepth);
        }
        return valid;
    }

    /// <summary>An OSC message with one float argument: address, "<c>,f</c>", big-endian float32.</summary>
    public static byte[] Float(string address, float value)
    {
        var head = Header(address, ",f", 4);
        BinaryPrimitives.WriteSingleBigEndian(head.AsSpan(head.Length - 4), value);
        return head;
    }

    /// <summary>An OSC message with one boolean: address and the type tag "<c>,T</c>" or "<c>,F</c>" (no argument bytes).</summary>
    public static byte[] Bool(string address, bool v) => Header(address, v ? ",T" : ",F", 0);

    private static byte[] Header(string address, string typeTags, int argumentBytes)
    {
        if (string.IsNullOrEmpty(address) || address[0] != '/' || !IsAddressText(address))
        {
            throw new ArgumentException($"not an OSC address: {address}", nameof(address));
        }
        var a = Padded(Encoding.ASCII.GetByteCount(address));
        var t = Padded(typeTags.Length);
        var bytes = new byte[a + t + argumentBytes];
        Encoding.ASCII.GetBytes(address.AsSpan(), bytes.AsSpan());
        Encoding.ASCII.GetBytes(typeTags.AsSpan(), bytes.AsSpan(a));
        return bytes;
    }

    /// <summary>Length of an OSC string field: the text, its NUL and the NUL padding to a multiple of 4.</summary>
    private static int Padded(int textLength) => (textLength + 4) & ~3;

    private static bool IsAddressText(string s)
    {
        foreach (var c in s)
        {
            if (c < 0x21 || c > 0x7e)
            {
                return false;
            }
        }
        return true;
    }

    private static bool Walk(ReadOnlySpan<byte> data, IFloatSink? sink, int depth, int maxDepth)
    {
        if (data.Length >= BundleTag.Length && data[..BundleTag.Length].SequenceEqual(BundleTag))
        {
            return WalkBundle(data, sink, depth + 1, maxDepth);
        }
        return WalkMessage(data, sink);
    }

    private static bool WalkBundle(ReadOnlySpan<byte> data, IFloatSink? sink, int level, int maxDepth)
    {
        if (level > maxDepth || data.Length < 16)
        {
            return false; // nested too deep, or no room for the time tag
        }
        var rest = data[16..];
        while (rest.Length > 0)
        {
            if (rest.Length < 4)
            {
                return false;
            }
            var size = BinaryPrimitives.ReadInt32BigEndian(rest);
            if (size <= 0 || size % 4 != 0 || size > rest.Length - 4)
            {
                return false;
            }
            if (!Walk(rest.Slice(4, size), sink, level, maxDepth))
            {
                return false;
            }
            rest = rest[(4 + size)..];
        }
        return true;
    }

    private static bool WalkMessage(ReadOnlySpan<byte> data, IFloatSink? sink)
    {
        if (data.Length == 0 || data[0] != (byte)'/')
        {
            return false;
        }
        var nul = data.IndexOf((byte)0);
        if (nul < 1)
        {
            return false; // no terminator: truncated
        }
        var address = data[..nul];
        foreach (var b in address)
        {
            if (b < 0x21 || b > 0x7e)
            {
                return false;
            }
        }
        var end = Padded(nul);
        if (end > data.Length || data[nul..end].IndexOfAnyExcept((byte)0) >= 0)
        {
            return false; // truncated, or bad padding after the address
        }
        // Exactly ",f\0\0" and four value bytes: no comma, other types (",i") or more arguments (",ff") are rejected.
        var rest = data[end..];
        if (rest.Length != 8 || rest[0] != (byte)',' || rest[1] != (byte)'f' || rest[2] != 0 || rest[3] != 0)
        {
            return false;
        }
        var value = BinaryPrimitives.ReadSingleBigEndian(rest[4..]);
        if (!float.IsFinite(value))
        {
            return false;
        }
        sink?.OnFloat(address, value);
        return true;
    }
}
