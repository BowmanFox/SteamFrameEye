// SPDX-License-Identifier: MIT
// Stand-ins for the VRCFaceTracking 5.4.5 SDK/Core members SteamFrameEyeModule.cs uses, with the same names,
// namespaces and member kinds (public fields where VRCFT has fields, properties where it has properties; checked
// against the metadata of lib/vrcft-5.4.5, which 6.0 keeps). Only what the module touches; no VRCFT code.
// StandIns.csproj builds them as their own assembly, VRCFaceTracking.SDK 6.0.0.0 like VRCFaceTracking 6.0's, so the
// module detects a claim-reporting host here; the checks set SteamFrameEyeModule.HostReportsClaimsOverride = false
// where they need a 5.4.x host.
using Microsoft.Extensions.Logging;
using VRCFaceTracking.Core.Library;
using VRCFaceTracking.Core.Params.Data;
using VRCFaceTracking.Core.Params.Expressions;

namespace VRCFaceTracking.Core.Library
{
    public enum ModuleState { Uninitialized = -1, Idle = 0, Active = 1 }
}

namespace VRCFaceTracking.Core.Types
{
    public struct Vector2
    {
        public float x, y;
    }
}

namespace VRCFaceTracking.Core.Params.Data
{
    using VRCFaceTracking.Core.Types;

    public struct UnifiedSingleEyeData
    {
        public Vector2 Gaze;
        public float PupilDiameter_MM;
        public float Openness;
    }

    public class UnifiedEyeData
    {
        public UnifiedSingleEyeData Left, Right;
        public float _maxDilation, _minDilation;
    }

    public struct UnifiedExpressionShape
    {
        public float Weight;
    }

    public struct UnifiedHeadData
    {
        public float HeadYaw, HeadPitch, HeadRoll, HeadPosX, HeadPosY, HeadPosZ;
    }

    public class UnifiedTrackingData
    {
        public UnifiedEyeData Eye = new();
        public UnifiedExpressionShape[] Shapes = new UnifiedExpressionShape[(int)UnifiedExpressions.Max + 1];
        public UnifiedHeadData Head;
    }
}

namespace VRCFaceTracking.Core.Params.Expressions
{
    /// <summary>UnifiedExpressions 0..87 and Max (brief §e.6, identical in VRCFT 5.4.5 and 6.0).</summary>
    public enum UnifiedExpressions
    {
        EyeSquintRight, EyeSquintLeft, EyeWideRight, EyeWideLeft, BrowPinchRight, BrowPinchLeft,
        BrowLowererRight, BrowLowererLeft, BrowInnerUpRight, BrowInnerUpLeft, BrowOuterUpRight, BrowOuterUpLeft,
        NasalDilationRight, NasalDilationLeft, NasalConstrictRight, NasalConstrictLeft, CheekSquintRight, CheekSquintLeft,
        CheekPuffRight, CheekPuffLeft, CheekSuckRight, CheekSuckLeft, JawOpen, JawRight,
        JawLeft, JawForward, JawBackward, JawClench, JawMandibleRaise, MouthClosed,
        LipSuckUpperRight, LipSuckUpperLeft, LipSuckLowerRight, LipSuckLowerLeft, LipSuckCornerRight, LipSuckCornerLeft,
        LipFunnelUpperRight, LipFunnelUpperLeft, LipFunnelLowerRight, LipFunnelLowerLeft, LipPuckerUpperRight, LipPuckerUpperLeft,
        LipPuckerLowerRight, LipPuckerLowerLeft, MouthUpperUpRight, MouthUpperUpLeft, MouthUpperDeepenRight, MouthUpperDeepenLeft,
        NoseSneerRight, NoseSneerLeft, MouthLowerDownRight, MouthLowerDownLeft, MouthUpperRight, MouthUpperLeft,
        MouthLowerRight, MouthLowerLeft, MouthCornerPullRight, MouthCornerPullLeft, MouthCornerSlantRight, MouthCornerSlantLeft,
        MouthFrownRight, MouthFrownLeft, MouthStretchRight, MouthStretchLeft, MouthDimpleRight, MouthDimpleLeft,
        MouthRaiserUpper, MouthRaiserLower, MouthPressRight, MouthPressLeft, MouthTightenerRight, MouthTightenerLeft,
        TongueOut, TongueUp, TongueDown, TongueRight, TongueLeft, TongueRoll,
        TongueBendDown, TongueCurlUp, TongueSquish, TongueFlat, TongueTwistRight, TongueTwistLeft,
        SoftPalateClose, ThroatSwallow, NeckFlexRight, NeckFlexLeft, Max,
    }
}

namespace VRCFaceTracking
{
    public struct ModuleMetadata
    {
        public List<Stream> StaticImages { get; set; }
        public string Name { get; set; }
        public bool Active { get; set; }
        public bool UsingEye { get; set; }
        public bool UsingExpression { get; set; }
    }

    public abstract class ExtTrackingModule
    {
        public ModuleState Status = ModuleState.Uninitialized;
        public ILogger Logger = null!;
        public ModuleMetadata ModuleInformation;

        public virtual (bool SupportsEye, bool SupportsExpression) Supported => (false, false);

        public abstract (bool eyeSuccess, bool expressionSuccess) Initialize(bool eyeAvailable, bool expressionAvailable);

        public abstract void Update();

        public abstract void Teardown();
    }

    public class UnifiedTracking
    {
        public static UnifiedTrackingData Data = new();
    }
}
