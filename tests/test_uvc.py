import contextlib
import errno
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ptzcam import uvc

_spec = importlib.util.spec_from_file_location("uvc_controls", Path(__file__).resolve().parent.parent / "tools"
                                               / "uvc_controls.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


def fake_sysfs(root: Path, nodes):
    """A /sys/class/video4linux look-alike: nodes are (videoN, camera name, index, driver)."""
    for driver in {n[3] for n in nodes}:
        (root / "drivers" / driver).mkdir(parents=True)
    for node, name, index, driver in nodes:
        d = root / "class" / node
        (d / "device").mkdir(parents=True)
        os.symlink(root / "drivers" / driver, d / "device" / "driver")
        (d / "name").write_text(name + "\n")
        (d / "index").write_text(f"{index}\n")
    return root / "class"


class KeyTests(unittest.TestCase):
    def test_names_as_v4l2_ctl_spells_them(self):
        self.assertEqual(uvc.key("Power Line Frequency"), "power_line_frequency")
        self.assertEqual(uvc.key("White Balance Temperature, Auto"), "white_balance_temperature_auto")
        self.assertEqual(uvc.key("Zoom, Absolute"), "zoom_absolute")
        self.assertEqual(uvc.key("  Pan (Absolute) "), "pan_absolute")
        self.assertEqual(uvc.key("power_line_frequency"), "power_line_frequency")


class FindDeviceTests(unittest.TestCase):
    NODES = [("video0", "Unite 200", 0, "uvcvideo"), ("video1", "Unite 200", 1, "uvcvideo"),
             ("video10", "Avaya HC020", 0, "uvcvideo"), ("video11", "Avaya HC020", 1, "uvcvideo"),
             ("video19", "rpi-hevc-dec", 0, "rpi-hevc-dec"), ("video9", "USB Webcam", 0, "uvcvideo")]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = mock.patch.object(uvc, "SYSFS", fake_sysfs(self.tmp, self.NODES))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_by_name_any_case_picture_node_only(self):
        self.assertEqual(uvc.find_device("avaya"), "/dev/video10")
        self.assertEqual(uvc.find_device("AVAYA hc020"), "/dev/video10")
        self.assertEqual(uvc.find_device("Unite"), "/dev/video0")

    def test_first_camera_in_number_order(self):
        self.assertEqual(uvc.find_device(None), "/dev/video0")
        self.assertEqual(uvc.find_device("u"), "/dev/video0")  # 'Unite 200' and 'USB Webcam' both match
        self.assertEqual(uvc.find_device("webcam"), "/dev/video9")
        (uvc.SYSFS / "video0" / "index").write_text("1\n")  # by number video9 is next, by name video10 would be
        self.assertEqual(uvc.find_device(None), "/dev/video9")

    def test_unknown_name_lists_the_connected_cameras(self):
        with self.assertRaises(LookupError) as cm:
            uvc.find_device("logitech")
        self.assertIn("Unite 200, USB Webcam, Avaya HC020", str(cm.exception))
        with self.assertRaises(LookupError):
            uvc.find_device("hevc")  # not a UVC camera

    def test_no_camera(self):
        with mock.patch.object(uvc, "SYSFS", self.tmp / "empty"):
            with self.assertRaises(LookupError) as cm:
                uvc.find_device("avaya")
        self.assertIn("no USB camera found", str(cm.exception))

    def test_absolute_path_returned_as_is(self):
        dev = self.tmp / "usb-2e7e_Avaya_HC020_20VH28214151-video-index0"
        os.symlink(self.tmp / "class" / "video10", dev)
        self.assertEqual(uvc.find_device(str(dev)), str(dev))
        with self.assertRaises(LookupError):
            uvc.find_device(str(self.tmp / "unplugged-video-index0"))


class CliTests(unittest.TestCase):
    """tools/uvc_controls.py: camera-compare reads the last word of `get`, and a refusal exits with 1."""
    CTRLS = [{"id": 0x980918, "type": 3, "name": "Power Line Frequency", "min": 0, "max": 2, "step": 1,
              "default": 1, "flags": 0}]

    def run_cli(self, *argv, find=None, set_error=None):
        out = io.StringIO()
        with mock.patch.object(cli, "find_device", side_effect=find, return_value="/dev/video2"), \
                mock.patch.object(cli, "controls", return_value=self.CTRLS), \
                mock.patch.object(cli, "get", return_value=0), \
                mock.patch.object(cli, "set_", side_effect=set_error), \
                mock.patch.object(cli, "os"), \
                mock.patch.object(sys, "argv", ["uvc_controls.py", *argv]), contextlib.redirect_stdout(out):
            try:
                cli.main()
            except SystemExit as e:
                return e.code, out.getvalue()
        return 0, out.getvalue()

    def test_get_set_and_refusals(self):
        self.assertEqual(self.run_cli("--camera", "avaya", "get", "power_line_frequency"),
                         (0, "power_line_frequency = 0\n"))
        self.assertEqual(self.run_cli("set", "power_line_frequency=0"), (0, "power_line_frequency: 0 -> 0\n"))
        code, out = self.run_cli("set", "power_line_frequency=7", set_error=OSError(errno.EINVAL, "Invalid argument"))
        self.assertEqual((code, out), (1, "power_line_frequency: the camera refused 7 (Invalid argument: that value "
                                          "or mode is not supported)\n"))
        code, _ = self.run_cli("get", "exposure")
        self.assertEqual(code, "no control 'exposure'; run without arguments to list them")
        code, _ = self.run_cli("--camera", "logitech", find=LookupError("no USB camera named 'logitech'"))
        self.assertEqual(code, "no USB camera named 'logitech'")


if __name__ == "__main__":
    unittest.main()
