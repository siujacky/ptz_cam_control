"""AutoTracker eye mode: the fixed eye camera picks the target and the sync map aims the PTZ at it."""
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from ptzcam import ptz
from ptzcam.ptz import EYE_ID_BASE, AutoTracker, mag_at, pos_for
from ptzcam.sync import SyncMap
from tests.helpers import FakeConfig
from tests.test_autotrack import Clock

PAN_PER_U, TILT_PER_V = 2000.0, -900.0  # a linear pair of cameras: pan = -1000 + 2000 u, tilt = 300 - 900 v


def linear_map(fw=0.5, fh=0.5, tilt_limits=(-432.0, 400.0)):
    rows = [[u, v, -1000 + PAN_PER_U * u, 300 + TILT_PER_V * v, fw, fh]
            for u in np.linspace(0.05, 0.95, 7) for v in np.linspace(0.05, 0.95, 5)]
    return SyncMap(np.array(rows), ptz_zoom=0, eye_size=(1920, 1080), tilt_limits=tilt_limits)


def true_ptz(u, v):
    return -1000 + PAN_PER_U * u, 300 + TILT_PER_V * v


class FakePTZ:
    """Absolute moves and zooms arrive at once; records what was sent."""

    def __init__(self):
        self.enabled = self.can_point = True
        self.at_home = True
        self.pan, self.tilt, self.zoom_pos = 0.0, -132.0, 0
        self.home_zoom, self.zoom_goal = 0, None
        self.aims, self.goals, self.calls = [], [], []

    def pan_tilt_to(self, pan, tilt, speed=1.0):
        self.aims.append((pan, tilt, speed))
        self.pan, self.tilt, self.at_home = pan, tilt, False
        return True

    def pose_estimate(self):
        return float(self.pan), float(self.tilt)

    @property
    def pt_arrived(self):
        return True

    def zoom_to(self, pos):
        self.goals.append(pos)
        self.zoom_pos = self.zoom_goal = pos

    @property
    def zoom_arrived(self):
        return True

    def home(self):
        self.calls.append("home")
        self.pan, self.tilt, self.zoom_pos, self.at_home = 0.0, -132.0, 0, True

    def stop(self):
        self.calls.append("stop")

    def stop_pantilt(self):
        self.calls.append("stop_pantilt")

    def move(self, *a, **k):
        self.calls.append("move")

    def is_moving(self):
        return False

    def quiet_since(self):
        return 0.0


def eye_track(tid, u, v, ts, category="person", h=0.08, vel=(0.0, 0.0), moving=True, counted=True, event_id=None):
    w = h * 0.4
    return SimpleNamespace(id=EYE_ID_BASE + tid, category=category, label=category, confirmed=True, last_ts=ts,
                           moving=moving, score=0.8, max_score=0.8, counted=counted, in_zone=True,
                           box=np.array([u - w / 2, v - h / 2, u + w / 2, v + h / 2]), center=(u, v), area=w * h,
                           vel=np.array(vel, float), first_ts=0.0, event_id=event_id)


def ptz_track(tid, x, y, ts, category="person", h=0.5):
    w = h * 0.4
    return SimpleNamespace(id=tid, category=category, label=category, confirmed=True, last_ts=ts, moving=True,
                           score=0.8, max_score=0.8, counted=False, in_zone=False,
                           box=np.array([x - w / 2, y - h / 2, x + w / 2, y + h / 2]), center=(x, y), area=w * h,
                           vel=np.zeros(2), first_ts=0.0, event_id=None)


