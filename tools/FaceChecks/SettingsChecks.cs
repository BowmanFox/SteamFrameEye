// SPDX-License-Identifier: MIT
// tools/FaceChecks, settings: FaceSettings defaults, every key, invalid values, forbidden ports, SFFACE_PORT and
// BabbleModuleInstalled on a temporary CustomLibs tree.
using SteamFrameEyeModule;
using static Checks;

static class SettingsChecks
{
    public static void Run()
    {
        var dir = Directory.CreateTempSubdirectory("facechecks-");
        try
        {
            var path = Path.Combine(dir.FullName, "SteamFrameEyeModule.json");
            var warnings = new List<string>();
            FaceSettings Load(string? json, Func<string, string?>? env = null)
            {
                warnings.Clear();
                if (json is null) File.Delete(path); else File.WriteAllText(path, json);
                return FaceSettings.Load(path, warnings.Add, env ?? (_ => null));
            }
            var d = Load(null);
            Check(d is { EyeEnabled: true, FaceSource: "off", FaceEnabled: false, FacePort: 8888, MinRateHz: 10, StaleMs: 500, DecayMs: 300,
                    FadeInMs: 150, SmileSlant: false, StatusKeeper: "off", StatusKeeperPort: 9000 } && warnings.Count == 0,
                "no settings file: the §8.4 defaults (face_source off), no warning");
            d = Load("{\"port\": 9021, \"gaze\": \"per_eye\", \"calibration\": \"auto\"}");
            Check(d is { FaceSource: "off", FaceEnabled: false, EyeEnabled: true, FacePort: 8888 } && warnings.Count == 0,
                "a 0.5.3 settings file (eye keys only): face_source is off when absent, eye keys are ignored here");
            d = Load("{\"eye_enabled\": false, \"face_source\": \"Babble_OSC\", \"face_port\": 18888, \"face_min_rate_hz\": 20, \"face_stale_ms\": 800," +
                     " \"face_decay_ms\": 0, \"face_fade_in_ms\": 2000, \"smile_slant\": true, \"status_keeper\": \"ALL\", \"status_keeper_port\": 19000}");
            Check(d is { EyeEnabled: false, FaceSource: "babble_osc", FaceEnabled: true, FacePort: 18888, MinRateHz: 20, StaleMs: 800, DecayMs: 0,
                    FadeInMs: 2000, SmileSlant: true, StatusKeeper: "all", StatusKeeperPort: 19000 } && warnings.Count == 0,
                "every key is read (choices case-insensitive, limits inclusive)");
            var invalid = new (string Json, Func<FaceSettings, bool> IsDefault)[]
            {
                ("{\"face_source\": \"on\"}", s => s.FaceSource == "off"),
                ("{\"face_source\": true}", s => s.FaceSource == "off"),
                ("{\"face_port\": 80}", s => s.FacePort == 8888),
                ("{\"face_port\": 70000}", s => s.FacePort == 8888),
                ("{\"face_port\": \"18888\"}", s => s.FacePort == 8888),
                ("{\"face_port\": 18888.5}", s => s.FacePort == 8888),
                ("{\"face_min_rate_hz\": 0}", s => s.MinRateHz == 10),
                ("{\"face_min_rate_hz\": 61}", s => s.MinRateHz == 10),
                ("{\"face_stale_ms\": 99}", s => s.StaleMs == 500),
                ("{\"face_stale_ms\": 5001}", s => s.StaleMs == 500),
                ("{\"face_decay_ms\": -1}", s => s.DecayMs == 300),
                ("{\"face_decay_ms\": 5001}", s => s.DecayMs == 300),
                ("{\"face_fade_in_ms\": 2001}", s => s.FadeInMs == 150),
                ("{\"smile_slant\": \"yes\"}", s => !s.SmileSlant),
                ("{\"eye_enabled\": 0}", s => s.EyeEnabled),
                ("{\"status_keeper\": \"both\"}", s => s.StatusKeeper == "off"),
                ("{\"status_keeper_port\": 1023}", s => s.StatusKeeperPort == 9000),
                ("{\"status_keeper_port\": 65536}", s => s.StatusKeeperPort == 9000),
            };
            foreach (var (json, isDefault) in invalid)
            {
                var s = Load(json);
                Check(isDefault(s) && warnings.Count == 1, $"invalid {json} -> default and one warning ({(warnings.Count > 0 ? warnings[0] : "none")})");
            }
            foreach (var port in new[] { 9000, 9001, 9021 })
            {
                var s = Load($"{{\"face_source\": \"babble_osc\", \"face_port\": {port}}}");
                Check(s.FacePort == 8888 && s.FaceEnabled && warnings.Count == 1, $"face_port {port} (VRChat OSC / SFEY) is refused: 8888 and one warning");
            }
            d = Load("{\"face_port\": 18888}", k => k == "SFFACE_PORT" ? "18999" : null);
            Check(d.FacePort == 18999 && warnings.Count == 0, "SFFACE_PORT overrides face_port");
            foreach (var bad in new[] { "9001", "80", "abc" })
            {
                d = Load("{\"face_port\": 18888}", k => k == "SFFACE_PORT" ? bad : null);
                Check(d.FacePort == 18888 && warnings.Count == 1, $"SFFACE_PORT={bad} is refused with one warning; face_port stays");
            }
            var previous = Environment.GetEnvironmentVariable("SFFACE_PORT");
            Environment.SetEnvironmentVariable("SFFACE_PORT", "18777");
            try
            {
                File.WriteAllText(path, "{}");
                Check(FaceSettings.Load(path, _ => { }).FacePort == 18777, "SFFACE_PORT is read from the process environment by default");
            }
            finally
            {
                Environment.SetEnvironmentVariable("SFFACE_PORT", previous);
            }
            d = Load("{\"face_source\": \"babble_osc\",");
            Check(d.FaceSource == "off" && warnings.Count == 1, "invalid JSON: defaults (face off) and one warning");
            d = Load("[1, 2]");
            Check(d.FaceSource == "off" && warnings.Count == 1, "a JSON array: defaults and one warning");

            var libs = Path.Combine(dir.FullName, "CustomLibs");
            Check(!FaceSettings.BabbleModuleInstalled(dir.FullName), "BabbleModuleInstalled: false without CustomLibs");
            Directory.CreateDirectory(Path.Combine(libs, "360b014b-b57b-450f-8f12-9904618ff370"));
            Check(!FaceSettings.BabbleModuleInstalled(dir.FullName), "BabbleModuleInstalled: false for a VRCFT-Babble folder without module.json");
            Directory.CreateDirectory(Path.Combine(libs, "58be7fa6-4102-44ab-adba-25021d3af8b7"));
            File.WriteAllText(Path.Combine(libs, "58be7fa6-4102-44ab-adba-25021d3af8b7", "module.json"), "{}");
            Check(!FaceSettings.BabbleModuleInstalled(dir.FullName), "BabbleModuleInstalled: false when only other modules have module.json");
            File.WriteAllText(Path.Combine(libs, "360b014b-b57b-450f-8f12-9904618ff370", "module.json"), "{}");
            Check(FaceSettings.BabbleModuleInstalled(dir.FullName), "BabbleModuleInstalled: true with CustomLibs\\360b014b-...\\module.json in the given config dir");
        }
        finally
        {
            dir.Delete(true);
        }
    }
}
