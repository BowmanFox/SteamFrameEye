// SPDX-License-Identifier: MIT
using System.Text.Json;

namespace SteamFrameEyeModule;

/// <summary>
/// Face-path keys of SteamFrameEyeModule.json (VRCFT-free; spec §8.4). The eye keys stay with EyeSettings, which
/// ignores these. Every key is optional; an invalid value gives one warning and the default. Without
/// "face_source" the module behaves exactly like 0.5.3 (eyes only).
/// </summary>
public sealed class FaceSettings
{
    public const string BabbleModuleId = "360b014b-b57b-450f-8f12-9904618ff370";
    public const int DefaultFacePort = 8888;
    public const int DefaultStatusKeeperPort = 9000;
    /// <summary>VRChat's OSC ports and the eye module's SFEY port: never a face port.</summary>
    public static readonly IReadOnlySet<int> ReservedFacePorts = new HashSet<int> { 9000, 9001, 9021 };

    public bool EyeEnabled { get; set; } = true;
    /// <summary>"babble_osc" or "off" (the default when the key is absent).</summary>
    public string FaceSource { get; set; } = "off";
    public bool FaceEnabled => FaceSource == "babble_osc";
    public int FacePort { get; set; } = DefaultFacePort;
    public int MinRateHz { get; set; } = 10;
    public int StaleMs { get; set; } = 500;
    public int DecayMs { get; set; } = 300;
    public int FadeInMs { get; set; } = 150;
    public bool SmileSlant { get; set; }
    /// <summary>"off", "face" or "all" (§8.5).</summary>
    public string StatusKeeper { get; set; } = "off";
    public int StatusKeeperPort { get; set; } = DefaultStatusKeeperPort;

    /// <param name="path">SteamFrameEyeModule.json; a missing file gives the defaults</param>
    /// <param name="warn">receives one line per invalid value</param>
    /// <param name="env">environment lookup (tests); SFFACE_PORT overrides face_port</param>
    public static FaceSettings Load(string path, Action<string> warn, Func<string, string?>? env = null)
    {
        env ??= Environment.GetEnvironmentVariable;
        var s = new FaceSettings();
        if (File.Exists(path))
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(path));
                if (doc.RootElement.ValueKind == JsonValueKind.Object)
                {
                    s.Read(doc.RootElement, warn);
                }
                else
                {
                    warn($"{Path.GetFileName(path)} is not a JSON object; face_source is \"off\"");
                }
            }
            catch (Exception e)
            {
                warn($"could not read the face settings in {Path.GetFileName(path)}: {e.Message}; face_source is \"off\"");
                s = new FaceSettings();
            }
        }
        var envPort = env("SFFACE_PORT");
        if (!string.IsNullOrEmpty(envPort))
        {
            if (int.TryParse(envPort, out var p) && ValidFacePort(p))
            {
                s.FacePort = p;
            }
            else
            {
                warn($"SFFACE_PORT={envPort} is not a valid face port (1024-65535 except 9000, 9001, 9021); using {s.FacePort}");
            }
        }
        return s;
    }

    /// <summary>1024-65535 except VRChat's OSC ports 9000/9001 and the SFEY port 9021.</summary>
    public static bool ValidFacePort(int port) => port is >= 1024 and <= 65535 && !ReservedFacePorts.Contains(port);

    /// <summary>
    /// True if the VRCFT-Babble module is installed in <paramref name="configDir"/> (the module's ConfigDir, so a
    /// test with SFEYE_CONFIG_DIR never looks at the real CustomLibs): CustomLibs\360b014b-...\module.json exists.
    /// It listens on 8888 too and would claim expressions first.
    /// </summary>
    public static bool BabbleModuleInstalled(string configDir) =>
        File.Exists(Path.Combine(configDir, "CustomLibs", BabbleModuleId, "module.json"));

    private void Read(JsonElement root, Action<string> warn)
    {
        EyeEnabled = Bool(root, "eye_enabled", EyeEnabled, warn);
        FaceSource = Choice(root, "face_source", FaceSource, warn, "babble_osc", "off");
        FacePort = Int(root, "face_port", FacePort, warn, ValidFacePort, "1024-65535 except 9000, 9001, 9021");
        MinRateHz = Int(root, "face_min_rate_hz", MinRateHz, warn, v => v is >= 1 and <= 60, "1-60");
        StaleMs = Int(root, "face_stale_ms", StaleMs, warn, v => v is >= 100 and <= 5000, "100-5000");
        DecayMs = Int(root, "face_decay_ms", DecayMs, warn, v => v is >= 0 and <= 5000, "0-5000");
        FadeInMs = Int(root, "face_fade_in_ms", FadeInMs, warn, v => v is >= 0 and <= 2000, "0-2000");
        SmileSlant = Bool(root, "smile_slant", SmileSlant, warn);
        StatusKeeper = Choice(root, "status_keeper", StatusKeeper, warn, "off", "face", "all");
        StatusKeeperPort = Int(root, "status_keeper_port", StatusKeeperPort, warn, v => v is >= 1024 and <= 65535, "1024-65535");
    }

    private static bool Bool(JsonElement root, string key, bool fallback, Action<string> warn)
    {
        if (!root.TryGetProperty(key, out var p))
        {
            return fallback;
        }
        if (p.ValueKind is JsonValueKind.True or JsonValueKind.False)
        {
            return p.GetBoolean();
        }
        warn($"{key} must be true or false, not {p.GetRawText()}; using {(fallback ? "true" : "false")}");
        return fallback;
    }

    private static int Int(JsonElement root, string key, int fallback, Action<string> warn, Func<int, bool> valid, string range)
    {
        if (!root.TryGetProperty(key, out var p))
        {
            return fallback;
        }
        if (p.ValueKind == JsonValueKind.Number && p.TryGetInt32(out var v) && valid(v))
        {
            return v;
        }
        warn($"{key} {p.GetRawText()} is not valid ({range}); using {fallback}");
        return fallback;
    }

    private static string Choice(JsonElement root, string key, string fallback, Action<string> warn, params string[] allowed)
    {
        if (!root.TryGetProperty(key, out var p))
        {
            return fallback;
        }
        if (p.ValueKind == JsonValueKind.String)
        {
            var v = (p.GetString() ?? "").Trim().ToLowerInvariant();
            if (Array.IndexOf(allowed, v) >= 0)
            {
                return v;
            }
        }
        warn($"{key} {p.GetRawText()} is not one of \"{string.Join("\", \"", allowed)}\"; using \"{fallback}\"");
        return fallback;
    }
}
