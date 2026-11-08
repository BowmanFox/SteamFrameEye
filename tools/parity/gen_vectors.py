#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Oracle vectors for tools/OpenCvParity (spec Appendix G); stdlib only.

    python tools/parity/gen_vectors.py --out DIR [--frames NAME,NAME,...]

Writes DIR/frame_<i>.bin (320000-byte input frames: lcg seeds 1-4, zero, 255, gradient,
checkerboard), DIR/expected_<i>_<phase>_<col0>.bin (160000-byte preprocess_ref outputs for phase
0/1 and col0 0/200) and DIR/manifest.json. <i> is the frame's index in the full list, also when
--frames selects a subset. Every vector is cross-checked against preprocess_swar; a difference
exits 1. The CI job opencv-parity feeds DIR to `dotnet run --project tools/OpenCvParity -- DIR`.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "headset" / "frameface"))
from frameface import preprocess as pp  # noqa: E402


def gradient():
    """Appendix D: ((i // 800) * 7 + (i % 800) * 3) & 0xFF."""
    return bytes(((i // 800) * 7 + (i % 800) * 3) & 0xFF for i in range(pp.FRAME_BYTES))


def checkerboard():
    """8 x 8 pixel cells; the odd plane is the inverse of the even plane, so a wrong plane choice
    (YUV2GRAY_Y422 = odd bytes vs YUV2GRAY_YUY2 = even bytes) cannot go unnoticed."""
    out = bytearray(pp.FRAME_BYTES)
    for y in range(400):
        even = bytes(255 if ((x // 8) + (y // 8)) & 1 else 0 for x in range(400))
        out[y * 800:(y + 1) * 800:2] = even
        out[y * 800 + 1:(y + 1) * 800:2] = bytes(255 - v for v in even)
    return bytes(out)


FRAMES = (                    # (name, builder); the position is the frame index <i>
    ("lcg1", lambda: pp.lcg_frame(1)),
    ("lcg2", lambda: pp.lcg_frame(2)),
    ("lcg3", lambda: pp.lcg_frame(3)),
    ("lcg4", lambda: pp.lcg_frame(4)),
    ("zero", lambda: bytes(pp.FRAME_BYTES)),
    ("all255", lambda: bytes([255]) * pp.FRAME_BYTES),
    ("gradient", gradient),
    ("checkerboard", checkerboard),
)
PIPELINE = ("plane of parity `phase` (1 = odd bytes = YUV2GRAY_Y422, 0 = even bytes = YUV2GRAY_YUY2) "
            "-> ColRange(col0, col0 + 200) -> Resize(400 x 400, linear) -> GaussianBlur(5 x 5, sigma 0) "
            "-> LUT")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def generate(out_dir, names=None, log=print):
    """Write the vectors of the selected frames (all by default) and return the manifest."""
    selected = [(i, name, build) for i, (name, build) in enumerate(FRAMES) if names is None or name in names]
    os.makedirs(out_dir, exist_ok=True)
    manifest = {
        "v": 1, "generator": "tools/parity/gen_vectors.py", "width": 400, "height": 400,
        "frame_bytes": pp.FRAME_BYTES, "out_bytes": pp.OUT_BYTES, "pipeline": PIPELINE,
        "lut": list(pp.LUT), "lut_sha256": sha256(pp.LUT), "frames": [], "expected": [],
    }
    for index, name, build in selected:
        frame = build()
        path = "frame_%d.bin" % index
        Path(out_dir, path).write_bytes(frame)
        manifest["frames"].append({"index": index, "name": name, "file": path, "sha256": sha256(frame)})
        for phase in (0, 1):
            for col0 in (0, 200):
                expected = pp.preprocess_ref(frame, phase, col0)
                if expected != pp.preprocess_swar(frame, phase, col0):
                    raise SystemExit("oracle and SWAR differ on %s phase %d col0 %d" % (name, phase, col0))
                path = "expected_%d_%d_%d.bin" % (index, phase, col0)
                Path(out_dir, path).write_bytes(expected)
                manifest["expected"].append({"frame": index, "phase": phase, "col0": col0, "file": path,
                                             "sha256": sha256(expected)})
        log("%s: frame_%d.bin + 4 expected outputs" % (name, index))
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=1)
        fh.write("\n")
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write the OpenCvParity oracle vectors (spec Appendix G).")
    ap.add_argument("--out", required=True, help="output directory (created if missing)")
    ap.add_argument("--frames", help="comma-separated subset of: " + ", ".join(n for n, _ in FRAMES))
    args = ap.parse_args(argv)
    names = None
    if args.frames:
        names = [n.strip() for n in args.frames.split(",") if n.strip()]
        unknown = sorted(set(names) - {n for n, _ in FRAMES})
        if unknown or not names:
            ap.error("unknown frame name(s): %s" % ", ".join(unknown or ["(none given)"]))
    manifest = generate(args.out, names)
    print("wrote %d frames, %d expected outputs and manifest.json to %s"
          % (len(manifest["frames"]), len(manifest["expected"]), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
