#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""FrameFace launcher: python3 -I ~/frameface/frameface_main.py <command> (spec §4.1, §4.21).

-I implies -P (the script's directory is not on sys.path), so the launcher puts its own directory
first; `frameface` is the package next to it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from frameface.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
