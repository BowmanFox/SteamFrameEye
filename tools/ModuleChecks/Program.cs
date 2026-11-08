// SPDX-License-Identifier: MIT
// Runs the real VRCFaceTracking module (spec §8.1, §11.3 "tools/ModuleChecks"): SteamFrameEyeModule.cs with
// EyeProcessing.cs and the VRCFT-free face files, compiled against stand-ins for the VRCFT types (StandIns/, their own
// assembly VRCFaceTracking.SDK 6.0.0.0 as VRCFaceTracking 6.0 loads it, so this host reports the claims unless a check
// sets HostReportsClaimsOverride), driven through Initialize, Update and Teardown in this process with
// UnifiedTracking.Data read back. It covers the host detection, the §8.1 rows, the face path's write set, the wake on
// the last row of an inference, the status keeper wiring and the VRCFT 6.0 claim reports, and the Teardown order and
// time. tools/FaceChecks tests the VRCFT-free files alone.
//
// Safety: TestPorts is armed first; SFEYE_CONFIG_DIR is set in this process to a new temporary folder (never
// %APPDATA%\VRCFaceTracking) and SFEYE_PORT/SFFACE_PORT are cleared; sockets use free 127.0.0.1 ports only; the
// status keeper only gets an injected sender. The eye receiver binds 0.0.0.0, so the rows that start it run only with
// MODULECHECKS_ALLOW_WILDCARD=1 (CI); everywhere else the eye port stays the forbidden 9021, where an eye start the
// checks did not expect throws in TestPorts before any socket exists.
//
// Exit codes: 0 every check passed (SKIP lines are allowed), 1 a check failed.
using System.Reflection;
using SteamFrameEyeModule;
using VRCFaceTracking;
using Module = SteamFrameEyeModule.SteamFrameEyeModule;

TestPorts.Forbidden = new HashSet<int> { 8888, 8889, 9000, 9001, 9015, 9020, 9021, 9023 };

var wildcard = Environment.GetEnvironmentVariable("MODULECHECKS_ALLOW_WILDCARD") == "1";
var config = Directory.CreateTempSubdirectory("modulechecks-");
Environment.SetEnvironmentVariable("SFEYE_CONFIG_DIR", config.FullName);
Environment.SetEnvironmentVariable("SFEYE_PORT", null);
Environment.SetEnvironmentVariable("SFFACE_PORT", null);
var rig = new Rig(config.FullName, wildcard);
Console.WriteLine($"ModuleChecks: config folder {config.FullName}; eye rows {(wildcard ? "on (MODULECHECKS_ALLOW_WILDCARD=1)" : "skipped")}");
try
{
    if (Basics.ConfigFolder(rig))
    {
        Basics.HostDetection(rig);
        ClaimRows.Run(rig);
        FacePath.WriteSet(rig);
        FacePath.WakeOnLastRow(rig);
        KeeperRuns.Run(rig);
        TeardownRuns.FaceOnly(rig);
        TeardownRuns.EyeAndFace(rig);
    }
}
catch (Exception e)
{
    rig.Check(false, $"unexpected exception {e}");
}
finally
{
    Module.ExpressionNameOverride = null;
    Module.HostReportsClaimsOverride = null;
    Module.StatusKeeperSendOverride = null;
    try
    {
        config.Delete(true);
    }
    catch (Exception e) when (e is IOException or UnauthorizedAccessException)
    {
        Console.WriteLine($"note: could not remove {config.FullName}: {e.Message}");
    }
}
Console.WriteLine(rig.Failures == 0
    ? $"All {rig.Count} module checks passed ({rig.Skips} skipped)."
    : $"{rig.Failures} of {rig.Count} module checks FAILED ({rig.Skips} skipped).");
return rig.Failures == 0 ? 0 : 1;

static class Basics
{
    /// <summary>Before any module exists: the module resolves its config folder to the temporary one.</summary>
    public static bool ConfigFolder(Rig rig)
    {
        var real = Path.GetFullPath(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "VRCFaceTracking"));
        var used = typeof(Module).GetProperty("ConfigDir", BindingFlags.NonPublic | BindingFlags.Static)?.GetValue(null) as string;
        var ok = used is not null && Path.GetFullPath(used) == Path.GetFullPath(rig.ConfigDir)
                 && !string.Equals(Path.GetFullPath(used).TrimEnd(Path.DirectorySeparatorChar), real.TrimEnd(Path.DirectorySeparatorChar), StringComparison.OrdinalIgnoreCase)
                 && !Directory.Exists(Path.Combine(used, "CustomLibs"));
        rig.Check(ok, $"the module's config folder is the new temporary {used}, never {real}");
        return ok;
    }

    /// <summary>
    /// module-4: a claim-reporting host is told apart by the VRCFaceTracking.SDK it loaded (6.0.0.0 vs 5.4.5.0). The
    /// stand-ins are their own assembly, VRCFaceTracking.SDK 6.0.0.0, and the module's code is in this program's
    /// assembly (1.0.0.0), so a module that asked its own assembly, or the entry assembly, would not detect this host.
    /// </summary>
    public static void HostDetection(Rig rig)
    {
        rig.Check(!Module.ReportsClaims(new Version(5, 4, 5, 0)) && Module.ReportsClaims(new Version(6, 0, 0, 0))
                  && Module.ReportsClaims(new Version(7, 1)) && !Module.ReportsClaims(null),
            "host detection: VRCFaceTracking.SDK 5.4.5.0 does not report the claims, 6.0.0.0 and later do, an unknown version does not");
        var vendored = Rig.RepoFile(Path.Combine("lib", "vrcft-5.4.5", "VRCFaceTracking.SDK.dll"));
        var version = vendored is null ? null : AssemblyName.GetAssemblyName(vendored).Version; // metadata only, nothing loaded
        rig.Check(version == new Version(5, 4, 5, 0) && !Module.ReportsClaims(version),
            $"the vendored SDK the module is built against is {version?.ToString() ?? "missing"}, a 5.4.x host's, which does not report the claims");
        var sdk = typeof(ExtTrackingModule).Assembly;
        var own = typeof(Module).Assembly;
        var entry = Assembly.GetEntryAssembly();
        rig.Check(sdk != own && sdk != entry && sdk.GetName().Name == "VRCFaceTracking.SDK" && sdk.GetName().Version == new Version(6, 0, 0, 0)
                  && !Module.ReportsClaims(own.GetName().Version) && !Module.ReportsClaims(entry?.GetName().Version),
            $"ExtTrackingModule comes from its own assembly, {sdk.GetName().Name} {sdk.GetName().Version} (the stand-ins, as VRCFaceTracking 6.0 " +
            $"loads its SDK), not from the module's {own.GetName().Name} {own.GetName().Version}, whose version is not a claim-reporting host's");
        rig.Check(Module.HostReportsClaimsOverride is null && Module.HostReportsClaims == Module.ReportsClaims(sdk.GetName().Version) && Module.HostReportsClaims,
            $"without the override the module follows the loaded ExtTrackingModule's assembly ({sdk.GetName().Name} {sdk.GetName().Version}) " +
            $"and detects a claim-reporting host; its own assembly's version ({own.GetName().Version}) would not");
    }
}
