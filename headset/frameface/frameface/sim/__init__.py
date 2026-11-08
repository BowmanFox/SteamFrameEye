# SPDX-License-Identifier: MIT
"""Simulated hardware for tests, the selftest and `frameface_main.py sim` (spec §7).

SimClock (deterministic time), VftModel/FakeVft (the tracker, every disputed fact a knob),
FakeKernel/make_rig (usbfs on the driver's real ctypes memory plus a temp sysfs/dev/proc tree),
make_scene_frame (synthetic images), TraceSys/ReplayKernel (trace and replay) and check_trace
(the driver-matrix trace invariants). BabaClient lives in sim/babaclient.py (task T4).
"""
from dataclasses import dataclass
import os
import shutil
import tempfile

from ..sysfs import IFACE_SEP, find_vft
from .simclock import SimClock
from .fakevft import FakeVft, VftModel
from .fakekernel import FakeKernel
from .scenes import make_scene_frame
from .replay import ReplayKernel, TraceSys
from .invariants import check_trace

__all__ = ["SimClock", "VftModel", "FakeVft", "FakeKernel", "Rig", "make_rig", "make_scene_frame", "TraceSys",
           "ReplayKernel", "check_trace"]


class _Tree:
    """The fake sysfs, /dev and /proc of a rig (§7.3)."""

    def __init__(self, root):
        self.root = root
        self.sys_root, self.dev_root, self.proc_root = (os.path.join(root, d) for d in ("sys", "dev", "proc"))

    def devpath(self, bus, dev):
        return os.path.join(self.dev_root, "bus", "usb", "%03d" % bus, "%03d" % dev)

    def _dev_dir(self, sysname):
        return os.path.join(self.sys_root, "bus", "usb", "devices", sysname)

    @staticmethod
    def _write(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else (str(data) + "\n").encode())

    def write_device(self, k):
        vft, d = k.vft, self._dev_dir(k.sysname)
        files = {"idVendor": "0bb4", "idProduct": "0321", "busnum": k.busnum, "devnum": k.devnum,
                 "speed": vft.speed_mbps, "descriptors": vft.descriptors, "product": "HTC Multimedia Camera",
                 "manufacturer": "HTC Multimedia Camera", "bMaxPower": "500mA" if vft.m.persona == "hs" else "512mA",
                 "power/control": "on", "power/autosuspend_delay_ms": 2000, "power/runtime_status": "active",
                 "power/runtime_suspended_time": 0}
        for name, value in files.items():
            self._write(os.path.join(d, *name.split("/")), value)
        for i in (0, 1):
            idir = self._dev_dir("%s%s1.%d" % (k.sysname, IFACE_SEP, i))
            os.makedirs(idir, exist_ok=True)
            self.set_iface_driver(k.sysname, i, k.kernel_driver.get(i))
        node = k.devpath
        self._write(node, b"")
        os.chmod(node, 0o444 if vft.m.node_mode == "ro" else 0o664)

    def set_iface_driver(self, sysname, i, name):
        path = os.path.join(self._dev_dir("%s%s1.%d" % (sysname, IFACE_SEP, i)), "driver")
        if name:
            self._write(path, name)
        elif os.path.exists(path):
            os.remove(path)

    def remove_device(self, k):
        for name in (k.sysname, k.sysname + IFACE_SEP + "1.0", k.sysname + IFACE_SEP + "1.1"):
            shutil.rmtree(self._dev_dir(name), ignore_errors=True)
        try:
            os.chmod(k.devpath, 0o664)
            os.remove(k.devpath)
        except OSError:
            pass

    def write_static(self, k):
        typec = os.path.join(self.sys_root, "class", "typec")
        for name, value in (("data_role", "[host] device"), ("power_role", "[source] sink"), ("port_type", "dual"),
                            ("power_operation_mode", "default")):
            self._write(os.path.join(typec, "port0", name), value)
        os.makedirs(os.path.join(typec, "port0-partner"), exist_ok=True)
        bat = os.path.join(self.sys_root, "class", "power_supply", "battery")
        for name, value in (("type", "Battery"), ("capacity", 71), ("status", "Discharging")):
            self._write(os.path.join(bat, name), value)
        os.makedirs(self.proc_root, exist_ok=True)
        owner = k.vft.m.other_usbfs_owner
        if owner:
            pid, comm = owner
            self._write(os.path.join(self.proc_root, str(pid), "comm"), comm)
            link = os.path.join(self.proc_root, str(pid), "fd", "7")
            os.makedirs(os.path.dirname(link), exist_ok=True)
            try:
                os.symlink(k.devpath, link)
            except (OSError, NotImplementedError):
                self._write(link, k.devpath)            # no symlinks here: a file holding the target


@dataclass
class Rig:
    kernel: FakeKernel
    vft: FakeVft
    sys_root: str
    dev_root: str
    proc_root: str
    devpath: str
    clock: object
    root: str = ""
    owns_root: bool = False

    def node(self):
        """The tracker's UsbNode from the fake sysfs (None while unplugged)."""
        return find_vft(self.sys_root, self.dev_root)

    def close(self) -> None:
        self.kernel.shutdown()
        if self.owns_root:
            _rmtree(self.root)


def _rmtree(root):
    for path, _, names in os.walk(root):                 # read-only nodes ("ro" rigs) on Windows
        for name in names:
            try:
                os.chmod(os.path.join(path, name), 0o666)
            except OSError:
                pass
    shutil.rmtree(root, ignore_errors=True)


def make_rig(model: "VftModel | None" = None, clock=None, tmp: "str | None" = None) -> Rig:
    """A FakeVft behind a FakeKernel with a temp sysfs/dev/proc tree (SimClock unless a clock is given)."""
    clock = SimClock() if clock is None else clock
    owns = tmp is None
    root = tempfile.mkdtemp(prefix="ffrig-") if owns else tmp
    vft = FakeVft(model if model is not None else VftModel(), clock)
    tree = _Tree(root)
    kernel = FakeKernel(vft, clock, tree)
    vft.kernel = kernel
    return Rig(kernel, vft, tree.sys_root, tree.dev_root, tree.proc_root, kernel.devpath, clock, root, owns)
