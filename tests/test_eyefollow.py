"""AutoTracker eye mode: the fixed eye camera picks the target and the sync map aims the PTZ at it."""
import math
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


def eye_track(tid, u, v, ts, category="person", h=0.08, vel=(0.0, 0.0), moving=True, counted=True, event_id=None,
              in_zone=True, first_ts=0.0):
    w = h * 0.4
    t = SimpleNamespace(id=EYE_ID_BASE + tid, category=category, label=category, confirmed=True, last_ts=ts,
                        moving=moving, score=0.8, max_score=0.8, counted=counted, in_zone=in_zone,
                        box=np.array([u - w / 2, v - h / 2, u + w / 2, v + h / 2]), center=(u, v), area=w * h,
                        vel=np.array(vel, float), first_ts=first_ts, event_id=event_id, inherited=None)

    def inherit(old):  # as Track.inherit: the count and event move over
        t.inherited = old.id
        if old.counted and not t.counted:
            t.counted, t.event_id = True, old.event_id
    t.inherit = inherit
    return t


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
        self.cfg.data["ptz"]["autozoom"] = False
        ts = 1.0
        while ts < 1.99:  # the eye sees them all along
            self.eye([eye_track(1, 0.5, 0.5, ts, event_id=42)], ts)
            ts += 1 / 15
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
        self.ptz.pan, self.ptz.tilt = true_ptz(0.8, 0.5)  # looking at the second one...
        self.clock.t = 2.0  # ...for longer than the PTZ picture lags
        self.at.set_enabled(True, 9)  # PTZ track id 9, clicked on the PTZ view
        self.at.update([ptz_track(9, 0.5, 0.5, 2.0)], 2.0)
        self.eye([eye_track(1, 0.2, 0.5, 2.1), eye_track(2, 0.8, 0.5, 2.1)], 2.1)
        self.assertEqual(self.at.target_id, EYE_ID_BASE + 2)
        self.assertTrue(self.at.user_locked)

    def test_manual_steering_pauses_eye_mode_too(self):
        self.at.manual_override()
        self.eye([eye_track(1, 0.4, 0.5, 1.0)], 1.0)
        self.assertEqual(self.ptz.aims, [])


