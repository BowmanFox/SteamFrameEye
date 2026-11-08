#!/bin/sh
# SPDX-License-Identifier: MIT
# FrameFace Linux/arm64 test runner (spec section 11.1).
#
#   sh tools/docker/arm64-checks.sh <repo> [pattern ...]      default pattern: test_ff_*.py
#
# Builds the native artefacts that exist at this stage of the task plan (each one optional) into a
# temporary directory, exports their paths for the tests, and runs unittest discovery once per
# pattern with FRAMEFACE_REQUIRE_NATIVE=1, so a Linux-only test fails instead of skipping. It writes
# nothing under the (read-only) repository and does not syntax-check install.sh (test_ff_install_sh
# and the CI bash -n list do that).
set -eu
if [ $# -lt 1 ]; then
  echo "usage: sh arm64-checks.sh <repo> [pattern ...]" >&2
  exit 2
fi
# absolute, because the tests run from the temporary directory ("sh arm64-checks.sh ." works too)
R=$(CDPATH= cd -- "$1" && pwd) || { echo "arm64-checks.sh: not a directory: $1" >&2; exit 2; }
shift; W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
if [ -f "$R/headset/frameface/csrc/abi_dump.c" ]; then
  gcc -std=c99 -O2 -Wall -Wextra -Werror -o "$W/abi_dump" "$R/headset/frameface/csrc/abi_dump.c"
  FRAMEFACE_ABI_DUMP="$W/abi_dump"; export FRAMEFACE_ABI_DUMP
fi
if [ -f "$R/headset/frameface/csrc/ffproc.c" ]; then
  gcc -std=c99 -O2 -Wall -Wextra -Werror -fPIC -shared -o "$W/libffproc.so" "$R/headset/frameface/csrc/ffproc.c"
  FRAMEFACE_LIBFFPROC="$W/libffproc.so"; export FRAMEFACE_LIBFFPROC
fi
[ $# -gt 0 ] || set -- 'test_ff_*.py'
cd "$W"
for p in "$@"; do
  FRAMEFACE_REQUIRE_NATIVE=1 PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s "$R/tools/tests" -p "$p" -v
done
