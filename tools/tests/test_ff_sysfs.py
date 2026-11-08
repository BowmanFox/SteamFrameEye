# SPDX-License-Identifier: MIT
"""sysfs discovery, owners, Type-C, battery, power state and node access (spec §4.4; §11.3 test_ff_sysfs,
task T2), on fake trees in temp directories."""
import ffutil  # first: installs the port guard

import os
import shutil
import tempfile
import unittest

ffutil.setup_path()
from frameface import sysfs  # noqa: E402
from frameface.descriptors import build_expected  # noqa: E402
from frameface.sim import VftModel, make_rig  # noqa: E402


def symlinks_work(tmp):
    try:
        os.symlink(os.path.join(tmp, "target"), os.path.join(tmp, "probe-link"))
        return True
    except (OSError, NotImplementedError):
        return False


class Tree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ffsysfs-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sys, self.dev, self.proc = (os.path.join(self.tmp, d) for d in ("sys", "dev", "proc"))
        self.devices = os.path.join(self.sys, "bus", "usb", "devices")

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(text if isinstance(text, bytes) else (text + "\n").encode())

    def device(self, name, vid, pid, bus, devnum, speed="480", descriptors=b""):
        for key, value in (("idVendor", vid), ("idProduct", pid), ("busnum", str(bus)), ("devnum", str(devnum)),
                           ("speed", speed), ("descriptors", descriptors)):
            self.write(os.path.join(self.devices, name, key), value)

    def test_scan_direct_hub_htc_and_focus3(self):
        hs = build_expected("hs")
        self.device("1-1", "0bb4", "0321", 1, 3, descriptors=hs)
        self.device("1-1.2", "0bb4", "0321", 1, 5, "5000")
        self.device("2-1", "0bb4", "0abc", 2, 2, "12")
        self.device("3-1", "0bb4", "06a1", 3, 4)
        self.device("4-1", "046d", "c52b", 4, 2, "1.5")
        self.device("usb1", "1d6b", "0002", 1, 1)                                     # root hub
        self.write(os.path.join(self.devices, "5-1", "idProduct"), "0321")            # vanished mid-scan
        os.makedirs(os.path.join(self.devices, "1-1" + sysfs.IFACE_SEP + "1.0"))
        nodes = sysfs.scan(self.sys, self.dev)
        self.assertEqual([n.sysname for n in nodes], ["1-1", "1-1.2", "2-1", "3-1", "4-1"])
        a = nodes[0]
        self.assertEqual((a.busnum, a.devnum, a.speed_mbps, a.behind_hub, a.vid, a.pid, a.descriptors),
                         (1, 3, 480, False, 0x0BB4, 0x0321, hs))
        self.assertEqual(a.devpath, os.path.join(self.dev, "bus", "usb", "001", "003"))
        self.assertEqual((nodes[1].behind_hub, nodes[1].speed_mbps, nodes[1].descriptors), (True, 5000, b""))
        self.assertEqual([sysfs.kind(n) for n in nodes], ["vft", "vft", "htc", "focus3", None])
        self.assertEqual(nodes[4].speed_mbps, 1)
        self.assertEqual(sysfs.find_vft(self.sys, self.dev).sysname, "1-1")
        self.assertEqual(sysfs.scan(os.path.join(self.tmp, "missing"), self.dev), [])
        self.assertIsNone(sysfs.find_vft(os.path.join(self.tmp, "missing"), self.dev))

    def test_interface_drivers_from_file_and_symlink(self):
        sep = sysfs.IFACE_SEP
        self.write(os.path.join(self.devices, "1-1" + sep + "1.0", "driver"), "uvcvideo")
        os.makedirs(os.path.join(self.devices, "1-1" + sep + "1.1"))
        os.makedirs(os.path.join(self.devices, "1-2" + sep + "1.0"))
        self.assertEqual(sysfs.interface_drivers(self.sys, "1-1"), {0: "uvcvideo", 1: None})
        if not symlinks_work(self.tmp):
            self.skipTest("symlinks are not available here; the file form was checked")
        target = os.path.join(self.sys, "bus", "usb", "drivers", "usbfs")
        os.makedirs(target)
        os.symlink(target, os.path.join(self.devices, "1-1" + sep + "1.1", "driver"))
        self.assertEqual(sysfs.interface_drivers(self.sys, "1-1"), {0: "uvcvideo", 1: "usbfs"})

    def test_find_owners_from_fake_proc(self):
        node = os.path.join(self.dev, "bus", "usb", "001", "003")
        self.write(node, b"")
        self.write(os.path.join(self.proc, "812", "comm"), "vftstream")
        self.write(os.path.join(self.proc, "812", "fd", "7"), node)               # the file form
        self.write(os.path.join(self.proc, "901", "comm"), "other")
        self.write(os.path.join(self.proc, "901", "fd", "1"), os.path.join(self.dev, "null"))
        os.makedirs(os.path.join(self.proc, "902"))                               # no fd dir (another user)
        self.write(os.path.join(self.proc, "self", "comm"), "python3")
        want = [(812, "vftstream")]
        if symlinks_work(self.tmp):
            self.write(os.path.join(self.proc, "1000", "comm"), "frameface")
            os.makedirs(os.path.join(self.proc, "1000", "fd"))
            os.symlink(node, os.path.join(self.proc, "1000", "fd", "5"))
            want.append((1000, "frameface"))
        self.assertEqual(sysfs.find_owners(node, self.proc), want)
        self.assertEqual(sysfs.find_owners(node, os.path.join(self.tmp, "noproc")), [])

    def test_typec_bracket_parsing(self):
        port = os.path.join(self.sys, "class", "typec", "port0")
        self.assertEqual(sysfs.read_typec(self.sys), dict.fromkeys(
            ("data_role", "power_role", "port_type", "power_operation_mode", "partner")))
        for key, value in (("data_role", "host [device]"), ("power_role", "[source] sink"),
                           ("port_type", "[dual] source sink"), ("power_operation_mode", "usb_power_delivery")):
            self.write(os.path.join(port, key), value)
        self.assertEqual(sysfs.read_typec(self.sys), {"data_role": "device", "power_role": "source", "port_type": "dual",
                                                      "power_operation_mode": "usb_power_delivery", "partner": False})
        os.makedirs(os.path.join(self.sys, "class", "typec", "port0-partner"))
        self.assertTrue(sysfs.read_typec(self.sys)["partner"])
        self.assertEqual((sysfs.bracketed("[host] device"), sysfs.bracketed("dual"), sysfs.bracketed(None)),
                         ("host", "dual", None))

    def test_battery(self):
        ps = os.path.join(self.sys, "class", "power_supply")
        self.assertEqual(sysfs.read_battery(self.sys), {"pct": None, "charging": None})
        self.write(os.path.join(ps, "ac", "type"), "Mains")
        self.write(os.path.join(ps, "ac", "status"), "Charging")
        self.write(os.path.join(ps, "battery", "type"), "Battery")
        self.write(os.path.join(ps, "battery", "capacity"), "71")
        for status, charging in (("Discharging", False), ("Not charging", False), ("Charging", True), ("Full", True),
                                 ("Unknown", None)):
            self.write(os.path.join(ps, "battery", "status"), status)
            self.assertEqual(sysfs.read_battery(self.sys), {"pct": 71, "charging": charging}, status)
        self.write(os.path.join(ps, "battery", "capacity"), "abc")
        self.assertIsNone(sysfs.read_battery(self.sys)["pct"])

    def test_power_state(self):
        d = os.path.join(self.devices, "1-1", "power")
        self.assertEqual(sysfs.read_power(self.sys, "1-1"), dict.fromkeys(
            ("control", "autosuspend_delay_ms", "runtime_status", "runtime_suspended_time")))
        for key, value in (("control", "auto"), ("autosuspend_delay_ms", "2000"), ("runtime_status", "suspended"),
                           ("runtime_suspended_time", "1234")):
            self.write(os.path.join(d, key), value)
        self.assertEqual(sysfs.read_power(self.sys, "1-1"), {"control": "auto", "autosuspend_delay_ms": 2000,
                                                             "runtime_status": "suspended",
                                                             "runtime_suspended_time": 1234})

    def test_node_access(self):
        node = os.path.join(self.dev, "bus", "usb", "001", "003")
        self.write(node, b"")
        os.chmod(node, 0o664)
        a = sysfs.node_access(node)
        self.assertEqual((a["exists"], a["writable"]), (True, True))
        if os.name == "posix":
            self.assertEqual((a["mode"], a["mode_str"][-9:], a["uid"]), ("0664", "rw-rw-r--", os.getuid()))
            self.assertIsInstance(a["group"], (str, type(None)))
        os.chmod(node, 0o444)
        self.addCleanup(os.chmod, node, 0o664)
        if os.name != "posix" or os.geteuid() != 0:                    # root may write anyway
            self.assertFalse(sysfs.node_access(node)["writable"])
        self.assertEqual(sysfs.node_access(os.path.join(self.dev, "nope"))["exists"], False)

    def test_rig_tree_reads_like_sysfs(self):
        rig = make_rig(VftModel(kernel_driver="uvcvideo", other_usbfs_owner=(812, "vftstream")))
        self.addCleanup(rig.close)
        node = rig.node()
        self.assertEqual((node.sysname, node.devnum, node.speed_mbps, node.descriptors),
                         ("1-1", 3, 480, build_expected("hs")))
        self.assertEqual(sysfs.interface_drivers(rig.sys_root, "1-1"), {0: "uvcvideo", 1: "uvcvideo"})
        self.assertEqual(sysfs.find_owners(node.devpath, rig.proc_root), [(812, "vftstream")])
        self.assertEqual(sysfs.read_power(rig.sys_root, "1-1"), {"control": "on", "autosuspend_delay_ms": 2000,
                                                                 "runtime_status": "active",
                                                                 "runtime_suspended_time": 0})
        self.assertEqual((sysfs.read_typec(rig.sys_root)["data_role"], sysfs.read_battery(rig.sys_root)),
                         ("host", {"pct": 71, "charging": False}))
        self.assertTrue(sysfs.node_access(node.devpath)["writable"])
        rig.kernel.unplug(replug_after=1.0)
        self.assertIsNone(rig.node())
        rig.clock.advance(1.0)
        self.assertEqual((rig.node().devnum, rig.node().devpath[-3:]), (4, "004"))


if __name__ == "__main__":
    unittest.main()
