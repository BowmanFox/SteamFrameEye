# SPDX-License-Identifier: MIT
"""Synthetic tracker frames (spec §7.4): make_scene_frame(scene, t, layout, gain) -> 320000 bytes.

Scenes (400x400 gray): mouth (a face-like gradient with LCG noise +-3 and a dark ellipse whose
height follows 0.5+0.5*sin(2*pi*0.5*t)), bars (8 vertical bars, 8 values), dark (values 0..2),
noise (LCG bytes), gradient (static), flat (constant 128). Layouts put the gray image into the
800-byte YUYV rows: h1_copy (odd = even = gray), h1_chroma80 (odd = 0x80), h2_full (the bytes are
an 800-wide image, so even and odd differ like neighbouring columns), flat_odd (odd constant).
Images are cached per (scene, animation phase, layout, gain) so a stream costs no rendering.
"""
import functools
import math

__all__ = ["make_scene_frame", "lcg_bytes", "SCENES", "LAYOUTS", "ANIMATED", "PHASES", "W", "H", "FRAME_BYTES"]

W, H = 400, 400
FRAME_BYTES = W * H * 2
SCENES = ("mouth", "bars", "dark", "noise", "gradient", "flat")
LAYOUTS = ("h1_copy", "h1_chroma80", "h2_full", "flat_odd")
ANIMATED = frozenset({"mouth", "noise"})
PHASES = 16                              # animation phases per 2 s (the mouth period)
_NOISE_STRIDE = 977


def lcg_bytes(seed: int, n: int) -> bytes:
    """The LCG of Appendix D: x = 1664525 x + 1013904223 (mod 2^32), byte = x >> 24."""
    x = seed & 0xFFFFFFFF
    out = bytearray(n)
    for i in range(n):
        x = (1664525 * x + 1013904223) & 0xFFFFFFFF
        out[i] = x >> 24
    return bytes(out)


@functools.lru_cache(maxsize=None)
def _background() -> bytes:
    """A face-like radial gradient (about 30..170) with LCG noise of +-3."""
    noise = lcg_bytes(7, W * H)
    out = bytearray(W * H)
    for y in range(H):
        dy2 = (y - 170) ** 2
        row = y * W
        for x in range(W):
            v = 170 - (((x - 200) ** 2 + dy2) * 140) // 125000
            out[row + x] = max(30, min(220, v + noise[row + x] % 7 - 3))
    return bytes(out)


@functools.lru_cache(maxsize=None)
def _noise_pool() -> bytes:
    return lcg_bytes(11, W * H + PHASES * _NOISE_STRIDE)


@functools.lru_cache(maxsize=64)
def _gray(scene: str, phase: int) -> bytes:
    if scene == "mouth":
        img = bytearray(_background())
        h = 0.5 + 0.5 * math.sin(2 * math.pi * phase / PHASES)
        cx, cy, a, b = 200, 290, 80.0, 8.0 + 40.0 * h
        for y in range(int(cy - b), int(cy + b) + 1):
            q = 1.0 - ((y - cy) / b) ** 2
            if q <= 0:
                continue
            dx = a * math.sqrt(q)
            x0, x1 = max(0, int(cx - dx)), min(W, int(cx + dx) + 1)
            row = y * W
            img[row + x0:row + x1] = bytes(12 + (v & 7) for v in img[row + x0:row + x1])
        return bytes(img)
    if scene == "bars":
        row = b"".join(bytes([16 + 32 * i]) * (W // 8) for i in range(8))
        return row * H
    if scene == "dark":
        return bytes((x + 2 * y) % 3 for y in range(H) for x in range(W))
    if scene == "noise":
        off = phase * _NOISE_STRIDE
        return _noise_pool()[off:off + W * H]
    if scene == "gradient":
        return bytes((x * 3 + y * 7) & 0xFF for y in range(H) for x in range(W))
    if scene == "flat":
        return bytes([128]) * (W * H)
    raise ValueError("unknown scene %r (one of %s)" % (scene, ", ".join(SCENES)))


@functools.lru_cache(maxsize=32)
def _gain_table(gain: float) -> bytes:
    return bytes(min(255, int(v * gain + 0.5)) for v in range(256))


_PLUS6 = bytes(min(255, v + 6) for v in range(256))
_MINUS6 = bytes(max(0, v - 6) for v in range(256))


@functools.lru_cache(maxsize=96)
def _frame(scene: str, phase: int, layout: str, gain: float) -> bytes:
    gray = _gray(scene, phase)
    if gain != 1.0:
        gray = gray.translate(_gain_table(gain))
    out = bytearray(FRAME_BYTES)
    out[0::2] = gray
    if layout == "h1_copy":
        out[1::2] = gray
    elif layout == "h1_chroma80":
        out[1::2] = b"\x80" * (W * H)
    elif layout == "h2_full":                   # neighbouring columns: +6 / -6 alternately
        odd = bytearray(gray.translate(_PLUS6))
        odd[0::2] = gray[0::2].translate(_MINUS6)
        out[1::2] = odd
    elif layout == "flat_odd":
        out[1::2] = b"\x10" * (W * H)
    else:
        raise ValueError("unknown layout %r (one of %s)" % (layout, ", ".join(LAYOUTS)))
    return bytes(out)


def animation_phase(scene: str, t: float) -> int:
    """The cached animation phase of an animated scene at time t (0 for static scenes)."""
    if scene not in ANIMATED:
        return 0
    return int((t % 2.0) / 2.0 * PHASES) % PHASES


def make_scene_frame(scene: str, t: float, layout: str = "h1_copy", gain: float = 1.0) -> bytes:
    """One 400x400 YUYV frame (320000 bytes) of `scene` at time t in `layout`, brightness x gain."""
    g = max(0.0, round(float(gain), 2))
    return _frame(scene, animation_phase(scene, t), layout, g)