class EyeFollowFixesTests(unittest.TestCase):
    """Behaviour the dual-camera review asked for (each test fails on the code before the fix)."""
    setUp, eye = EyeFollowTests.setUp, EyeFollowTests.eye

    def test_idle_away_from_home_goes_home_after_return_home_seconds(self):
        self.ptz.at_home, self.ptz.pan = False, 600.0  # the user steered it, then let go
        self.clock.t = 1.0
        self.at.manual_override()  # pauses 10 s; the countdown starts after that
        for k in range(60):
            self.eye([], 11.5 + k * 0.1)
        self.assertIn("home", self.ptz.calls)

    def test_a_target_the_eye_lost_is_not_matched_in_the_ptz_picture(self):
        self.eye([eye_track(1, 0.5, 0.5, 1.0, event_id=42)], 1.0)
        self.eye([], 1.5)
        self.clock.t = 2.0
        p = ptz_track(7, 0.5, 0.5, 2.0, h=0.08 / 0.5)  # a passer-by right where the target was
        self.at.update([p], 2.0)
        self.assertIsNone(self.at.ptz_match_id)
        self.assertIsNone(p.event_id)
        self.assertEqual(self.at._bias, [0.0, 0.0])

    def test_targets_are_picked_inside_the_zone_that_counts(self):
        self.at.eye_zone = [[0.0, 0.0], [0.3, 0.0], [0.3, 1.0], [0.0, 1.0]]
        outside = eye_track(1, 0.6, 0.5, 1.0, counted=False, in_zone=False, vel=(0.02, 0.0))
        self.eye([outside], 1.0)
        self.assertIsNone(self.at.target_id)  # it can never become an event: the PTZ stays Home
        self.assertEqual(self.ptz.aims, [])
        inside = eye_track(2, 0.2, 0.5, 1.1, counted=False, in_zone=True, vel=(0.02, 0.0))
        self.eye([outside, inside], 1.1)
        self.assertEqual(self.at.target_id, EYE_ID_BASE + 2)

    def test_a_target_past_the_tilt_limit_is_not_re_aimed_every_frame(self):
        self.cfg.data["ptz"]["autozoom"] = False
        self.ptz.zoom_pos = 9000  # zoomed in: a small field, so "far off" is a short way
        ts = 1.0
        for _ in range(45):  # standing below the lowest tilt (v = 0.95 needs tilt -555)
            self.eye([eye_track(1, 0.5, 0.95, ts, h=0.05)], ts)
            ts += 1 / 15
        self.assertLessEqual(len(self.ptz.aims), 2)
        self.assertGreaterEqual(min(a[1] for a in self.ptz.aims), -432)

    def test_above_the_highest_tilt_it_zooms_only_as_far_as_still_fits(self):
        self.at.sync = linear_map(tilt_limits=(-432.0, 100.0))  # tilt 100 is v = 0.222
        ts = 1.0
        for _ in range(30):
            self.eye([eye_track(1, 0.5, 0.1, ts, h=0.05)], ts)
            ts += 1 / 15
        self.assertLessEqual(max(a[1] for a in self.ptz.aims), 100)
        unclamped = pos_for(mag_at(0, []) * 0.5 * 0.6 / 0.05, [])
        self.assertLess(self.ptz.goals[-1], unclamped)
        mag = mag_at(self.ptz.goals[-1], [])
        field_h = 0.5 * abs(TILT_PER_V) / mag
        above = (300 + TILT_PER_V * 0.1) - 100  # 110 units out of reach
        self.assertLessEqual(above + 0.5 * 0.05 * abs(TILT_PER_V), field_h / 2 + 1)

    def test_a_picked_target_is_found_again_under_a_new_id(self):
        self.eye([], 0.9)  # in eye mode (the switch itself forgets any target)
        self.at.set_enabled(True, EYE_ID_BASE + 1)
        ts = 1.0
        for i in range(30):  # walking right, counted into event 5
            self.eye([eye_track(1, 0.3 + 0.03 * i / 15, 0.5, ts, vel=(0.03, 0.0), event_id=5)], ts)
            ts += 1 / 15
        lost = ts
        for _ in range(int(2.5 * 15)):  # behind a van for 2.5 s: the eye's tracker drops it
            self.eye([], ts)
            ts += 1 / 15
        back = eye_track(2, 0.36 + 0.03 * (ts - lost), 0.5, ts, vel=(0.03, 0.0), counted=False, first_ts=ts)
        self.eye([back], ts)
        self.assertEqual(self.at.target_id, EYE_ID_BASE + 2)
        self.assertTrue(self.at.user_locked)
        self.assertEqual(back.inherited, EYE_ID_BASE + 1)
        self.assertEqual(back.event_id, 5)  # its count and event carry over: not counted twice
        self.assertNotIn("home", self.ptz.calls)

    def test_a_click_on_the_ptz_view_keeps_the_target_and_maps_through_the_late_pose(self):
        self.cfg.data["ptz"]["autozoom"] = False  # a zoom step makes that moment's picture timing uncertain
        a = lambda ts: eye_track(1, 0.3, 0.5, ts)
        b = lambda ts: eye_track(2, 0.7, 0.5, ts)
        ts = 1.0
        while ts < 1.9:  # following A
            self.eye([a(ts)], ts)
            ts += 1 / 15
        self.at.set_enabled(True, EYE_ID_BASE + 2)  # the user picks B on the wide view: a swing
        self.eye([a(1.95), b(1.95)], 1.95)
        self.assertEqual(self.at.target_id, EYE_ID_BASE + 2)
        self.clock.t = 2.1
        self.at.set_enabled(True, 7)  # ...then clicks the person in the middle of the (late) PTZ picture: A
        self.assertEqual(self.at.target_id, EYE_ID_BASE + 2)  # B is kept until the click is placed
        self.eye([a(2.12), b(2.12)], 2.12)
        self.assertEqual(self.at.target_id, EYE_ID_BASE + 2)  # nothing else is picked meanwhile
        self.clock.t = 2.15
        self.at.update([ptz_track(7, 0.5, 0.5, 2.15, h=0.08 / 0.5)], 2.15)
        self.eye([a(2.2), b(2.2)], 2.2)
        self.assertEqual(self.at.target_id, EYE_ID_BASE + 1)
        self.assertTrue(self.at.user_locked)

    def test_a_click_right_after_a_swing_waits_for_a_picture_that_shows_the_pose(self):
        self.eye([eye_track(1, 0.7, 0.5, 1.0)], 1.0)  # the first aim swings the camera from Home
        self.at.set_enabled(True, 7)
        self.clock.t = 1.2
        self.at.update([ptz_track(7, 0.5, 0.5, 1.2, h=0.08 / 0.5)], 1.2)  # the picture still shows Home
        self.assertEqual(self.at._ptz_pick, 7)  # not placed through a pose the picture does not show yet
        self.ptz.is_moving = lambda: True  # and it never settles
        self.clock.t = 3.0
        self.at.update([ptz_track(7, 0.5, 0.5, 3.0, h=0.08 / 0.5)], 3.0)
        self.assertIsNone(self.at._ptz_pick)  # given up after PICK_TIMEOUT_S
        self.assertEqual(self.at.target_id, EYE_ID_BASE + 1)  # still following what it was
        self.ptz.is_moving = lambda: False  # still at the target for seconds: the picture shows this pose
        self.clock.t = 5.0
        self.at.set_enabled(True, 7)
        self.at.update([ptz_track(7, 0.5, 0.5, 5.0, h=0.08 / 0.5)], 5.0)
        self.assertAlmostEqual(self.at._ptz_pick[0], 0.7, delta=0.02)


