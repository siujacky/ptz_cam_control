import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from ptzcam import ptz
from ptzcam.ptz import AutoTracker, mag_at, pos_for
from ptzcam.tracker import Track
from tests.helpers import FakeConfig, det


class Clock:
    """Stands in for the time module inside ptzcam.ptz, so command rate limits follow the simulated frames."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def time(self):
        return self.t


class FakePTZ:
    def __init__(self):
        self.enabled = True
        self.at_home = True
        self.zoom_pos = 0
        self.moving = False
        self.quiet = 0.0
        self.calls = []
        self.moves = []  # (pan, tilt); a stop is (0, 0)
        self.goals = []  # absolute zoom positions sent
        self.zoom_goal = None
        self.home_zoom = None

    def move(self, pan, tilt, speed=0.5, hold=1.0):
        self.calls.append("move")
        self.moves.append((pan, tilt))

    def stop(self):
        self.calls.append("stop")

    def stop_pantilt(self):
        self.calls.append("stop_pantilt")
        self.moves.append((0.0, 0.0))

    def zoom_to(self, pos):
        self.calls.append("zoom_to")
        self.goals.append(pos)
        self.zoom_goal = pos

    @property
    def zoom_arrived(self):
        return self.zoom_goal is None or abs(self.zoom_pos - self.zoom_goal) <= 64

    def home(self):
        self.calls.append("home")

    def is_moving(self):
        return self.moving

    def quiet_since(self):
        return self.quiet


def track(tid, x, ts, category="vehicle", score=0.8, counted=False, in_zone=False, y=0.5, w=0.1, h=0.2,
          vel=(0.0, 0.0), moving=True, first_ts=0.0):
    box = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
    return SimpleNamespace(id=tid, category=category, label=category, confirmed=True, last_ts=ts, moving=moving,
                           score=score, max_score=score, counted=counted, in_zone=in_zone, box=box,
                           center=(x, y), area=w * h, vel=np.array(vel), first_ts=first_ts)


def person(tid, x, y, ts):
    t = Track(tid, det(x, y), ts, 16 / 9)
    t.confirmed = True
    return t


class AutoTrackTests(unittest.TestCase):
    def setUp(self):
        self.cfg = FakeConfig(ptz={"autotrack": True, "return_home_seconds": 5})
        self.ptz = FakePTZ()
        self.clock = Clock()
        patcher = mock.patch.object(ptz, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.at = AutoTracker(self.ptz, self.cfg)

    def step(self, tracks, ts):
        self.clock.t = ts
        self.at.update(tracks, ts)

    def test_only_countable_objects_are_followed(self):
        self.step([track(1, 0.3, 1.0, score=0.4)], 1.0)                          # below min_score 0.5
        self.assertIsNone(self.at.target_id)
        self.cfg.data["detect"]["categories"]["vehicle"]["enabled"] = False      # category not counted
        self.step([track(2, 0.3, 1.1)], 1.1)
        self.assertIsNone(self.at.target_id)
        self.cfg.data["detect"]["categories"]["vehicle"]["enabled"] = True
        self.cfg.data["detect"]["zone"] = [[0, 0], [0.5, 0], [0.5, 1], [0, 1]]
        self.step([track(3, 0.8, 1.2)], 1.2)                                     # outside the zone
        self.assertIsNone(self.at.target_id)
        self.step([track(4, 0.3, 1.3, in_zone=True)], 1.3)
        self.assertEqual(self.at.target_id, 4)

    def test_user_pick_is_never_swapped_for_another_object(self):
        self.at.set_enabled(True, track_id=7)
        self.step([track(7, 0.5, 1.0, category="person")], 1.0)
        self.assertEqual(self.at.target_id, 7)
        ts = 1.0
        for _ in range(40):                                                     # 4 s with only a stranger in view
            ts += 0.1
            self.step([track(8, 0.9, ts, counted=True), track(9, 0.2, ts, category="person")], ts)
            self.assertEqual(self.at.target_id, 7)
        for _ in range(15):                                                     # past return_home_seconds
            ts += 0.1
            self.step([track(8, 0.9, ts, counted=True)], ts)
        self.assertIn("home", self.ptz.calls)
        self.assertFalse(self.at.user_locked)                                   # given up only at return-home

    def test_auto_target_may_be_replaced_after_it_is_lost(self):
        self.step([track(1, 0.5, 1.0)], 1.0)
        self.assertEqual(self.at.target_id, 1)
        self.step([track(2, 0.9, 3.0)], 3.0)                                    # 2 s later: #1 gone, #2 moving
        self.assertEqual(self.at.target_id, 2)

    def test_people_come_first(self):
        car = track(1, 0.3, 1.0, counted=True, w=0.3, h=0.3)                   # big and already counted
        walker = track(2, 0.7, 1.0, category="person", w=0.05, h=0.15)
        self.step([car, walker], 1.0)
        self.assertEqual(self.at.target_id, 2)

    def test_a_person_takes_over_from_another_target(self):
        self.step([track(1, 0.5, 1.0)], 1.0)
        self.step([track(1, 0.5, 1.1), track(2, 0.2, 1.1, category="person", moving=False)], 1.1)
        self.assertEqual(self.at.target_id, 1)                                  # not seen moving yet
        self.step([track(1, 0.5, 1.2), track(2, 0.2, 1.2, category="person")], 1.2)
        self.assertEqual(self.at.target_id, 2)
        self.at.set_enabled(True, track_id=1)                                   # but the user's pick stays
        self.step([track(1, 0.5, 1.3), track(2, 0.2, 1.3, category="person")], 1.3)
        self.assertEqual(self.at.target_id, 1)

    def test_objects_too_fast_to_catch_are_not_chased(self):
        self.step([track(1, 0.3, 1.0, vel=(0.6, 0.0))], 1.0)                   # crosses the view in under 2 s
        self.assertIsNone(self.at.target_id)
        car = Track(2, det(0.10, 0.5, category="vehicle"), 1.0, 16 / 9)
        for i in range(1, 8):                                                   # 0.45 frame widths/s for 0.47 s
            car.update(det(0.10 + 0.03 * i, 0.5, category="vehicle"), 1.0 + i / 15, 4, 0.5, False)
        car.vel = np.array([0.25, 0.0])         # its velocity estimate still lagging, as seen live at pick time
        self.assertTrue(car.confirmed and car.moving)
        self.step([car], car.last_ts)
        self.assertIsNone(self.at.target_id)
        self.step([track(3, 0.3, 1.6, vel=(0.2, 0.05))], 1.6)
        self.assertEqual(self.at.target_id, 3)

    def test_target_found_again_after_the_camera_turns(self):
        old = person(5, 0.3, 0.2, 1.0)
        old.moving = old.counted = True
        old.event_id, old.db_id = 40, 77
        self.step([old], 1.0)
        self.assertEqual(self.at.target_id, 5)
        self.step([], 1.3)                       # lost while the camera turns towards it
        new = person(9, 0.4, 0.35, 1.6)          # found on the way to the centre; not moving (camera turning)
        self.step([new], 1.6)
        self.assertEqual(self.at.target_id, 9)
        self.assertEqual((new.counted, new.event_id, new.db_id, old.db_id), (True, 40, 77, None))
        self.assertTrue(new.cam_moved)           # keeps the count-time direction

    def test_a_parked_car_is_not_mistaken_for_the_lost_target(self):
        self.step([track(20, 0.48, 1.0, w=0.16, h=0.15), track(4, 0.58, 1.0, w=0.16, h=0.15, moving=False)], 1.0)
        self.assertEqual(self.at.target_id, 20)
        ts = 1.0
        for _ in range(70):                      # 7 s: #20 drove on, #4 stays parked right next to where it was
            ts += 0.1
            self.step([track(4, 0.58, ts, w=0.16, h=0.15, moving=False)], ts)
            self.assertNotEqual(self.at.target_id, 4)
        self.assertIn("home", self.ptz.calls)

    def test_a_lost_car_is_not_swapped_for_cars_coming_into_view(self):
        self.step([track(1, 0.5, 1.0, vel=(0.2, 0.0))], 1.0)
        self.assertEqual(self.at.target_id, 1)
        ts = 1.0
        for i in range(25):                      # 2.5 s: the camera pans on; parked cars come into view, new ids
            ts += 0.1
            self.step([track(10 + i, 0.75, ts, moving=False, first_ts=ts)], ts)
            self.assertIn(self.at.target_id, (1, None))
        self.assertEqual(self.ptz.moves[-1], (0.0, 0.0))  # and no coasting after a vehicle

    def test_a_much_bigger_new_person_is_not_the_lost_one(self):
        self.step([track(5, 0.4, 1.0, category="person", h=0.12, counted=True)], 1.0)
        self.step([track(8, 0.45, 1.4, category="person", h=0.5, moving=False, first_ts=1.2)], 1.4)
        self.assertEqual(self.at.target_id, 5)

    def test_a_rider_found_again_as_their_bike(self):
        self.step([track(5, 0.3, 1.0, category="person", y=0.2, counted=True)], 1.0)
        self.step([track(8, 0.35, 1.4, category="bicycle", y=0.3, moving=False, first_ts=1.2)], 1.4)
        self.assertEqual(self.at.target_id, 8)  # and no count is moved across categories (no inherit call)

    def test_a_flickering_rider_is_not_swapped_for_their_bike(self):
        rider = lambda ts: track(5, 0.5, ts, category="person", y=0.4, h=0.3)
        bike = lambda ts: track(4, 0.5, ts, category="bicycle", y=0.55, h=0.2, first_ts=0.5)
        self.step([rider(1.0), bike(1.0)], 1.0)
        self.assertEqual(self.at.target_id, 5)
        self.step([bike(1.07)], 1.07)            # the person detection drops out for a frame
        self.assertEqual(self.at.target_id, 5)
        self.step([rider(1.13), bike(1.13)], 1.13)
        self.assertEqual(self.at.target_id, 5)
        self.assertEqual(self.ptz.calls.count("home"), 0)

    def test_a_found_target_that_never_moves_is_dropped(self):
        old = person(5, 0.3, 0.2, 1.0)
        old.moving = True
        self.step([old], 1.0)
        ts = 1.5
        new = person(9, 0.45, 0.4, ts)
        self.step([new], ts)
        self.assertEqual(self.at.target_id, 9)
        for _ in range(30):                      # camera still (settled from t=2): 3 s is not enough
            ts += 0.1
            new.last_ts = ts
            self.step([new], ts)
        self.assertNotIn("home", self.ptz.calls)
        for _ in range(30):
            ts += 0.1
            new.last_ts = ts
            self.step([new], ts)
        self.assertIn("home", self.ptz.calls)
        self.assertIsNone(self.at.target_id)

    def test_learned_speed_keeps_pace_and_coasts_through_a_short_loss(self):
        ts, x = 1.0, 0.5
        for _ in range(20):                      # 2 s drifting right, mostly inside the dead zone
            self.step([track(1, x, ts, category="person", vel=(0.05, 0.0))], ts)
            ts += 0.1
            x += 0.005
        self.assertGreater(self.ptz.moves[-1][0], 0)
        self.assertGreater(self.at._speed[0], 0)
        seen = ts - 0.1
        self.step([], seen + 0.6)                # behind a window bar: keep turning at its speed
        self.assertGreater(self.ptz.moves[-1][0], 0)
        self.step([], seen + AutoTracker.COAST_S + 0.3)
        self.assertEqual(self.ptz.moves[-1], (0.0, 0.0))

    def run_person(self, n, h, x=0.5, vel=(0.0, 0.0)):
        for _ in range(n):
            self.step([track(1, x, self.ts, category="person", h=h, vel=vel)], self.ts)
            self.ts += 0.1

    def test_zoom_is_one_measured_step_that_waits_for_the_lens(self):
        self.cfg.data["ptz"].update(target_size=0.6, max_zoom_pos=12800)
        self.ptz.zoom_pos, self.ts = 1428, 1.0
        self.run_person(1, 0.2, vel=(0.2, 0.0))               # drifting: the camera is not keeping up
        self.run_person(7, 0.2)                               # centred, but dwell and picture lag not over
        self.assertEqual(self.ptz.goals, [])
        self.run_person(5, 0.2)
        goal = pos_for(mag_at(1428, []) * 3.0, [])            # 3x too small: straight to the right zoom
        self.assertEqual(self.ptz.goals, [goal])
        self.run_person(10, 0.2)                              # lens still on its way: nothing more is sent
        self.assertEqual(self.ptz.goals, [goal])
        self.ptz.zoom_pos = goal                              # arrived
        self.run_person(12, 0.55)                             # within 20 % of the target size: hold
        self.assertEqual(self.ptz.goals, [goal])
        self.run_person(12, 0.4)                              # measured again, off by more: one correction
        self.assertEqual(len(self.ptz.goals), 2)
        self.assertGreater(self.ptz.goals[1], goal)
        self.ptz.zoom_pos = self.ptz.goals[1]
        self.run_person(12, 0.95)                             # too big (it walked closer): back out
        self.assertLess(self.ptz.goals[-1], self.ptz.zoom_pos)

    def test_zoom_is_capped_skips_without_positions_and_backs_out_when_leaving(self):
        self.cfg.data["ptz"].update(target_size=0.6, max_zoom_pos=6000)
        self.ptz.zoom_pos, self.ts = 1428, 1.0
        self.run_person(14, 0.05)
        self.assertEqual(self.ptz.goals, [6000])              # capped at max_zoom_pos
        self.ptz.zoom_pos = 6000
        self.run_person(1, 0.3, x=0.97)                       # at the edge, about to leave: out at once
        self.assertEqual(self.ptz.goals[-1], pos_for(mag_at(6000, []) / 2, []))
        self.at.reset_target()
        self.ptz.zoom_pos, self.ptz.goals, self.ptz.zoom_goal = None, [], None
        self.run_person(20, 0.05)                             # no position readout (HTTP): never zooms
        self.assertEqual(self.ptz.goals, [])

    def test_a_lost_vehicle_sends_the_camera_home_at_once(self):
        self.step([track(1, 0.5, 1.0)], 1.0)
        self.step([], 1.3)
        self.assertNotIn("home", self.ptz.calls)              # a flicker
        self.step([], 1.6)
        self.assertIn("home", self.ptz.calls)                 # gone for 0.6 s: it will not come back
        self.assertIsNone(self.at.target_id)

    def test_the_next_object_takes_over_when_the_target_is_gone(self):
        self.step([track(1, 0.5, 1.0), track(2, 0.2, 1.0, moving=False)], 1.0)
        self.assertEqual(self.at.target_id, 1)
        self.step([track(2, 0.25, 1.6)], 1.6)                 # #1 gone; #2 is moving now
        self.assertEqual(self.at.target_id, 2)
        self.assertNotIn("home", self.ptz.calls)

    def test_a_lost_person_is_looked_for_wider_then_home(self):
        self.ptz.zoom_pos, self.ptz.home_zoom = 9000, 1428
        self.step([track(5, 0.5, 1.0, category="person")], 1.0)
        self.step([], 1.6)
        self.assertEqual(self.ptz.goals, [])                  # still coasting after it
        self.step([], 2.4)
        self.assertEqual(self.ptz.goals, [1428])              # back to the Home zoom to look wider
        self.step([], 5.0)
        self.assertNotIn("home", self.ptz.calls)
        self.step([], 6.1)                                    # past return_home_seconds (5)
        self.assertIn("home", self.ptz.calls)

if __name__ == "__main__":
    unittest.main()
