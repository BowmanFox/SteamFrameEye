#!/usr/bin/env python3
"""Generates headset/icon.png (256x256 RGBA) for the SteamVR dashboard tab: a schematic eye.
Standard library only (PNG written by hand)."""
import math
import os
import struct
import sys
import zlib

W = H = 256
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "icon.png")

BG = (36, 48, 64, 255)
SCLERA = (236, 238, 240, 255)
IRIS = (78, 160, 168, 255)
PUPIL = (18, 22, 28, 255)
GLINT = (255, 255, 255, 255)


def main():
    px = [[(0, 0, 0, 0)] * W for _ in range(H)]
    # rounded square background
    rad = 40
    for y in range(H):
        for x in range(W):
            dx = max(rad - x, 0, x - (W - 1 - rad))
            dy = max(rad - y, 0, y - (H - 1 - rad))
            if dx * dx + dy * dy <= rad * rad:
                px[y][x] = BG
    # almond-shaped eye opening
    for x in range(20, W - 20):
        t = (x - 20) / (W - 40)
        h = 66 * math.sin(math.pi * t) ** 0.85
        for y in range(int(128 - h), int(128 + h) + 1):
            px[y][x] = SCLERA
    # iris, pupil, glint (clipped to the opening)
    def disc(cx, cy, r, color):
        for y in range(cy - r, cy + r + 1):
            for x in range(cx - r, cx + r + 1):
                if 0 <= x < W and 0 <= y < H and (x - cx) ** 2 + (y - cy) ** 2 <= r * r and px[y][x] in (SCLERA, IRIS, PUPIL):
                    px[y][x] = color
    disc(128, 128, 50, IRIS)
    disc(128, 128, 24, PUPIL)
    disc(112, 112, 9, GLINT)

    raw = b"".join(b"\x00" + bytes(c for p in row for c in p) for row in px)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)) \
        + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    out = os.path.normpath(OUT)
    with open(out, "wb") as fh:
        fh.write(png)
    print("wrote", out, len(png), "bytes")


if __name__ == "__main__":
    sys.exit(main())
