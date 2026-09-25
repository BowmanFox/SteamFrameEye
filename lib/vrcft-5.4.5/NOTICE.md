# Reference assemblies for building against VRCFaceTracking 5.4.5

These files are only used as compile-time references (`Private=false`); they are not shipped with the module.

- `VRCFaceTracking.SDK.dll`, `VRCFaceTracking.Core.dll` — from VRCFaceTracking 5.4.5, Copyright 2024 benaclejames, Apache License 2.0 (see `LICENSE-VRCFaceTracking`). Source: https://github.com/benaclejames/VRCFaceTracking
- `Microsoft.Extensions.Logging.Abstractions.dll` (10.0.x) — Microsoft, MIT License.

When VRCFT is installed locally the build prefers the installed copies, so a newer VRCFT can be targeted without touching this folder (`-p:VrcftDir=...`).
