import unittest

import numpy as np

from ptzcam.tracker import Tracker
from tests.helpers import det

FPS = 15.0


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def tick(self):
        self.t += 1 / FPS
        return self.t


class TrackerTests(unittest.TestCase):
    def test_walker_is_one_moving_track(self):
        tr, clock = Tracker(16 / 9), Clock()
        for i in range(60):
            tracks, removed = tr.update([det(0.1 + 0.01 * i, 0.5)], clock.tick())
        self.assertEqual(len(tracks), 1)
        t = tracks[0]
        self.assertTrue(t.confirmed)
        self.assertTrue(t.moving)
        self.assertEqual(t.direction(), "ltr")
        self.assertEqual(t.label, "person")

    def test_parked_car_never_moves(self):
        rng = np.random.default_rng(1)
        tr, clock = Tracker(16 / 9), Clock()
        for _ in range(300):
            jitter = rng.normal(0, 0.002, 2)
            tracks, _ = tr.update([det(0.5 + jitter[0], 0.6 + jitter[1], 0.2, 0.12, 0.9, "vehicle")], clock.tick())
        self.assertEqual(len(tracks), 1)
        self.assertTrue(tracks[0].confirmed)
        self.assertFalse(tracks[0].moving)

    def test_two_people_crossing_keep_their_ids(self):
        tr, clock = Tracker(16 / 9), Clock()
        for i in range(80):
            tracks, _ = tr.update([det(0.1 + 0.01 * i, 0.42), det(0.9 - 0.01 * i, 0.58)], clock.tick())
        self.assertEqual(len(tracks), 2)
        self.assertEqual(sorted(t.direction() for t in tracks), ["ltr", "rtl"])
        self.assertEqual({t.id for t in tracks}, {1, 2})

    def test_camera_motion_is_not_object_motion(self):
        tr, clock = Tracker(16 / 9), Clock()
        car = dict(w=0.2, h=0.12, score=0.9, category="vehicle")
        for _ in range(10):
            tr.update([det(0.5, 0.5, **car)], clock.tick())
        tr.motion_frozen = True  # camera pans: the parked car slides across the image
        for i in range(30):
            tr.update([det(0.5 - 0.01 * i, 0.5, **car)], clock.tick())
        tr.motion_frozen = False
        tr.reanchor(clock.t)
        for _ in range(60):
            tracks, _ = tr.update([det(0.21, 0.5, **car)], clock.tick())
        self.assertEqual(len(tracks), 1)
        self.assertFalse(tracks[0].moving)

    def test_occlusion_glitch_does_not_make_a_parked_car_move(self):
        tr, clock = Tracker(16 / 9), Clock()
        car = dict(w=0.2, h=0.12, score=0.9, category="vehicle")
        for _ in range(60):
            tr.update([det(0.5, 0.6, **car)], clock.tick())
        for _ in range(2):  # something passes in front: the detector boxes only the visible top 40 %
            tr.update([det(0.5, 0.6 - 0.036, 0.2, 0.048, 0.9, "vehicle")], clock.tick())
        for _ in range(60):
            tracks, _ = tr.update([det(0.5, 0.6, **car)], clock.tick())
        self.assertEqual(len(tracks), 1)
        self.assertFalse(tracks[0].moving)

    def test_low_threshold_category_starts_tracks(self):
        tr, clock = Tracker(16 / 9), Clock()
        for i in range(10):  # a dim person at night, detected at 0.28
            tracks, _ = tr.update([det(0.3 + 0.01 * i, 0.5, score=0.28)], clock.tick(), {"person": 0.25})
        self.assertEqual(len(tracks), 1)
        self.assertTrue(tracks[0].confirmed)
        tr2, clock2 = Tracker(16 / 9), Clock()
        for i in range(10):  # same detections with the default threshold: no track
            tracks2, _ = tr2.update([det(0.3 + 0.01 * i, 0.5, score=0.28)], clock2.tick())
        self.assertEqual(tracks2, [])

    def test_single_frame_false_positive_is_dropped(self):
        tr, clock = Tracker(16 / 9), Clock()
        tr.update([det(0.3, 0.3)], clock.tick())
        for _ in range(4):
            tracks, removed = tr.update([], clock.tick())
        self.assertEqual(tracks, [])
        self.assertEqual(removed, [])

    def test_confirmed_track_is_removed_after_max_lost(self):
        tr, clock = Tracker(16 / 9), Clock()
        for i in range(20):
            tr.update([det(0.2 + 0.01 * i, 0.5)], clock.tick())
        removed_all = []
        for _ in range(40):  # ~2.7 s without detections
            tracks, removed = tr.update([], clock.tick())
            removed_all += removed
        self.assertEqual(tracks, [])
        self.assertEqual(len(removed_all), 1)

    def test_categories_do_not_steal_each_others_tracks(self):
        tr, clock = Tracker(16 / 9), Clock()
        for i in range(30):  # cyclist: person and bicycle boxes overlap heavily
            x = 0.2 + 0.01 * i
            tracks, _ = tr.update([det(x, 0.45, 0.06, 0.2), det(x, 0.52, 0.08, 0.12, 0.8, "bicycle")], clock.tick())
        self.assertEqual(sorted(t.category for t in tracks), ["bicycle", "person"])


if __name__ == "__main__":
    unittest.main()