class SlewingPTZ(FakePTZ):
    """Absolute moves take time (pt_rate units per second at full speed), as on the real camera."""

    def __init__(self, clock, rate=250.0):
        super().__init__()
        self.clock, self.rate = clock, rate
        self.pan, self.tilt = 0.0, -150.0
        self.goal, self.speed, self.last_motion = None, 1.0, 0.0

    def pan_tilt_to(self, pan, tilt, speed=1.0):
        self.aims.append((pan, tilt, speed))
        self.goal, self.speed, self.at_home, self.last_motion = (float(pan), float(tilt)), speed, False, self.clock.t
        return True

    def step(self, dt):
        if self.goal is None:
            return
        self.pan += math.copysign(min(abs(self.goal[0] - self.pan), self.rate * self.speed * dt), self.goal[0] - self.pan)
        self.tilt += math.copysign(min(abs(self.goal[1] - self.tilt), self.rate * self.speed * dt), self.goal[1] - self.tilt)
        self.last_motion = self.clock.t
        if abs(self.pan - self.goal[0]) < 0.5 and abs(self.tilt - self.goal[1]) < 0.5:
            self.goal = None

    @property
    def pt_arrived(self):
        return self.goal is None

    def is_moving(self):
        return self.goal is not None

    def quiet_since(self):
        return self.last_motion


