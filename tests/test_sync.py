import math
import tempfile
import unittest
from pathlib import Path

import numpy as np

from ptzcam.ptz import mag_at
from ptzcam.sync import SyncMap

HFOV, VFOV = math.radians(90.0), 2 * math.atan(math.tan(math.radians(90.0) / 2) * 9 / 16)
K1 = -0.12  # barrel distortion of the eye's wide lens
UNITS = 14.4  # PTZ units per degree
P0, T0 = 20.0, -300.0
PTZ_VFOV_CAL = 18.0  # the PTZ's vertical field of view (degrees) at the calibration zoom


def _rot(yaw, pitch, roll):
    y, p, r = (math.radians(a) for a in (yaw, pitch, roll))
    ry = np.array([[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]])
    rp = np.array([[1, 0, 0], [0, math.cos(p), -math.sin(p)], [0, math.sin(p), math.cos(p)]])
    rr = np.array([[math.cos(r), -math.sin(r), 0], [math.sin(r), math.cos(r), 0], [0, 0, 1]])
    return ry @ rp @ rr


R = _rot(2.0, -1.5, 1.0)


def true_angles(u, v):
    """Eye position -> (pan, tilt) in degrees for the simulated pair (undistorts the eye's lens numerically)."""
    xd = (u - 0.5) * 2 * math.tan(HFOV / 2)
    yd = (v - 0.5) * 2 * math.tan(VFOV / 2)
    xu, yu = xd, yd
    for _ in range(30):
        r2 = xu * xu + yu * yu
        xu, yu = xd / (1 + K1 * r2), yd / (1 + K1 * r2)
    ray = R @ np.array([xu, yu, 1.0])
    pan = math.degrees(math.atan2(ray[0], ray[2]))
    tilt = -math.degrees(math.atan2(ray[1], math.hypot(ray[0], ray[2])))
    return pan, tilt


def true_ptz(u, v):
    pan, tilt = true_angles(u, v)
    return P0 + UNITS * pan, T0 + UNITS * tilt


def true_fh(u, v):
    """Height of the PTZ's picture in the eye's picture at (u, v), at the calibration zoom."""
    dv = 1e-4
    deg_per_v = abs(true_angles(u, v + dv)[1] - true_angles(u, v - dv)[1]) / (2 * dv)
    return PTZ_VFOV_CAL / deg_per_v


def samples(noise=0.0015, seed=1):
    rng = np.random.default_rng(seed)
    rows = []
    for u in np.linspace(0.12, 0.88, 7):
        for v in np.linspace(0.2, 0.8, 4):
            pan, tilt = true_ptz(u, v)
            fh = true_fh(u, v)
            mu, mv = u + rng.normal(0, noise), v + rng.normal(0, noise)  # where the matching found the PTZ centre
            rows.append([mu, mv, round(pan), round(tilt), fh * 16 / 9 * 0.98, fh])
    return np.array(rows)


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.m = SyncMap(samples(), ptz_zoom=1428, eye_size=(1920, 1080))

    def test_points_the_ptz_accurately_inside_the_calibrated_area(self):
        rng = np.random.default_rng(7)
        errs = []
        for _ in range(200):
            u, v = rng.uniform(0.15, 0.85), rng.uniform(0.22, 0.78)
            pan, tilt = self.m.to_ptz(u, v)
            tp, tt = true_ptz(u, v)
            errs.append(max(abs(pan - tp), abs(tilt - tt)))
        self.assertLess(np.median(errs), 3.0)  # 3 units = 0.2 degrees
        self.assertLess(max(errs), 8.0)

    def test_eye_and_ptz_maps_agree(self):
        for u, v in ((0.3, 0.4), (0.5, 0.5), (0.8, 0.7)):
            back = self.m.to_eye(*self.m.to_ptz(u, v))
            self.assertAlmostEqual(back[0], u, delta=0.004)
            self.assertAlmostEqual(back[1], v, delta=0.004)

    def test_zoom_from_the_eyes_box_height(self):
        u, v, box_h, target = 0.6, 0.35, 0.05, 0.6
        pos = self.m.zoom_for(u, v, box_h, target)
        expected = mag_at(1428, []) * true_fh(u, v) * target / box_h
        self.assertAlmostEqual(mag_at(pos, []) / expected, 1.0, delta=0.03)
        self.assertEqual(self.m.zoom_for(u, v, box_h, target, max_pos=5000), min(pos, 5000))
        self.assertIsNone(self.m.zoom_for(u, v, 0.0, target))

    def test_a_tall_box_never_asks_for_less_than_the_widest_zoom(self):
        self.assertEqual(self.m.zoom_for(0.5, 0.5, 0.9, 0.6), 0)

    def test_tilt_is_clamped_to_what_the_ptz_can_reach(self):
        m = SyncMap(samples(), 1428, (1920, 1080), tilt_limits=(-432.0, 300.0))
        lowest = min(m.to_ptz(u, 0.95)[1] for u in (0.2, 0.5, 0.8))
        self.assertGreaterEqual(lowest, -432)

    def test_knows_where_it_was_calibrated(self):
        self.assertTrue(self.m.covers(0.5, 0.5))
        self.assertFalse(self.m.covers(0.02, 0.05))

    def test_ptz_view_outline_shrinks_as_it_zooms_in(self):
        wide = self.m.ptz_view(*self.m.to_ptz(0.5, 0.5), 1428)
        tele = self.m.ptz_view(*self.m.to_ptz(0.5, 0.5), 8192)
        self.assertAlmostEqual((wide[0] + wide[2]) / 2, 0.5, delta=0.01)
        self.assertGreater(wide[3] - wide[1], 2 * (tele[3] - tele[1]))

    def test_leave_one_out_errors_expose_a_bad_sample(self):
        good = self.m.residuals()
        self.assertLess(good["pan_median"], 3.0)
        s = samples()
        s[5, 2] += 120  # one wrong match during calibration
        bad = SyncMap(s, 1428, (1920, 1080)).residuals()
        self.assertGreater(bad["pan_max"], 60)

    def test_saved_and_loaded_map_answers_the_same(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sync.json"
            self.m.save(p)
            m2 = SyncMap.load(p)
        self.assertEqual(m2.to_ptz(0.4, 0.6), self.m.to_ptz(0.4, 0.6))
        self.assertIsNone(SyncMap.load(Path("/nonexistent/sync.json")))

    def test_few_samples_still_fit_a_plane_and_too_few_are_refused(self):
        s = samples()[[0, 6, 21, 27]]
        m = SyncMap(s, 1428, (1920, 1080))
        pan, tilt = m.to_ptz(0.5, 0.5)
        tp, tt = true_ptz(0.5, 0.5)
        self.assertLess(abs(pan - tp), 60)
        with self.assertRaises(ValueError):
            SyncMap(s[:2], 1428, (1920, 1080))


if __name__ == "__main__":
    unittest.main()
