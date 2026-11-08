// SPDX-License-Identifier: MIT
// Deterministic checks of the module's VRCFT-free face code (spec §8, §11.3 "tools/FaceChecks"): OSC parser,
// Babble45 -> UnifiedExpressions mapping, rate gate and fades, log lines, settings, ModuleClaims and LostClaims,
// receiver and status keeper. SteamFrameEyeModule.cs itself is not compiled here; tools/ModuleChecks runs it. No
// VRCFaceTracking, headset or Baballonia needed. Sockets are used only on free loopback ports; the status keeper
// only with an injected sender. This file arms TestPorts, checks the guard and runs the sections in order:
// OscChecks.cs, MappingChecks.cs, GateChecks.cs, LogChecks.cs, SettingsChecks.cs, ClaimChecks.cs, ReceiverChecks.cs
// and KeeperChecks.cs; Checks.cs has the PASS/FAIL bookkeeping and the helpers.
// Exit code 0 = every check passed.
using SteamFrameEyeModule;
using static Checks;

TestPorts.Forbidden = new HashSet<int> { 8888, 8889, 9000, 9001, 9015, 9020, 9021 };

var guardMessage = new Func<int, string>(p => $"test guard: port {p} is forbidden in tests");

// ---------------------------------------------------------------- TestPorts
var guardArmed = TestPorts.Forbidden.SetEquals(new[] { 8888, 8889, 9000, 9001, 9015, 9020, 9021 })
                 && Throws<InvalidOperationException>(() => TestPorts.ThrowIfForbidden(8888), e => e.Message == guardMessage(8888));
Check(TestPorts.Forbidden.SetEquals(new[] { 8888, 8889, 9000, 9001, 9015, 9020, 9021 }),
    "TestPorts is armed with 8888, 8889, 9000, 9001, 9015, 9020, 9021 before anything else runs");
Check(Throws<InvalidOperationException>(() => TestPorts.ThrowIfForbidden(8888), e => e.Message == guardMessage(8888)),
    "TestPorts.ThrowIfForbidden(8888) throws \"test guard: port 8888 is forbidden in tests\" (a pure check, no socket)");
Check(!Throws<Exception>(() => TestPorts.ThrowIfForbidden(Util.FreeUdpPort())), "a free ephemeral port passes the guard");
{
    // First prove that Start consults the guard, on a free port forbidden only for this check (it must stay
    // unbound); Start on 8888 is tried only after that proof and with the guard armed, so a receiver that lost its
    // guard call (or a broken guard) can never bind a port of the software running live on this PC.
    var probePort = Util.FreeUdpPort();
    var armed = TestPorts.Forbidden!;
    bool consultsGuard;
    try
    {
        TestPorts.Forbidden = new HashSet<int>(armed) { probePort };
        var probe = new FaceReceiver(probePort, NoLog, NoLog);
        consultsGuard = Throws<InvalidOperationException>(() => probe.Start(out _), e => e.Message == guardMessage(probePort))
                        && !probe.IsListening;
        probe.Stop();
    }
    finally
    {
        TestPorts.Forbidden = armed;
    }
    consultsGuard &= Util.PortFree(probePort);
    Check(consultsGuard, $"FaceReceiver.Start consults the guard before any socket exists (free port {probePort}, forbidden only for this check, stays unbound)");
    var guarded = new FaceReceiver(8888, NoLog, NoLog);
    Check(guardArmed && consultsGuard
          && Throws<InvalidOperationException>(() => guarded.Start(out _), e => e.Message == guardMessage(8888)) && !guarded.IsListening,
        "FaceReceiver.Start on 8888 is refused by the guard before any socket exists (tried only after the proofs above)");
    Check(!Throws<Exception>(() => _ = new StatusKeeper(9000, "face", () => 0, _ => { })),
        "a status keeper with an injected sender (the only kind built here) uses no socket, whatever its port");
}

OscChecks.Run();
MappingChecks.Run();
GateChecks.Run();
LogChecks.Run();
SettingsChecks.Run();
ClaimChecks.Run();
ClaimChecks.Lost();
ReceiverChecks.Loopback();
ReceiverChecks.WakePattern();
KeeperChecks.Run();

Console.WriteLine(Failures == 0 ? $"All {Total} face checks passed." : $"{Failures} of {Total} face checks FAILED.");
return Failures == 0 ? 0 : 1;
