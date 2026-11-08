// SPDX-License-Identifier: MIT
namespace SteamFrameEyeModule;

/// <summary>
/// Port guard for tests (VRCFT-free). Production leaves <see cref="Forbidden"/> null, so the guard does nothing.
/// tools/FaceChecks and tools/ModuleHarness set it to the ports of software that may run live on the developer's
/// PC (Babble OSC 8888/8889, VRChat OSC 9000/9001, Steam Link 9015, Baballonia DFR 9020, FrameEye SFEY 9021) as
/// their first statement. The face receiver, the eye receiver and the status keeper's default sender call
/// <see cref="ThrowIfForbidden"/> before any bind or send, so a test can never reach one of those ports.
/// </summary>
public static class TestPorts
{
    /// <summary>Ports no bind or send may use; null in production.</summary>
    public static IReadOnlySet<int>? Forbidden { get; set; }

    /// <summary>Throws if <paramref name="port"/> is forbidden; call it before creating or using a socket.</summary>
    public static void ThrowIfForbidden(int port)
    {
        if (Forbidden is { } forbidden && forbidden.Contains(port))
        {
            throw new InvalidOperationException($"test guard: port {port} is forbidden in tests");
        }
    }
}
