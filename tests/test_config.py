import unittest
from pathlib import Path

from ptzcam.config import DEFAULTS, ConfigError, check_known, deep_merge, needs_restart, validate
from ptzcam.detector import hef_kind

ROOT = Path(__file__).resolve().parent.parent
HAILO = hef_kind(ROOT / "models" / "yolov11m.hef") is not None if (ROOT / "models" / "yolov11m.hef").exists() else False


class LowlightConfigTests(unittest.TestCase):
    def check(self, patch, paths=False):
        return validate(deep_merge(DEFAULTS, patch), ROOT, check_paths=paths)

    def test_defaults_and_rules(self):
        ll = self.check({})["lowlight"]
        self.assertEqual((ll["mode"], ll["detect"], ll["save_clips"]), ("auto", False, True))
        for bad in ({"mode": "dark"}, {"night_luma": 80}, {"model": "models/nope.hef"}, {"detect": "yes"},
                    {"model": "../config.json"}):
            with self.assertRaises(ConfigError, msg=bad):
                self.check({"lowlight": bad})
        self.assertEqual(self.check({"lowlight": {"model": ""}})["lowlight"]["model"], "")
        self.assertTrue(needs_restart({("lowlight", "model")}))
        br = self.check({})["brightness"]
        self.assertEqual((br["auto"], br["detect"]), (True, True))
        with self.assertRaises(ConfigError):
            self.check({"brightness": {"auto": "yes"}})
        self.assertFalse(needs_restart({("brightness", "auto")}))

    def test_zoom_curve_rules(self):
        good = [[0, 1.0], [8192, 3.4], [16384, 12.1]]
        self.assertEqual(self.check({"ptz": {"zoom_curve": good}})["ptz"]["zoom_curve"], good)
        for bad in ([[0, 1.0]], [[0, 1.0], [8192, 0.9]], [[0, 1.0], [0, 2.0]], [[0, 0.5], [100, 2.0]],
                    [[0, 1.0], [20000, 2.0]], "x"):
            with self.assertRaises(ConfigError, msg=bad):
                self.check({"ptz": {"zoom_curve": bad}})
        with self.assertRaises(ConfigError):  # replaced by the measured lens; the API refuses it now
            check_known({"ptz": {"zoom_speed": 3}})
        self.assertFalse(needs_restart({("lowlight", "mode")}))  # applied per frame, no restart

    @unittest.skipUnless(HAILO, "HailoRT and the model files are needed")
    def test_model_kinds_are_checked_on_change(self):
        self.assertEqual(hef_kind(ROOT / "models" / "yolov11m.hef"), "detector")
        self.assertEqual(hef_kind(ROOT / "models" / "lowlight" / "zero_dce.hef"), "enhancer")
        with self.assertRaises(ConfigError):  # a detector is not a night-vision model
            self.check({"lowlight": {"model": "models/yolov8m.hef"}}, paths=True)
        self.check({"lowlight": {"model": "models/lowlight/zero_dce_pp.hef"}}, paths=True)


if __name__ == "__main__":
    unittest.main()
