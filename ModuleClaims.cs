// SPDX-License-Identifier: MIT
namespace SteamFrameEyeModule;

/// <summary>
/// Which capabilities the module starts and claims (VRCFT-free). VRCFaceTracking keeps the first claim of each
/// capability for the whole run, so the module never claims what it was not offered and never claims a path
/// that did not start (G1 §6.2): Initialize returns (eyeAvailable &amp;&amp; eyeOk, expressionAvailable &amp;&amp; faceOk).
/// </summary>
public static class ModuleClaims
{
    /// <param name="EyeAvailable">VRCFT offered eye tracking (no other module claimed it first)</param>
    /// <param name="ExpressionAvailable">VRCFT offered expressions</param>
    /// <param name="EyeEnabled">settings: eye_enabled</param>
    /// <param name="FaceEnabled">settings: face_source is "babble_osc"</param>
    /// <param name="EnumOk">every face mapping target matches its UnifiedExpressions name</param>
    public readonly record struct Inputs(bool EyeAvailable, bool ExpressionAvailable, bool EyeEnabled, bool FaceEnabled, bool EnumOk);

    public static bool ShouldStartEye(Inputs i) => i.EyeAvailable && i.EyeEnabled;

    public static bool ShouldStartFace(Inputs i) => i.ExpressionAvailable && i.FaceEnabled && i.EnumOk;

    public static (bool Eye, bool Expression) Decide(Inputs i, bool eyeStarted, bool faceStarted)
        => (ShouldStartEye(i) && eyeStarted, ShouldStartFace(i) && faceStarted);
}

/// <summary>
/// The claims a claim-reporting host (VRCFaceTracking 6.0) says it gave this module, judged against the claims
/// Initialize returned (VRCFT-free). A claim the module made that the host does not report is lost: another module
/// won it, and the host ignores this module's data for it. Each lost claim is warned about once per run; when the
/// module holds none of its claims and none was warned about yet, one warning names them all. The module asks only on
/// such a host while its Status is Active, from 1 s after Initialize on and never at its first Update (6.0 sends
/// Active with both claims false first and the claims it gave right after).
/// </summary>
public sealed class LostClaims
{
    private const string Hint = " (Steam Link module? VRCFT-Babble?), so VRCFaceTracking ignores ";
    private const string Fix = "; remove the other module and restart VRCFaceTracking";
    private bool _eyeWarned, _expressionWarned;

    /// <param name="claimedEye">Initialize returned eyeSuccess</param>
    /// <param name="claimedExpression">Initialize returned expressionSuccess</param>
    /// <param name="usingEye">the host's report, ModuleInformation.UsingEye</param>
    /// <param name="usingExpression">the host's report, ModuleInformation.UsingExpression</param>
    /// <returns>the warning (without "SteamFrameEye: ") for a claim lost and not warned about yet, otherwise null</returns>
    public string? Check(bool claimedEye, bool claimedExpression, bool usingEye, bool usingExpression)
    {
        var eye = claimedEye && !usingEye && !_eyeWarned;
        var expression = claimedExpression && !usingExpression && !_expressionWarned;
        if (!eye && !expression)
        {
            return null;
        }
        var first = !_eyeWarned && !_expressionWarned;
        var holdsNone = (!claimedEye || !usingEye) && (!claimedExpression || !usingExpression);
        _eyeWarned |= eye;
        _expressionWarned |= expression;
        if (first && holdsNone)
        {
            var claims = eye && expression ? "eye and expression claims" : eye ? "eye claim" : "expression claim";
            return $"another module won this module's {claims}{Hint}its data{Fix}";
        }
        var lost = eye ? "eye" : "expression"; // only one is new here: two new losses at once are the first, holding none
        return $"another module won this module's {lost} claim{Hint}this module's {lost} data{Fix}";
    }
}