class EyeFollowTests(unittest.TestCase):
    def setUp(self):
        self.cfg = FakeConfig(ptz={"autotrack": True, "autozoom": True, "target_size": 0.6, "max_zoom_pos": 12800,
                                   "return_home_seconds": 3.0, "pt_rate": 250.0},
                              eye={"enabled": True, "follow": True})
        self.ptz = FakePTZ()
        self.clock = Clock()
        patcher = mock.patch.object(ptz, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.at = AutoTracker(self.ptz, self.cfg)
        self.at.sync, self.at.eye_active = linear_map(), True

    def eye(self, tracks, ts):
        self.clock.t = ts
        self.at.update_eye(tracks, ts)

    def run_walker(self, seconds, u0=0.3, v=0.5, vel=(0.02, 0.0), h=0.08, dt=1 / 15):
        ts = 1.0
        for _ in range(int(seconds / dt)):
            self.eye([eye_track(1, u0 + vel[0] * (ts - 1.0), v, ts, vel=vel, h=h)], ts)
            ts += dt
        return ts

    def test_eye_mode_needs_a_sync_map_and_a_usable_eye(self):
        self.eye([eye_track(1, 0.4, 0.5, 1.0)], 1.0)
        self.assertEqual(self.at.mode, "eye")
        self.at.eye_active = False
        self.at.update([], 1.1)
        self.assertEqual(self.at.mode, "ptz")
        self.assertIsNone(self.at.target_id)  # eye ids mean nothing to the PTZ tracker

    def test_aims_through_the_map_where_the_walker_will_be(self):
        self.eye([eye_track(1, 0.3, 0.5, 1.0, vel=(0.02, 0.0))], 1.0)
        pan, tilt, speed = self.ptz.aims[0]
        lead_pan = true_ptz(0.3, 0.5)[0]
        self.assertGreater(pan, lead_pan)  # ahead of it, in its direction of travel
        self.assertLess(pan - lead_pan, PAN_PER_U * 0.02 * 2.0)
        self.assertAlmostEqual(tilt, true_ptz(0.3, 0.5)[1], delta=3)
        self.assertEqual(speed, 1.0)  # a fresh target is swung to at full speed

    def test_pursuit_re_aims_at_a_limited_rate(self):
        self.run_walker(3.0)
        aims = len(self.ptz.aims)
        self.assertGreater(aims, 3)
        self.assertLessEqual(aims, 3.0 / self.at.AIM_PERIOD_S + 2)

    def test_people_come_first_and_fast_cars_are_left_alone(self):
        car = eye_track(2, 0.6, 0.5, 1.0, category="vehicle", vel=(0.5, 0.0))  # needs 1000 units/s: too fast
        self.eye([car], 1.0)
        self.assertIsNone(self.at.target_id)
        slow_car = eye_track(3, 0.6, 0.5, 1.1, category="vehicle", vel=(0.02, 0.0))
        self.eye([slow_car], 1.1)
        self.assertEqual(self.at.target_id, slow_car.id)
        walker = eye_track(4, 0.4, 0.5, 1.2)
        self.eye([eye_track(3, 0.602, 0.5, 1.2, category="vehicle", vel=(0.02, 0.0)), walker], 1.2)
        self.assertEqual(self.at.target_id, walker.id)

    def test_zoom_is_one_step_from_the_eyes_box_once_pointed(self):
        ts = 1.0
        for _ in range(30):  # a person standing at the centre, 0.05 of the eye frame tall
            self.eye([eye_track(1, 0.5, 0.5, ts, h=0.05)], ts)
            ts += 1 / 15
        self.assertEqual(len(self.ptz.goals), 1)
        expected = pos_for(mag_at(0, []) * 0.5 * 0.6 / 0.05, [])  # footprint 0.5 x target 0.6 / box 0.05
        self.assertAlmostEqual(self.ptz.goals[0], expected, delta=40)

    def test_a_moving_target_gets_less_zoom_so_it_stays_in_frame(self):
        ts = 1.0
        for i in range(30):  # 0.06/s drifts 90 units between aims: more than FIT (0.3) of a 6x field (167 units)
            self.eye([eye_track(1, 0.45 + 0.06 * i / 15, 0.5, ts, h=0.05, vel=(0.06, 0.0))], ts)
            ts += 1 / 15
        still = pos_for(mag_at(0, []) * 0.5 * 0.6 / 0.05, [])
        self.assertTrue(self.ptz.goals)
        self.assertLess(max(self.ptz.goals), still - 500)

    def test_below_the_lowest_tilt_it_zooms_only_as_far_as_still_fits(self):
        ts = 1.0
        for _ in range(30):  # v = 0.9 needs tilt -510, below the -432 limit
            self.eye([eye_track(1, 0.5, 0.9, ts, h=0.05)], ts)
            ts += 1 / 15
        self.assertGreaterEqual(min(a[1] for a in self.ptz.aims), -432)
        unclamped = pos_for(mag_at(0, []) * 0.5 * 0.6 / 0.05, [])
        self.assertLess(self.ptz.goals[-1], unclamped)
        mag = mag_at(self.ptz.goals[-1], [])
        field_h = 0.5 * abs(TILT_PER_V) / mag  # tilt units the frame covers at that zoom
        short = 432 + (300 + TILT_PER_V * 0.9)
        self.assertLessEqual(-short + 0.5 * 0.05 * abs(TILT_PER_V), field_h / 2 + 1)

    def test_a_lost_vehicle_goes_home_and_a_lost_person_is_waited_for(self):
        car = eye_track(1, 0.5, 0.5, 1.0, category="vehicle", vel=(0.02, 0.0))
        self.eye([car], 1.0)
        self.eye([], 1.3)
        self.assertNotIn("home", self.ptz.calls)
        self.eye([], 1.7)
        self.assertIn("home", self.ptz.calls)
        self.ptz.calls.clear()
        self.eye([eye_track(2, 0.4, 0.5, 2.0)], 2.0)
        self.eye([], 3.0)  # coasting
        self.assertNotIn("home", self.ptz.calls)
        self.eye([], 5.5)  # past return_home_seconds
        self.assertIn("home", self.ptz.calls)

    def test_ptz_picture_corrects_the_aim_and_joins_the_event(self):
        self.eye([eye_track(1, 0.5, 0.5, 1.0, event_id=42)], 1.0)
        self.clock.t = 2.0  # long after the aim: the PTZ picture shows this pose
        # The PTZ sees the person 0.1 of its frame right of centre: the map is off by 0.1 x 0.5 x 2000 = 100 units.
        p = ptz_track(7, 0.6, 0.5, 2.0, h=0.08 / 0.5)
        self.at.update([p], 2.0)
        self.assertEqual(self.at.ptz_match_id, 7)
        self.assertEqual(p.event_id, 42)
        self.assertTrue(p.counted)  # never counted again by the PTZ
        self.assertGreater(self.at._bias[0], 20)
        self.eye([eye_track(1, 0.5, 0.5, 2.5, event_id=42)], 2.5)
        self.assertGreater(self.ptz.aims[-1][0], true_ptz(0.5, 0.5)[0] + 20)  # aims further right now

    def test_a_click_on_the_ptz_view_follows_that_object(self):
        self.eye([eye_track(1, 0.2, 0.5, 1.0), eye_track(2, 0.8, 0.5, 1.0)], 1.0)
        self.ptz.pan, self.ptz.tilt = true_ptz(0.8, 0.5)  # looking at the second one
        self.at.set_enabled(True, 9)  # PTZ track id 9, clicked on the PTZ view
        self.at.update([ptz_track(9, 0.5, 0.5, 1.1)], 1.1)
        self.eye([eye_track(1, 0.2, 0.5, 1.2), eye_track(2, 0.8, 0.5, 1.2)], 1.2)
        self.assertEqual(self.at.target_id, EYE_ID_BASE + 2)
        self.assertTrue(self.at.user_locked)

    def test_manual_steering_pauses_eye_mode_too(self):
        self.at.manual_override()
        self.eye([eye_track(1, 0.4, 0.5, 1.0)], 1.0)
        self.assertEqual(self.ptz.aims, [])


if __name__ == "__main__":
    unittest.main()
