import unittest

import cv2
import numpy as np

from ptzcam.ptz import ZOOM_RANGE, ZOOM_RATIO, mag_at, pos_for
from ptzcam.zoomcal import estimate_scale

W, H = 1280, 720


def scene(seed=0):
    """A street-like picture with plenty of corners: random boxes, discs and lines on a gradient."""
    rng = np.random.default_rng(seed)
    img = np.dstack([np.tile(np.linspace(40, 200, W, dtype=np.uint8), (H, 1))] * 3).copy()
    for _ in range(250):
        c = tuple(int(v) for v in rng.integers(0, 256, 3))
        x, y = int(rng.integers(0, W)), int(rng.integers(0, H))
        kind = rng.integers(0, 3)
        if kind == 0:
            cv2.rectangle(img, (x, y), (x + int(rng.integers(8, 90)), y + int(rng.integers(8, 60))), c, -1)
        elif kind == 1:
            cv2.circle(img, (x, y), int(rng.integers(4, 30)), c, -1)
        else:
            cv2.line(img, (x, y), (int(rng.integers(0, W)), int(rng.integers(0, H))), c, 2)
    return img


def zoomed(img, s, dx=6, dy=-4):
    """The same view zoomed in by `s`, about a point slightly off centre (a real lens's optical axis)."""
    cw, ch = W / s, H / s
    x0, y0 = W / 2 + dx - cw / 2, H / 2 + dy - ch / 2
    m = np.float32([[s, 0, -x0 * s], [0, s, -y0 * s]])
    return cv2.warpAffine(img, m, (W, H), flags=cv2.INTER_LINEAR)


class EstimateScaleTests(unittest.TestCase):
    def test_measures_known_zoom_steps(self):
        img = scene()
        for s in (1.1, 1.3, 1.6):
            est = estimate_scale(img, zoomed(img, s))
            self.assertIsNotNone(est, s)
            self.assertAlmostEqual(est, s, delta=0.02 * s)

    def test_featureless_pictures_give_no_answer(self):
        white = np.full((H, W, 3), 253, np.uint8)       # the washed-out camera
        self.assertIsNone(estimate_scale(white, white))


class CurveTests(unittest.TestCase):
    CURVE = [[0, 1.0], [4096, 1.8], [8192, 3.5], [12288, 7.0], [16384, 12.5]]

    def test_measured_curve_round_trips_and_clamps(self):
        for pos in (0, 1000, 4096, 6000, 9000, 16384):
            self.assertAlmostEqual(pos_for(mag_at(pos, self.CURVE), self.CURVE), pos, delta=1)
        self.assertAlmostEqual(mag_at(8192, self.CURVE), 3.5)
        self.assertAlmostEqual(mag_at(6144, self.CURVE), (1.8 * 3.5) ** 0.5, places=4)  # log-linear between points
        self.assertEqual(pos_for(100.0, self.CURVE), 16384)                              # beyond the lens: full zoom
        self.assertEqual(pos_for(0.5, self.CURVE), 0)

    def test_assumed_model_without_a_curve(self):
        self.assertAlmostEqual(mag_at(ZOOM_RANGE, []), ZOOM_RATIO)
        self.assertAlmostEqual(mag_at(0, []), 1.0)
        self.assertEqual(pos_for(mag_at(5000, []), []), 5000)


if __name__ == "__main__":
    unittest.main()
