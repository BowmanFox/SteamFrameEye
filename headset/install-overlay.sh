#!/bin/bash
# Install FrameEye Bridge as a SteamVR dashboard application on the headset.
#
#   bash install-overlay.sh <pc-ip>[:port] [more sender options]
#
# Copies the app to ~/frameeye, retires the systemd user unit variant if it is enabled, stops a
# running instance (it would keep running its old code), writes frameeye.vrmanifest with the
# sender options, registers it with SteamVR, enables auto-launch and starts it. Re-run to update
# the app or to change the target.
# Everything stays in the home directory; no root, steamos-readonly untouched.
set -euo pipefail
TARGET="${1:?usage: install-overlay.sh <pc-ip>[:port] [more sender options]}"
shift
case "$TARGET" in *:*) ;; *) TARGET="$TARGET:9021" ;; esac
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/frameeye"
mkdir -p "$DEST"
for f in frameeye_osc.py frameeye_overlay.py frameeye_overlay.sh openvr_min.py bitmap_font.py gl_texture.py icon.png; do
  cp "$HERE/$f" "$DEST/$f"
done
chmod +x "$DEST/frameeye_overlay.py" "$DEST/frameeye_overlay.sh"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if systemctl --user is-enabled frameeye-osc.service >/dev/null 2>&1; then
  echo "retiring the systemd user unit frameeye-osc.service (the overlay replaces it)"
  systemctl --user disable --now frameeye-osc.service || true
fi
if pkill -TERM -f 'frameeye_overlay\.py'; then
  echo "stopping the running instance so the new version takes over"
  for _ in $(seq 20); do
    pgrep -f 'frameeye_overlay\.py' >/dev/null || break
    sleep 0.25
  done
fi
python3 "$DEST/frameeye_overlay.py" --install --raw "$TARGET" "$@"
echo
python3 "$DEST/frameeye_overlay.py" --status
