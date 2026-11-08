# SPDX-License-Identifier: MIT
"""Discovery and diagnostics from sysfs and /proc (spec §4.4): the presence scan, interface drivers,
the owners of a node, the device's power state, the Type-C roles, the battery and node access.

Read-only: nothing here opens a usbfs node. Every root is a parameter so tests (and the selftest)
run on a fake tree built by frameface.sim.make_rig. Missing or unreadable files give None values,
never exceptions, because devices come and go while we read.
"""
from dataclasses import dataclass
import os
import re
import stat

from .descriptors import FOCUS3_PID, PID, VID

__all__ = ["UsbNode", "SYSNAME_RE", "IFACE_SEP", "scan", "find_vft", "kind", "interface_drivers", "find_owners",
           "read_power",
           "read_typec", "read_battery", "node_access", "bracketed"]

SYSNAME_RE = re.compile(r"^\d+-\d+(\.\d+)*$")
# interface directories are "<sysname>:<config>.<interface>"; "#" stands in for ":" in fake trees on file
# systems that forbid colons (Windows test runs); real sysfs never uses it
_IFACE_RE = re.compile(r"^(\d+-\d+(?:\.\d+)*)[:#](\d+)\.(\d+)$")
IFACE_SEP = "#" if os.name == "nt" else ":"


@dataclass
class UsbNode:
    sysname: str
    devpath: str
    busnum: int
    devnum: int
    speed_mbps: int
    behind_hub: bool
    vid: int
    pid: int
    descriptors: bytes


def _read(path):
    """The stripped text of a sysfs file, or None."""
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace").strip()
    except (OSError, ValueError):
        return None


def _int(text, base=10):
    if text is None:
        return None
    try:
        return int(text, base)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return None


def _devices_dir(sys_root):
    return os.path.join(sys_root, "bus", "usb", "devices")


def scan(sys_root: str = "/sys", dev_root: str = "/dev") -> list:
    """Every USB device (sysfs names like 1-1 or 1-1.2; interfaces and root hubs excluded)."""
    base = _devices_dir(sys_root)
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    out = []
    for name in names:
        if not SYSNAME_RE.match(name):
            continue
        d = os.path.join(base, name)
        vid, pid = _int(_read(os.path.join(d, "idVendor")), 16), _int(_read(os.path.join(d, "idProduct")), 16)
        bus, dev = _int(_read(os.path.join(d, "busnum"))), _int(_read(os.path.join(d, "devnum")))
        if None in (vid, pid, bus, dev):
            continue                    # vanished while we read, or not a device
        try:
            with open(os.path.join(d, "descriptors"), "rb") as f:
                desc = f.read()
        except OSError:
            desc = b""
        out.append(UsbNode(sysname=name, devpath=os.path.join(dev_root, "bus", "usb", "%03d" % bus, "%03d" % dev),
                           busnum=bus, devnum=dev, speed_mbps=_int(_read(os.path.join(d, "speed"))) or 0,
                           behind_hub="." in name, vid=vid, pid=pid, descriptors=desc))
    return out


def kind(node: UsbNode) -> "str | None":
    """"vft" (0bb4:0321), "focus3" (0bb4:06a1, unsupported), "htc" (another 0bb4 device, e.g. the
    firmware-update mode "HTC Boot") or None."""
    if node.vid != VID:
        return None
    if node.pid == PID:
        return "vft"
    return "focus3" if node.pid == FOCUS3_PID else "htc"


def find_vft(sys_root: str = "/sys", dev_root: str = "/dev") -> "UsbNode | None":
    """The first VIVE Facial Tracker (0bb4:0321) of the scan, or None."""
    return next((n for n in scan(sys_root, dev_root) if kind(n) == "vft"), None)


def interface_drivers(sys_root: str, sysname: str) -> dict:
    """{interface number: bound driver name or None} from <sysname>:<c>.<i>/driver (a symlink to the
    driver, or in fake trees a file holding its name). Diagnostics only: DISCONNECT_CLAIM never
    depends on it."""
    base = _devices_dir(sys_root)
    out = {}
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names:
        m = _IFACE_RE.match(name)
        if not m or m.group(1) != sysname:
            continue
        link = os.path.join(base, name, "driver")
        driver = None
        if os.path.islink(link):
            try:
                driver = os.path.basename(os.readlink(link).rstrip("/\\")) or None
            except OSError:
                driver = None
        elif os.path.isfile(link):
            driver = _read(link) or None
        out[int(m.group(3))] = driver
    return dict(sorted(out.items()))


_EXTENDED = "\\\\?\\"                   # the extended-length prefix Windows readlink returns


def _norm(path):
    """A comparable form of a path (on Windows test trees: no extended-length prefix, case folded)."""
    if path.startswith(_EXTENDED):
        path = path[len(_EXTENDED):]
    return os.path.normcase(os.path.normpath(path))


