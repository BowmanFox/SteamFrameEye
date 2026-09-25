#!/bin/sh
# Launcher for the Steam Frame Eye overlay. SteamVR runs this through frameeye.vrmanifest
# (binary_path_linux_arm) with the arguments recorded by --install; it also works from a shell.
cd "$(dirname "$0")" || exit 1
exec /usr/bin/python3 ./frameeye_overlay.py "$@"
