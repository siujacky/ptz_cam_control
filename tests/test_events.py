import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from ptzcam.events import EventManager, point_in_polygon
from ptzcam.storage import Storage
from ptzcam.tracker import Tracker
from tests.helpers import FakeBus, FakeConfig, FakeRecorder, det

FPS = 15.0
FRAME = np.zeros((720, 1280, 3), dtype=np.uint8)


class EventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.storage = Storage(root / "t.db")
        self.bus, self.rec = FakeBus(), FakeRecorder()
        self.cfg = FakeConfig()
        self.media = root / "media"
        self.events = EventManager(self.cfg, self.storage, self.rec, self.media, self.bus)
        self.tracker = Tracker(16 / 9)
        self.t = 1_800_000_000.0

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def step(self, dets, counting=True):
        self.t += 1 / FPS
        tracks, removed = self.tracker.update(dets, self.t)
        self.events.process(self.t, FRAME, tracks, removed, counting)
        return tracks

    def walk(self, frames=45, y=0.5, category="person", x0=0.1, dx=0.012, **kw):
        for i in range(frames):
            self.step([det(x0 + dx * i, y, category=category, **kw)])

    def idle(self, seconds):
        for _ in range(int(seconds * FPS)):
            self.step([])

    def test_a_target_found_again_keeps_its_event_and_improves_its_snapshot(self):
        self.walk(frames=30)                                       # counted
        old = next(t for t in self.tracker.tracks if t.counted)
        eid = old.event_id
        version = self.storage.get_event(eid)["snap_version"]
        # The camera turns and zooms in: the tracker loses it and finds a much bigger box (a new track).
        for _ in range(6):
            tracks = self.step([det(0.5, 0.5, w=0.25, h=0.6)], counting=False)
        new = next(t for t in tracks if t.id != old.id and t.confirmed)
        new.inherit(old)                                           # what auto-follow does when it re-locks
        self.events.active.snap_written = 0.0                      # skip the snapshot rate limit
        self.step([det(0.5, 0.5, w=0.25, h=0.6)], counting=False)
        self.assertGreater(self.storage.get_event(eid)["snap_version"], version)  # the close-up is the photo
        self.walk(frames=30, x0=0.5, w=0.25, h=0.6)                # camera still again; it walks on
        self.assertEqual(len(self.storage.event_objects(eid)), 1)  # not counted twice
        self.assertEqual(len(self.storage.list_events()[0]), 1)

    def test_walker_creates_one_event_with_media_and_clip_job(self):
        self.walk()
        rows, _ = self.storage.list_events()
        self.assertEqual(len(rows), 1)
        ev = rows[0]
        self.assertEqual(ev["top_category"], "person")
        self.assertTrue((self.media / ev["snapshot"]).is_file())
        self.assertTrue((self.media / ev["thumb"]).is_file())
        self.assertEqual(len(self.rec.jobs), 1)
        eid, t0, t1, out, cb = self.rec.jobs[0]
        self.assertAlmostEqual(t1 - t0, 30.0)
        self.assertAlmostEqual(ev["trigger_ts"] - t0, 5.0)
        self.assertEqual(len(self.storage.event_objects(eid)), 1)
        self.assertIn("event_new", self.bus.names())
        self.assertIn("counts", self.bus.names())
        cb(eid, True)
        self.assertEqual(self.storage.get_event(eid)["clip_status"], "ready")
        self.assertEqual(self.bus.messages[-1][0], "event_update")

    def test_second_object_in_window_joins_event_and_later_one_starts_new(self):
        self.walk(category="person", y=0.4)
        self.walk(category="vehicle", y=0.7, w=0.2, h=0.12, score=0.9)
        rows, _ = self.storage.list_events()
        self.assertEqual(len(rows), 1)
        self.assertEqual(event_categories(rows[0]), {"person": 1, "vehicle": 1})
        self.assertEqual(rows[0]["top_category"], "person")
        self.idle(30)  # let the 30 s window close
        self.walk(category="animal", y=0.6)
        rows, _ = self.storage.list_events()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["top_category"], "animal")

    def test_parked_car_and_paused_counting_create_nothing(self):
        for _ in range(150):
            self.step([det(0.5, 0.6, 0.2, 0.12, 0.9, "vehicle")])
        for i in range(45):
            self.step([det(0.1 + 0.012 * i, 0.4)], counting=False)
        self.assertEqual(self.storage.list_events()[0], [])
        self.assertEqual(self.rec.jobs, [])

    def test_disabled_category_and_low_confidence_are_ignored(self):
        self.cfg.data["detect"]["categories"]["animal"]["enabled"] = False
        self.walk(category="animal")
        self.walk(category="person", y=0.3, score=0.4)  # below person min_score 0.5
        self.assertEqual(self.storage.list_events()[0], [])

    def test_zone_limits_counting(self):
        self.cfg.data["detect"]["zone"] = [[0.0, 0.0], [1.0, 0.0], [1.0, 0.3], [0.0, 0.3]]  # top band only
        self.walk(y=0.6)  # feet at y≈0.675: outside
        self.assertEqual(self.storage.list_events()[0], [])
        self.walk(y=0.15)  # feet at y≈0.225: inside
        self.assertEqual(len(self.storage.list_events()[0]), 1)

    def test_zone_is_ignored_while_the_camera_is_away(self):
        self.cfg.data["detect"]["zone"] = [[0.0, 0.0], [1.0, 0.0], [1.0, 0.3], [0.0, 0.3]]
        for i in range(20):  # camera panned away: image positions say nothing about the real zone
            tracks = self.step([det(0.1 + 0.012 * i, 0.15)], counting=False)
        self.assertFalse(any(t.in_zone for t in tracks))
        self.assertEqual(self.storage.list_events()[0], [])
        tracks = self.step([det(0.1 + 0.012 * 20, 0.15)])  # back on the Home view: the zone applies again
        self.assertTrue(all(t.in_zone for t in tracks if t.confirmed))

    def test_point_in_polygon(self):
        sq = [[0, 0], [1, 0], [1, 1], [0, 1]]
        self.assertTrue(point_in_polygon(0.5, 0.5, sq))
        self.assertFalse(point_in_polygon(1.5, 0.5, sq))