def _link_target(path):
    if os.path.islink(path):
        try:
            return _norm(os.readlink(path))
        except OSError:
            return None
    if os.path.isfile(path):                    # fake /proc: a file holding the link target
        target = _read(path)
        return _norm(target) if target else None
    return None


def find_owners(node_path: str, proc_root: str = "/proc") -> list:
    """[(pid, comm)] of the processes with `node_path` open, from /proc/<pid>/fd/*. The caller's own
    process is listed too if it holds the node; callers that ask about other owners drop os.getpid()."""
    want = {_norm(node_path)}
    try:
        want.add(_norm(os.path.realpath(node_path)))
    except (OSError, ValueError):
        pass
    try:
        pids = [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:
        return []
    out = []
    for pid in sorted(pids, key=int):
        fd_dir = os.path.join(proc_root, pid, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue                            # gone, or another user's process
        if any(_link_target(os.path.join(fd_dir, fd)) in want for fd in fds):
            out.append((int(pid), _read(os.path.join(proc_root, pid, "comm")) or "?"))
    return out


def read_power(sys_root: str, sysname: str) -> dict:
    """The device's runtime PM state: control ("on"/"auto"), autosuspend_delay_ms, runtime_status
    ("active"/"suspended"/...) and runtime_suspended_time (ms); None for missing files."""
    d = os.path.join(_devices_dir(sys_root), sysname, "power")
    return {"control": _read(os.path.join(d, "control")),
            "autosuspend_delay_ms": _int(_read(os.path.join(d, "autosuspend_delay_ms"))),
            "runtime_status": _read(os.path.join(d, "runtime_status")),
            "runtime_suspended_time": _int(_read(os.path.join(d, "runtime_suspended_time")))}


def bracketed(text: "str | None") -> "str | None":
    """The selected value of a sysfs choice list: "host [device]" -> "device"; plain text as is."""
    if text is None:
        return None
    m = re.search(r"\[([^\]]*)\]", text)
    return m.group(1).strip() if m else (text.strip() or None)


def read_typec(sys_root: str = "/sys") -> dict:
    """Type-C port0 roles: data_role ("device" = charging/PC mode, the tracker cannot enumerate),
    power_role, port_type, power_operation_mode and partner (True if a partner is attached; None
    for every key when the port is absent)."""
    base = os.path.join(sys_root, "class", "typec")
    port = os.path.join(base, "port0")
    if not os.path.isdir(port):
        return {"data_role": None, "power_role": None, "port_type": None, "power_operation_mode": None,
                "partner": None}
    return {"data_role": bracketed(_read(os.path.join(port, "data_role"))),
            "power_role": bracketed(_read(os.path.join(port, "power_role"))),
            "port_type": bracketed(_read(os.path.join(port, "port_type"))),
            "power_operation_mode": bracketed(_read(os.path.join(port, "power_operation_mode"))),
            "partner": os.path.isdir(os.path.join(base, "port0-partner"))}


def read_battery(sys_root: str = "/sys") -> dict:
    """{"pct": capacity or None, "charging": True (Charging/Full), False (Discharging/Not charging) or
    None} of the first power supply of type Battery ("battery" preferred)."""
    base = os.path.join(sys_root, "class", "power_supply")
    try:
        names = sorted(os.listdir(base), key=lambda n: (n != "battery", n))
    except OSError:
        names = []
    for name in names:
        d = os.path.join(base, name)
        if (_read(os.path.join(d, "type")) or "").lower() != "battery":
            continue
        status = (_read(os.path.join(d, "status")) or "").lower()
        charging = True if status in ("charging", "full") else False if status in (
            "discharging", "not charging") else None
        pct = _int(_read(os.path.join(d, "capacity")))
        return {"pct": pct if pct is None or 0 <= pct <= 100 else None, "charging": charging}
    return {"pct": None, "charging": None}


def node_access(path: str) -> dict:
    """Permissions of a device node: mode ("0664"), mode_str ("crw-rw-r--"), uid, gid, group name
    and writable (os.access W_OK for this process); exists False and None values if it is missing."""
    try:
        st = os.stat(path)
    except OSError:
        return {"exists": False, "mode": None, "mode_str": None, "uid": None, "gid": None, "group": None,
                "writable": False}
    group = None
    try:
        import grp
        group = grp.getgrgid(st.st_gid).gr_name
    except (ImportError, KeyError, OverflowError):
        group = None
    return {"exists": True, "mode": "%04o" % stat.S_IMODE(st.st_mode), "mode_str": stat.filemode(st.st_mode),
            "uid": st.st_uid, "gid": st.st_gid, "group": group, "writable": os.access(path, os.W_OK)}
