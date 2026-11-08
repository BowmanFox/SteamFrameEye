#!/bin/bash
# SPDX-License-Identifier: MIT
# FrameFace installer for the Steam Frame (headset side; spec section 9.3). It runs as the normal
# user, never calls sudo and writes only under $HOME.
#
#   bash install.sh [--no-start] [--new-token] [--reset-profile] [--allow-peer CIDR]...
#   bash install.sh --rollback
#   bash install.sh --uninstall [--purge]
#
# Install and upgrade: copies the release files of this folder to ~/frameface.new, builds the C
# helpers there, runs the isolated selftest gate, and only when the gate passes swaps
# ~/frameface.new into ~/frameface (the previous version moves to ~/frameface.prev), installs the
# systemd user unit and starts it. Run it from the unpacked or uploaded folder, never from
# ~/frameface itself:
#   mkdir -p ~/frameface-upload-manual && tar xzf FrameFace-<version>.tar.gz -C ~/frameface-upload-manual
#   bash ~/frameface-upload-manual/install.sh && rm -rf ~/frameface-upload-manual
# --rollback swaps ~/frameface and ~/frameface.prev (run it again to swap back). --uninstall removes
# the program, the unit, every ~/frameface-upload-* folder except the one it runs from and the
# probe images; --purge also removes ~/.config/frameface and ~/.local/state/frameface. Both may
# run from anywhere and can be repeated.
set -u
umask 022
unset CDPATH      # `bash some/relative/install.sh` must find this folder, not a namesake on an exported CDPATH

