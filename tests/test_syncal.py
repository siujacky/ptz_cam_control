import unittest

import cv2
import numpy as np

from ptzcam.syncal import calibrate_sync, match_views

WW, WH = 3200, 1800  # the simulated world; the eye sees all of it, the PTZ half of it in each direction
EYE = (1920, 1080)
PX_PER_PAN, PX_PER_TILT = 1.6, 1.5
HOME_CX, HOME_CY = WW / 2, 0.7 * WH  # Home looks at the lower part of the street, at the lowest tilt


def world(seed=3):
    rng = np.random.default_rng(seed)
    img = np.full((WH, WW, 3), 128, np.uint8)
    for _ in range(900):
        x, y = int(rng.integers(0, WW)), int(rng.integers(0, WH))
        c = tuple(int(k) for k in rng.integers(0, 256, 3))
        if rng.random() < 0.5:
            cv2.rectangle(img, (x, y), (x + int(rng.integers(10, 120)), y + int(rng.integers(10, 120))), c, -1)
        else:
            cv2.circle(img, (x, y), int(rng.integers(5, 60)), c, -1)
    for _ in range(120):
        cv2.putText(img, str(int(rng.integers(0, 10 ** 6))), (int(rng.integers(0, WW)), int(rng.integers(0, WH))),
                    cv2.FONT_HERSHEY_SIMPLEX, float(rng.uniform(0.8, 2.5)), (255, 255, 255), 2)
    return img


class FakePTZ:
    TILT_MIN, TILT_MAX = -432, 400

    def __init__(self, scene):
        self.scene, self.pan, self.tilt, self.zoom = scene, 0, -431, 1428
        self.rng = np.random.default_rng(5)

    def move(self, pan, tilt):
        self.pan, self.tilt = int(max(-1000, min(1000, pan))), int(max(self.TILT_MIN, min(self.TILT_MAX, tilt)))

    def position(self):
        return self.pan, self.tilt, self.zoom

    def zoom_to(self, pos):
        self.zoom = pos

    def home(self):
        self.pan, self.tilt = 0, -431

    def centre(self, pan=None, tilt=None):
        pan = self.pan if pan is None else pan
        tilt = self.tilt if tilt is None else tilt
        return HOME_CX + PX_PER_PAN * pan, HOME_CY - PX_PER_TILT * (tilt + 431)

    def frame(self):
        cx, cy = self.centre()
        m = np.float32([[1, 0, cx - WW / 4], [0, 1, cy - WH / 4]])  # the world region (WW/2 x WH/2) centred on (cx, cy)
        view = cv2.warpAffine(self.scene, m, (WW // 2, WH // 2), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=(20, 20, 20))
        view = cv2.resize(view, EYE, interpolation=cv2.INTER_AREA)
        # Another camera: different exposure and colour, a little blur and noise.
        view = cv2.GaussianBlur(cv2.convertScaleAbs(view, alpha=0.8, beta=25), (0, 0), 0.8)
        return np.clip(view + self.rng.normal(0, 3, view.shape), 0, 255).astype(np.uint8)


class MatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scene = world()
        cls.eye = cv2.resize(cls.scene, EYE, interpolation=cv2.INTER_AREA)

    def test_finds_the_ptz_view_inside_the_eyes_picture(self):
        ptz = FakePTZ(self.scene)
        ptz.move(-300, -100)
        u, v, fw, fh, n = match_views(ptz.frame(), self.eye)
        cx, cy = ptz.centre()
        self.assertAlmostEqual(u, cx / WW, delta=0.004)
        self.assertAlmostEqual(v, cy / WH, delta=0.004)
        self.assertAlmostEqual(fw, 0.5, delta=0.015)
        self.assertAlmostEqual(fh, 0.5, delta=0.015)
        self.assertGreaterEqual(n, 25)

    def test_unrelated_pictures_do_not_match(self):
        noise = np.random.default_rng(9).integers(0, 256, (1080, 1920, 3), dtype=np.uint8)
        self.assertIsNone(match_views(noise, self.eye))
        self.assertIsNone(match_views(np.zeros((1080, 1920, 3), np.uint8), self.eye))


class CalibrateTest(unittest.TestCase):
    def test_sweep_gives_a_map_that_points_the_ptz_where_the_eye_looks(self):
        scene = world()
        eye = cv2.resize(scene, EYE, interpolation=cv2.INTER_AREA)
        ptz = FakePTZ(scene)
        m = calibrate_sync(ptz, ptz.frame, lambda: eye, zoom=0, grid=(4, 3), settle_s=0.0, log=lambda *_: None)
        self.assertEqual((ptz.pan, ptz.tilt), (0, -431))  # left at Home
        self.assertEqual(m.tilt_limits[0], -432)  # found the lowest tilt
        for u, v in ((0.3, 0.3), (0.5, 0.5), (0.7, 0.6), (0.2, 0.65)):
            pan, tilt = m.to_ptz(u, v)
            true_pan = (u * WW - HOME_CX) / PX_PER_PAN
            true_tilt = (HOME_CY - v * WH) / PX_PER_TILT - 431
            self.assertLess(abs(pan - true_pan), 8, (u, v))
            self.assertLess(abs(tilt - true_tilt), 8, (u, v))
        fp = m.footprint(0.5, 0.5)
        self.assertAlmostEqual(fp[1], 0.5, delta=0.02)


if __name__ == "__main__":
    unittest.main()