class EyeFollowReviewTests(unittest.TestCase):
    """The second review: what the first round of fixes still got wrong (each fails on that code)."""
    setUp, eye = EyeFollowTests.setUp, EyeFollowTests.eye

    def walk_then_vanish(self, u0, vel, seconds=1.0):
        ts = 1.0
        while ts < 1.0 + seconds:
            u = u0 + vel[0] * (ts - 1.0)
            self.eye([eye_track(1, u, 0.5, ts, vel=vel, event_id=5)], ts)
            ts += 1 / 15
        return ts, u0 + vel[0] * seconds

    def test_someone_appearing_where_the_target_left_the_picture_is_not_given_its_count(self):
        ts, u = self.walk_then_vanish(0.92, (0.06, 0.0))  # A walks out through the right edge
        for _ in range(15):
            self.eye([], ts)
            ts += 1 / 15
        b = eye_track(2, 0.98, 0.5, ts, vel=(-0.06, 0.0), counted=False, first_ts=ts)  # B walks in there, going left
        self.eye([b], ts)
        self.assertIsNone(b.inherited)
        self.assertFalse(b.counted)

    def test_someone_walking_the_other_way_is_not_taken_for_the_lost_target(self):
        ts, u = self.walk_then_vanish(0.4, (0.06, 0.0))  # behind a van mid-picture
        for _ in range(8):
            self.eye([], ts)
            ts += 1 / 15
        b = eye_track(2, u + 0.06 * 0.6, 0.5, ts, vel=(-0.06, 0.0), counted=False, first_ts=ts)
        self.eye([b], ts)
        self.assertIsNone(b.inherited)
        self.assertNotEqual(self.at.target_id, EYE_ID_BASE + 2)

    def test_a_still_re_detection_is_followed_unproven_and_given_up(self):
        ts, u = self.walk_then_vanish(0.4, (0.06, 0.0))
        for _ in range(8):
            self.eye([], ts)
            ts += 1 / 15
        s_ = lambda t: eye_track(2, u + 0.03, 0.5, t, moving=False, counted=False, first_ts=ts - 0.1)  # standing
        self.eye([s_(ts)], ts)
        self.assertEqual(self.at.target_id, EYE_ID_BASE + 2)
        self.assertIsNone(self.at._target.inherited)  # not clearly the same person: no count moves over
        walker = lambda t: eye_track(3, 0.2 + 0.05 * (t - ts), 0.6, t, vel=(0.05, 0.0), counted=False)
        self.eye([s_(ts + 0.5), walker(ts + 0.5)], ts + 0.5)
        self.assertEqual(self.at.target_id, EYE_ID_BASE + 3)  # anything seen moving takes over
        self.at.reset_target()
        self.ptz.calls.clear()
        ts2 = ts + 1.0
        self.walk_then_vanish(0.4, (0.06, 0.0))  # again, and this time nobody else comes
        t = 2.6
        self.eye([s_(t)], t)
        while t < 2.6 + self.at.UNPROVEN_S + 0.5:
            t += 1 / 15
            self.eye([s_(t)], t)
        self.assertIn("home", self.ptz.calls)
        self.assertIsNone(self.at.target_id)

    def test_a_target_the_eye_lost_long_ago_is_not_matched(self):
        self.cfg.data["ptz"]["autozoom"] = False
        self.cfg.data["ptz"]["return_home_seconds"] = 15.0
        ts, _ = self.walk_then_vanish(0.5, (0.0, 0.0))  # A stands at the centre for a second, then is gone
        while ts < 8.0:
            self.eye([], ts)
            ts += 1 / 15
        self.clock.t = 8.0
        p = ptz_track(7, 0.5, 0.5, 8.0, h=0.08 / 0.5)  # a passer-by right where A was
        self.at.update([p], 8.0)
        self.assertIsNone(self.at.ptz_match_id)
        self.assertIsNone(p.event_id)
        self.assertEqual(self.at._bias, [0.0, 0.0])

    def test_a_jittering_target_past_the_tilt_limit_is_aimed_only_now_and_then(self):
        self.cfg.data["ptz"]["autozoom"] = False
        self.ptz.zoom_pos = 9000
        rng = np.random.default_rng(3)
        ts = 1.0
        while ts < 4.0:  # standing below the lowest tilt; detections jitter a little
            j = rng.uniform(-0.002, 0.002, 2)
            self.eye([eye_track(1, 0.5 + j[0], 0.95 + j[1], ts, h=0.05)], ts)
            ts += 1 / 15
        self.assertLessEqual(len(self.ptz.aims), 3.0 / self.at.AIM_PERIOD_S + 1)

    def test_while_the_ptz_counts_only_what_it_counted_is_followed(self):
        self.at.eye_counted_only = True
        walker = lambda ts, counted: eye_track(1, 0.4 + 0.02 * ts, 0.5, ts, vel=(0.02, 0.0), counted=counted)
        self.eye([walker(1.0, False)], 1.0)
        self.assertIsNone(self.at.target_id)  # not counted by the PTZ yet: it stays Home to count it
        self.assertEqual(self.ptz.aims, [])
        self.eye([walker(1.2, True)], 1.2)  # the PTZ counted it (linked through the sync map)
        self.assertEqual(self.at.target_id, EYE_ID_BASE + 1)

    def test_a_click_on_a_walking_group_locks_the_one_clicked(self):
        self.cfg.data["ptz"]["autozoom"] = False
        self.ptz = cam = SlewingPTZ(self.clock)
        self.at = AutoTracker(cam, self.cfg)
        self.at.sync, self.at.eye_active = linear_map(), True
        sync, lag_ptz, lag_eye, dt = self.at.sync, 0.6, 0.15, 1 / 15
        for speed in (0.03, 0.06):
            with self.subTest(speed=speed):
                self.at.reset_target()
                cam.pan, cam.tilt, cam.goal = 0.0, -150.0, None
                start = {"A": 0.30, "B": 0.26, "C": 0.23}  # B 0.04 behind A, C 0.03 behind B
                ids = {"A": 1, "B": 2, "C": 3}
                u_at = lambda n, t: start[n] + speed * t
                hist, t, clicked = [], 0.0, False
                while t < 6.0:
                    self.clock.t = t
                    cam.step(dt)
                    hist.append((t, cam.pan, cam.tilt))
                    self.at.update_eye([eye_track(ids[n], u_at(n, t - lag_eye), 0.5, t, vel=(speed, 0.0))
                                        for n in "ABC"], t)
                    if not clicked and t >= 5.0:
                        clicked = True
                        self.at.set_enabled(True, 9)  # B, on the PTZ view
                    then = [x for x in hist if x[0] <= t - lag_ptz]
                    if then:  # the PTZ picture: scene and pose lag_ptz ago
                        cu, cv = sync.to_eye(then[-1][1], then[-1][2])
                        fw, _ = sync.field(cu, cv, 0, [])
                        self.at.update([ptz_track(pid, 0.5 + (u_at(n, t - lag_ptz) - cu) / fw, 0.5, t, h=0.16)
                                        for n, pid in (("A", 8), ("B", 9), ("C", 10))
                                        if 0 < 0.5 + (u_at(n, t - lag_ptz) - cu) / fw < 1], t)
                    if clicked and self.at._ptz_pick is None and self.at.user_locked:
                        break
                    t = round(t + dt, 6)
                self.assertEqual(self.at.target_id, EYE_ID_BASE + 2)
                self.assertTrue(self.at.user_locked)


if __name__ == "__main__":
    unittest.main()
