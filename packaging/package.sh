#!/usr/bin/env bash
# Package the built module the way VRCFaceTracking expects it:
#   SteamFrameEyeModule-<version>.zip  = module.json + SteamFrameEyeModule.dll (+ README, LICENSE)
#   FrameEye-Bridge-<version>.tar.gz = headset side: FrameEye Bridge, the SteamVR dashboard app (sender, installer, unit)
#
# VRCFT installs such a zip through "install local module" (InstallLocalModule: extracts, requires
# module.json, moves the folder to %APPDATA%\VRCFaceTracking\CustomLibs\<ModuleId>\ and loads
# DllFileName from it). Unzipping it there by hand gives the same result.
#
# usage: packaging/package.sh <version> <download-url-base> <page-url> [dll-path] [out-dir]
set -euo pipefail
VERSION="${1:?version}"
DL_BASE="${2:?download url base, e.g. https://git.example/org/repo/releases/download/v1.0.0}"
PAGE_URL="${3:?module page url}"
DLL="${4:-bin/Release/net10.0/SteamFrameEyeModule.dll}"
OUT="${5:-dist}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
MODULE_ID="$(tr -d '[:space:]' < "$HERE/module-id.txt")"
ZIP="SteamFrameEyeModule-${VERSION}.zip"
TGZ="FrameEye-Bridge-${VERSION}.tar.gz"

[ -f "$ROOT/$DLL" ] || { echo "module dll not found: $ROOT/$DLL (run dotnet build -c Release first)" >&2; exit 1; }
mkdir -p "$OUT"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

sed -e "s|__MODULE_ID__|$MODULE_ID|" \
    -e "s|__VERSION__|$VERSION|" \
    -e "s|__DATE__|$(date -u +%Y-%m-%dT%H:%M:%S)|" \
    -e "s|__DOWNLOAD_URL__|$DL_BASE/$ZIP|" \
    -e "s|__PAGE_URL__|$PAGE_URL|" \
    "$HERE/module.json.template" > "$STAGE/module.json"
cp "$ROOT/$DLL" "$STAGE/SteamFrameEyeModule.dll"
cp "$ROOT/README.md" "$STAGE/README.md"
[ -f "$ROOT/LICENSE" ] && cp "$ROOT/LICENSE" "$STAGE/LICENSE"

if command -v zip >/dev/null 2>&1; then
  (cd "$STAGE" && zip -q -9 "$OLDPWD/$OUT/$ZIP" module.json SteamFrameEyeModule.dll README.md $( [ -f LICENSE ] && echo LICENSE ))
else
  python3 - "$STAGE" "$OUT/$ZIP" <<'PY'
import os, sys, zipfile
stage, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for name in sorted(os.listdir(stage)):
        z.write(os.path.join(stage, name), name)
PY
fi

tar czf "$OUT/$TGZ" -C "$ROOT/headset" -T "$ROOT/headset/release-files.txt"
cp "$STAGE/module.json" "$OUT/module.json"
echo "packaged:"; ls -la "$OUT"
echo "module.json:"; cat "$OUT/module.json"