case ${BASH_SOURCE[0]} in
  */*) SRC_DIR=${BASH_SOURCE[0]%/*} ;;
  *) SRC_DIR=. ;;
esac
SRC=$(cd -- "${SRC_DIR:-/}" 2>/dev/null && pwd -P) || { printf '[FAIL] cannot find the folder of install.sh\n'; exit 1; }
cd -- "${HOME:?HOME is not set}" || exit 1

CUR="$HOME/frameface"
PREV="$HOME/frameface.prev"
NEW="$HOME/frameface.new"
CONF_DIR="$HOME/.config/frameface"
CONF="$CONF_DIR/config.json"
STATE="$HOME/.local/state/frameface"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/frameface.service"
SVC=frameface.service

OPT_NO_START=0
OPT_NEW_TOKEN=0
OPT_RESET_PROFILE=0
OPT_PURGE=0
PEERS=()

info() { printf '%s\n' "$*"; }
ok() { printf '[ok] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*"; }
fail() { printf '[FAIL] %s\n' "$*"; }

usage() {
  printf '%s\n' \
    "usage: bash install.sh [--no-start] [--new-token] [--reset-profile] [--allow-peer CIDR]..." \
    "       bash install.sh --rollback" \
    "       bash install.sh --uninstall [--purge]" \
    "" \
    "Installs or upgrades FrameFace from this folder into ~/frameface (no sudo)." \
    "  --no-start        install and enable the user unit, but do not (re)start it" \
    "  --new-token       generate a new stream token (Baballonia's address changes)" \
    "  --reset-profile   forget the learned tracker profile (~/.local/state/frameface/profile.json)" \
    "  --allow-peer CIDR also allow this IPv4 network, e.g. 192.168.4.31/32 (the stream is unencrypted)" \
    "  --rollback        swap ~/frameface and ~/frameface.prev, re-install that unit and start it" \
    "  --uninstall       remove the program, the unit, upload folders and probe images" \
    "  --purge           with --uninstall: also remove ~/.config/frameface and ~/.local/state/frameface"
}

usage_error() {
  printf 'install.sh: %s\n' "$*" >&2
  usage >&2
}

indent() {
  [ -n "$1" ] || return 0
  printf '%s\n' "$1" | sed 's/^/    /'
}

# physical path of a directory (symlinks resolved), empty if it does not exist
phys() { (cd -- "$1" 2>/dev/null && pwd -P); }

version_of() {
  local v
  v=$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' "$1/frameface/__init__.py" 2>/dev/null)
  printf '%s' "${v:-unknown}"
}

valid_cidr() {
  local re='^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})/([0-9]{1,2})$' i
  [[ $1 =~ $re ]] || return 1
  for i in 1 2 3 4; do
    [ "${BASH_REMATCH[$i]}" -le 255 ] || return 1
  done
  [ "${BASH_REMATCH[5]}" -le 32 ]
}

user_env() { export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"; }

stop_service() {
  if systemctl --user is-active --quiet "$SVC"; then
    info "stopping $SVC (it tears the tracker down cleanly)"
    systemctl --user stop "$SVC" || warn "could not stop $SVC"
  fi
}

copy_selftest_result() {
  if [ -f "$CUR/selftest.json" ]; then
    mkdir -p -m 0700 -- "$STATE"
    install -m 0600 "$CUR/selftest.json" "$STATE/selftest.json" || warn "could not copy selftest.json to $STATE"
  else
    warn "$CUR/selftest.json is missing"
  fi
}

udev_text() {
  printf '%s\n' "" \
    "[info] P7 FAIL: this user cannot open the tracker's USB node. One-time udev fallback: type these" \
    "       commands yourself (FrameFace never runs sudo), then unplug and replug the tracker:" \
    "  sudo install -m 0644 ~/frameface/70-vive-facial-tracker.rules /etc/udev/rules.d/" \
    "  sudo mkdir -p /etc/atomic-update.conf.d" \
    "  echo /etc/udev/rules.d/70-vive-facial-tracker.rules | sudo tee /etc/atomic-update.conf.d/vive-facial-tracker.conf" \
    "  sudo udevadm control --reload && sudo udevadm trigger --action=add --subsystem-match=usb --attr-match=idVendor=0bb4" \
    "  undo: sudo rm /etc/udev/rules.d/70-vive-facial-tracker.rules /etc/atomic-update.conf.d/vive-facial-tracker.conf && sudo udevadm control --reload" \
    "  The atomic-update entry keeps the rule across SteamOS updates (~/frameface/README.md, \"udev fallback\")."
}

do_install() {
  local home_p d f p out rc

  # 0. never from an installed copy: the stage and the swap would replace this script's own folder
  home_p=$(phys "$HOME")
  for d in "$CUR" "$PREV" "$NEW"; do
    for p in "$home_p/${d##*/}" "$(phys "$d")"; do
      [ -n "$p" ] || continue
      case "$SRC/" in
        "$p/"*)
          fail "install.sh is running from $SRC: run install.sh from the unpacked or uploaded folder"
          info "  e.g. mkdir -p ~/frameface-upload-manual && tar xzf FrameFace-<version>.tar.gz -C ~/frameface-upload-manual"
          info "       bash ~/frameface-upload-manual/install.sh"
          return 1 ;;
      esac
    done
  done

  # 1. preflight
  d=$(uname -m 2>/dev/null || echo unknown)
  if [ "$d" = aarch64 ]; then ok "architecture $d"; else warn "architecture $d: the Steam Frame is aarch64; continuing"; fi
  if [ "$(id -u)" = 0 ]; then warn "running as root: FrameFace belongs to the normal user (steamos); do not use sudo"; fi
  if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 not found"
    return 1
  fi
  if ! python3 -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    fail "python3 3.10 or newer is required (found: $(python3 -V 2>&1))"
    return 1
  fi
  ok "$(python3 -V 2>&1)"
  user_env
  if ! systemctl --user show-environment >/dev/null 2>&1; then
    fail "systemctl --user does not work (XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR)"
    info "  hint: the user's systemd instance must be running: switch the headset on, let it reach Game Mode"
    info "  or Desktop Mode (logged in as this user), then run install.sh again"
    return 1
  fi
  local have_gcc=1
  if ! command -v gcc >/dev/null 2>&1; then
    have_gcc=0
    warn "gcc not found: the C pixel helper cannot be built; FrameFace uses its Python pixels (<= 30 fps)"
  fi

  # 2. stage exactly the release files of this folder
  if [ ! -f "$SRC/release-files.txt" ]; then
    fail "release-files.txt is missing in $SRC (incomplete upload or archive)"
    return 1
  fi
  local -a files=()
  while IFS= read -r f || [ -n "$f" ]; do
    f=${f%$'\r'}
    [ -n "$f" ] || continue
    case $f in
      /*|..|../*|*/..|*/../*|*[!A-Za-z0-9._/-]*)
        fail "invalid entry in release-files.txt: $f"
        return 1 ;;
    esac
    if [ ! -f "$SRC/$f" ]; then
      fail "release file missing: $SRC/$f (incomplete upload or archive)"
      return 1
    fi
    files+=("$f")
  done < "$SRC/release-files.txt"
  if [ ${#files[@]} -eq 0 ]; then
    fail "release-files.txt in $SRC lists no files"
    return 1
  fi
  rm -rf -- "$NEW"
  if ! mkdir -p -- "$NEW"; then
    fail "cannot create $NEW"
    return 1
  fi
  for f in "${files[@]}"; do
    case $f in */*) mkdir -p -- "$NEW/${f%/*}" ;; esac
    if ! cp -- "$SRC/$f" "$NEW/$f"; then
      fail "cannot copy $f to $NEW"
      rm -rf -- "$NEW"
      return 1
    fi
  done
  ok "staged ${#files[@]} files of FrameFace $(version_of "$NEW") in $NEW"

  # 3. native helpers (a failure is a warning: the selftest and the service fall back)
  mkdir -p -- "$NEW/lib"
  local log="$NEW/lib/build.log"
  : > "$log"
  if [ "$have_gcc" = 1 ]; then
    printf '$ gcc -std=c99 -O2 -Wall -Wextra -fPIC -shared -o lib/libffproc.so csrc/ffproc.c\n' >> "$log"
    if (cd -- "$NEW" && gcc -std=c99 -O2 -Wall -Wextra -fPIC -shared -o lib/libffproc.so csrc/ffproc.c) >> "$log" 2>&1; then
      ok "built lib/libffproc.so (C pixel helper)"
    else
      rm -f -- "$NEW/lib/libffproc.so"
      warn "could not build the C pixel helper (~/frameface/lib/build.log): FrameFace uses its Python pixels (<= 30 fps)"
    fi
    printf '$ gcc -std=c99 -O2 -Wall -Wextra -o lib/abi_dump csrc/abi_dump.c\n' >> "$log"
    if (cd -- "$NEW" && gcc -std=c99 -O2 -Wall -Wextra -o lib/abi_dump csrc/abi_dump.c) >> "$log" 2>&1; then
      ok "built lib/abi_dump"
    else
      rm -f -- "$NEW/lib/abi_dump"
      warn "could not build lib/abi_dump (~/frameface/lib/build.log): the selftest skips its ABI check"
    fi
  else
    printf 'gcc not found: nothing was built\n' >> "$log"
  fi

  # 4. the install gate: isolated selftest (the running service and its state are not touched)
  info "selftest of the new version (isolated; the running service is not touched):"
  out=$(python3 -I "$NEW/frameface_main.py" selftest --lib "$NEW/lib" --result "$NEW/selftest.json" 2>&1)
  rc=$?
  indent "$out"
  case $rc in
    0) ok "selftest passed" ;;
    3) rm -f -- "$NEW/lib/libffproc.so"
       warn "the C pixel helper failed the parity check and was removed: FrameFace uses its Python pixels (<= 30 fps)" ;;
    4) fail "libturbojpeg missing: the service will report it" ;;
    *) if [ -d "$CUR" ]; then
         fail "selftest failed (exit $rc): nothing was installed; FrameFace $(version_of "$CUR") stays installed and running unchanged"
       else
         fail "selftest failed (exit $rc): nothing was installed"
       fi
       rm -rf -- "$NEW"
       return 1 ;;
  esac

  # 5. config (created once; the token and allow_peers survive upgrades)
  if [ ! -f "$CONF" ] || [ "$OPT_NEW_TOKEN" = 1 ] || [ ${#PEERS[@]} -gt 0 ]; then
    local -a ic=(init-config)
    if [ "$OPT_NEW_TOKEN" = 1 ]; then ic+=(--new-token); fi
    for p in ${PEERS[@]+"${PEERS[@]}"}; do ic+=(--allow-peer "$p"); done
    if ! python3 -I "$NEW/frameface_main.py" "${ic[@]}"; then
      fail "init-config failed: nothing was installed"
      rm -rf -- "$NEW"
      return 1
    fi
  else
    ok "config kept: $CONF"
  fi

  # 6. stop the running version (SIGTERM: clean teardown)
  stop_service
  if [ "$OPT_RESET_PROFILE" = 1 ]; then
    rm -f -- "$STATE/profile.json"
    ok "tracker profile forgotten (--reset-profile)"
  fi

  # 7. swap, then record this version's gate result in the state dir
  rm -rf -- "$PREV"
  if [ -e "$CUR" ] || [ -L "$CUR" ]; then
    if ! mv -- "$CUR" "$PREV"; then
      fail "cannot move $CUR to $PREV"
      return 1
    fi
  fi
  if ! mv -- "$NEW" "$CUR"; then
    fail "cannot move $NEW to $CUR"
    if [ -d "$PREV" ] && [ ! -e "$CUR" ]; then mv -- "$PREV" "$CUR"; fi
    return 1
  fi
  if [ -d "$PREV" ]; then
    ok "installed FrameFace $(version_of "$CUR") in $CUR (previous version $(version_of "$PREV") in $PREV)"
  else
    ok "installed FrameFace $(version_of "$CUR") in $CUR"
  fi
  copy_selftest_result

  # 8. the systemd user unit
  local unit_ok=1
  mkdir -p -- "$UNIT_DIR"
  if ! install -m 0644 "$CUR/frameface.service" "$UNIT"; then
    fail "cannot install $UNIT"
    return 1
  fi
  systemctl --user daemon-reload || { fail "systemctl --user daemon-reload failed"; unit_ok=0; }
  systemctl --user enable "$SVC" || { fail "systemctl --user enable $SVC failed"; unit_ok=0; }
  if [ "$OPT_NO_START" = 0 ]; then
    systemctl --user restart "$SVC" || { fail "systemctl --user restart $SVC failed (journalctl --user -u frameface)"; unit_ok=0; }
  else
    info "not started (--no-start); start it with: python3 -I ~/frameface/frameface_main.py start"
  fi

  # 9. report: status, URL, quick probe
  if [ "$OPT_NO_START" = 0 ]; then
    local end=$((SECONDS + 5)) st_rc=1
    while :; do
      out=$(python3 -I "$CUR/frameface_main.py" status 2>&1)
      st_rc=$?
      [ "$st_rc" = 0 ] && break
      [ "$SECONDS" -ge "$end" ] && break
      sleep 1
    done
    if [ "$st_rc" = 0 ]; then
      ok "the service answers:"
    else
      warn "the service did not answer within 5 s (journalctl --user -u frameface):"
    fi
    indent "$out"
  fi
  info "Baballonia Face Camera Address (\"This is a...\" = Wireless/IP Camera):"
  python3 -I "$CUR/frameface_main.py" url || warn "could not print the URL: python3 -I ~/frameface/frameface_main.py url"
  info "quick probe:"
  out=$(python3 -I "$CUR/frameface_main.py" probe --quick 2>&1)
  indent "$out"
  if printf '%s\n' "$out" | grep -Eq '\[FAIL\] P7([^0-9]|$)'; then
    udev_text
  fi
  if [ "$unit_ok" != 1 ]; then
    return 1
  fi
  ok "FrameFace $(version_of "$CUR") is installed"
  return 0
}

do_rollback() {
  local out rc
  if [ ! -f "$PREV/frameface_main.py" ]; then
    fail "no previous version to roll back to ($PREV/frameface_main.py is missing); nothing was changed"
    return 1
  fi
  user_env
  stop_service
  rm -rf -- "$NEW"
  if [ -e "$CUR" ] || [ -L "$CUR" ]; then
    if ! mv -- "$CUR" "$NEW"; then
      fail "cannot move $CUR aside"
      return 1
    fi
  fi
  if ! mv -- "$PREV" "$CUR"; then
    fail "cannot move $PREV to $CUR"
    if [ -e "$NEW" ]; then mv -- "$NEW" "$CUR"; fi
    return 1
  fi
  if [ -e "$NEW" ]; then
    mv -- "$NEW" "$PREV" || warn "could not move the newer version to $PREV"
  fi
  ok "rolled back: $CUR is FrameFace $(version_of "$CUR"); $PREV holds $(version_of "$PREV") (--rollback again swaps back)"
  mkdir -p -- "$UNIT_DIR"
  if ! install -m 0644 "$CUR/frameface.service" "$UNIT"; then
    fail "cannot install $UNIT"
    return 1
  fi
  systemctl --user daemon-reload || warn "systemctl --user daemon-reload failed"
  info "selftest --quick of the restored version:"
  out=$(python3 -I "$CUR/frameface_main.py" selftest --quick --lib "$CUR/lib" --result "$CUR/selftest.json" 2>&1)
  rc=$?
  indent "$out"
  if [ "$rc" != 0 ]; then
    warn "selftest --quick exit $rc; rolling back anyway, as requested"
  fi
  copy_selftest_result
  if ! systemctl --user start "$SVC"; then
    fail "systemctl --user start $SVC failed (journalctl --user -u frameface)"
    return 1
  fi
  ok "started $SVC"
  return 0
}

do_uninstall() {
  local d own
  user_env
  systemctl --user disable --now "$SVC" >/dev/null 2>&1 || true
  # the unit, and its enable link in case `disable` could not reach the user's systemd
  rm -f -- "$UNIT" "$UNIT_DIR/default.target.wants/$SVC"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  rm -rf -- "$CUR" "$PREV" "$NEW"
  for d in "$HOME"/frameface-upload-*; do
    [ -d "$d" ] || continue
    own=$(phys "$d")
    if [ "$own" = "$SRC" ]; then
      continue
    fi
    rm -rf -- "$d"
  done
  for d in "$STATE"/probe-*-images; do
    if [ -e "$d" ] || [ -L "$d" ]; then
      rm -rf -- "$d"
    fi
  done
  rm -f -- "$STATE/probe-stopped-service"
  if [ "$OPT_PURGE" = 1 ]; then
    rm -rf -- "$CONF_DIR" "$STATE"
    ok "removed $CONF_DIR and $STATE (--purge)"
  else
    info "kept $CONF_DIR (config, token) and $STATE (profile, probe reports); --uninstall --purge removes them"
  fi
  ok "FrameFace is uninstalled (the unit, ~/frameface, ~/frameface.prev, upload folders and probe images are gone)"
  return 0
}

main() {
  local mode=install modes=0 p
  while [ $# -gt 0 ]; do
    case $1 in
      --no-start) OPT_NO_START=1 ;;
      --new-token) OPT_NEW_TOKEN=1 ;;
      --reset-profile) OPT_RESET_PROFILE=1 ;;
      --allow-peer)
        if [ $# -lt 2 ]; then
          usage_error "--allow-peer needs a CIDR such as 192.168.4.31/32"
          return 2
        fi
        PEERS+=("$2")
        shift ;;
      --allow-peer=*) PEERS+=("${1#--allow-peer=}") ;;
      --rollback) mode=rollback; modes=$((modes + 1)) ;;
      --uninstall) mode=uninstall; modes=$((modes + 1)) ;;
      --purge) OPT_PURGE=1 ;;
      -h|--help) usage; return 0 ;;
      *) usage_error "unknown option: $1"; return 2 ;;
    esac
    shift
  done
  if [ "$modes" -gt 1 ]; then
    usage_error "--rollback and --uninstall cannot be combined"
    return 2
  fi
  if [ "$OPT_PURGE" = 1 ] && [ "$mode" != uninstall ]; then
    usage_error "--purge is only used with --uninstall"
    return 2
  fi
  if [ "$mode" != install ] && { [ "$OPT_NO_START$OPT_NEW_TOKEN$OPT_RESET_PROFILE" != 000 ] || [ ${#PEERS[@]} -gt 0 ]; }; then
    usage_error "--no-start, --new-token, --reset-profile and --allow-peer are install options"
    return 2
  fi
  for p in ${PEERS[@]+"${PEERS[@]}"}; do
    if ! valid_cidr "$p"; then
      usage_error "invalid --allow-peer $p (an IPv4 CIDR such as 192.168.4.31/32)"
      return 2
    fi
  done
  case $mode in
    install) do_install ;;
    rollback) do_rollback ;;
    uninstall) do_uninstall ;;
  esac
}

main "$@"; exit $?