def event_categories(row):
    import json
    return json.loads(row["categories"])



class WideClipTests(unittest.TestCase):
    """With an eye camera every event also asks for clip_wide.mp4; eye.clips "auto" keeps it only when the PTZ's
    own clip does not show the whole scene."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.storage = Storage(root / "t.db")
        self.rec, self.wide = FakeRecorder(), FakeRecorder()
        self.cfg = FakeConfig()
        self.media = root / "media"
        self.events = EventManager(self.cfg, self.storage, self.rec, self.media, FakeBus(), wide_recorder=self.wide)
        self.tracker = Tracker(16 / 9)
        self.t = 1_800_000_000.0

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def count_one(self, camera="eye"):
        for i in range(30):
            self.t += 1 / FPS
            tracks, removed = self.tracker.update([det(0.1 + 0.012 * i, 0.5)], self.t)
            self.events.process(self.t, FRAME, tracks, removed, True, camera=camera)
        eid, _, _, out, done = self.wide.jobs[-1]
        out.write_bytes(b"x" * 20000)  # the clip builder made it
        return eid, out, done

    def ptz_clip(self, ok=True):
        eid, _, _, out, done = self.rec.jobs[-1]
        done(eid, ok)

    def test_auto_drops_the_wide_clip_when_the_ptz_stayed_home(self):
        for ptz_first in (True, False):  # the two rings finish in either order
            with self.subTest(ptz_first=ptz_first):
                self.t += 60
                eid, out, done = self.count_one()
                if ptz_first:
                    self.ptz_clip()
                done(eid, True)
                if not ptz_first:  # kept until the PTZ clip is known to show it all
                    self.assertEqual(self.storage.get_event(eid)["clip_wide_status"], "ready")
                    self.assertTrue(out.exists())
                    self.ptz_clip()
                row = self.storage.get_event(eid)
                self.assertIsNone(row["clip_wide_status"])
                self.assertFalse(out.exists())
                self.assertEqual(row["clip_status"], "ready")
                self.assertEqual(row["main_camera"], "ptz")
        self.assertEqual(self.storage._all("SELECT camera FROM tracks")[0]["camera"], "eye")

    def test_auto_keeps_it_when_the_ptz_followed_or_the_eye_was_main(self):
        eid, out, done = self.count_one()
        self.events.wide_needed()  # the PTZ left Home during the event
        self.ptz_clip()
        done(eid, True)
        self.assertEqual(self.storage.get_event(eid)["clip_wide_status"], "ready")
        self.assertTrue(out.exists())
        self.t += 60
        self.events.main = "eye"  # the PTZ washed out or offline when the event began
        eid, out, done = self.count_one()
        self.ptz_clip()
        done(eid, True)
        self.assertEqual(self.storage.get_event(eid)["clip_wide_status"], "ready")

    def test_auto_keeps_it_when_the_ptz_clip_failed(self):
        for ptz_first in (True, False):
            with self.subTest(ptz_first=ptz_first):
                self.t += 60
                eid, out, done = self.count_one()
                if ptz_first:
                    self.ptz_clip(ok=False)
                done(eid, True)
                if not ptz_first:
                    self.ptz_clip(ok=False)
                row = self.storage.get_event(eid)
                self.assertEqual((row["clip_status"], row["clip_wide_status"]), ("failed", "ready"))
                self.assertTrue(out.exists())

    def test_the_decision_survives_a_restart(self):
        eid, out, done = self.count_one()
        self.events.wide_needed()
        self.ptz_clip()
        # The service died before the wide clip was cut; a new run recovers the job with a new EventManager.
        fresh = EventManager(self.cfg, self.storage, FakeRecorder(), self.media, FakeBus(), wide_recorder=FakeRecorder())
        fresh._wide_clip_done(eid, True, 0.4)
        row = self.storage.get_event(eid)
        self.assertEqual(row["clip_wide_status"], "ready")
        self.assertAlmostEqual(row["clip_wide_lead"], 0.4)
        self.assertTrue(out.exists())

    def test_always_and_never(self):
        self.cfg.data["eye"]["clips"] = "always"
        eid, out, done = self.count_one()
        done(eid, True)
        self.assertEqual(self.storage.get_event(eid)["clip_wide_status"], "ready")
        self.cfg.data["eye"]["clips"] = "never"
        jobs = len(self.wide.jobs)
        self.t += 60  # a new event
        for i in range(30):
            self.t += 1 / FPS
            tracks, removed = self.tracker.update([det(0.9 - 0.012 * i, 0.3)], self.t)
            self.events.process(self.t, FRAME, tracks, removed, True, camera="eye")
        self.assertEqual(len(self.wide.jobs), jobs)  # never asked for


class PhotoSourceTests(unittest.TestCase):
    """Photos come from the main camera where its picture surely shows the object: the wide view holds the PTZ's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.storage = Storage(root / "t.db")
        self.media = root / "media"
        self.events = EventManager(FakeConfig(), self.storage, FakeRecorder(), self.media, FakeBus(),
                                   wide_recorder=FakeRecorder())
        self.trackers = {"ptz": Tracker(16 / 9), "eye": Tracker(16 / 9)}
        self.t = 1_800_000_000.0

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def run_both(self, counter, usable=True, frames=30):
        """The PTZ's picture is level 40, the eye's 110; `counter` counts a walker that both see."""
        level = {"ptz": 40, "eye": 110}
        for i in range(frames):
            self.t += 1 / FPS
            for cam in ("ptz", "eye"):
                frame = np.full((720, 1280, 3), level[cam], np.uint8)
                tracks, removed = self.trackers[cam].update([det(0.2 + 0.01 * i, 0.5)], self.t)
                self.events.process(self.t, frame, tracks, removed, cam == counter,
                                    usable=usable or cam != counter, camera=cam)
        rows = self.storage._all("SELECT snapshot FROM events")
        self.assertEqual(len(rows), 1)
        return float(np.median(cv2.imread(str(self.media / rows[0]["snapshot"]))))

    def test_the_ptz_counts_and_the_wide_camera_is_main_without_the_sync(self):
        self.events.main, self.events.photo_from_main = "eye", True
        self.assertAlmostEqual(self.run_both("ptz"), 110, delta=8)  # the wide picture: it shows the PTZ's view

    def test_the_wide_camera_counts_and_the_ptz_is_main_without_the_sync(self):
        self.events.main, self.events.photo_from_main = "ptz", True
        self.assertAlmostEqual(self.run_both("eye"), 110, delta=8)  # its own picture of it: the PTZ may not show it

    def test_a_washed_out_ptz_counting_gives_way_to_the_wide_picture(self):
        self.events.main, self.events.photo_from_main = "eye", False
        self.assertAlmostEqual(self.run_both("ptz", usable=False), 110, delta=8)

if __name__ == "__main__":
    unittest.main()
