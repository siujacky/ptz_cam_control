import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from ptzcam.events import EventManager
from ptzcam.lowlight import VIEW_SIZE, AutoLevels, LightMeter, NightView
from ptzcam.nightclip import NightClipper
from ptzcam.storage import Storage, event_json
from ptzcam.tracker import Tracker
from tests.helpers import FakeBus, FakeConfig, FakeRecorder, det

FPS = 15.0
LL = {"mode": "auto", "night_luma": 45, "day_luma": 70}


def flat(v):
    return np.full((720, 1280, 3), v, np.uint8)


class LightMeterTests(unittest.TestCase):
    def test_hysteresis_and_modes(self):
        m = LightMeter()
        self.assertTrue(m.update(flat(15), 0.0, LL))               # starts at night straight away
        for i in range(8):                                         # half a second of headlights
            self.assertTrue(m.update(flat(220), 0.1 + i / FPS, LL))
        t = 1.0
        for _ in range(int(60 * FPS)):                             # dawn: a minute at mid brightness
            t += 1 / FPS
            m.update(flat(60), t, LL)
        self.assertTrue(m.night)                                   # between the thresholds: unchanged
        for _ in range(int(60 * FPS)):
            t += 1 / FPS
            m.update(flat(120), t, LL)
        self.assertFalse(m.night)
        self.assertTrue(m.update(flat(120), t + 1, {**LL, "mode": "on"}))
        self.assertFalse(LightMeter().update(flat(5), 0.0, {**LL, "mode": "off"}))


class NightViewTests(unittest.TestCase):
    def test_balance_denoise_and_no_ghosting(self):
        rng = np.random.default_rng(1)
        nv = NightView()
        cast = np.zeros((400, 600, 3), np.float32)
        cast[..., 0], cast[..., 1], cast[..., 2] = 150, 90, 110          # magenta street-light cast (RGB)
        cast[:, :200] *= 0.5                                             # some structure to stretch
        for i in range(40):
            f = cast + rng.normal(0, 8, cast.shape)
            f[150:250, 40 + 10 * i:120 + 10 * i] = 250                     # a bright object moving right
            out = nv.process(np.clip(f, 0, 255).astype(np.uint8))
        self.assertEqual(out.shape, (VIEW_SIZE[1], VIEW_SIZE[0], 3))
        self.assertEqual(out.dtype, np.uint8)
        sx = VIEW_SIZE[0] / 600
        bg = out[300:390, int(320 * sx):int(590 * sx)].reshape(-1, 3).astype(float)   # static background, BGR
        b, g, r = bg.mean(axis=0)
        self.assertLess(max(b, g, r) - min(b, g, r), 25)                 # the cast is (mostly) gone
        self.assertLess(bg.std(axis=0).mean(), 8 * 255 / 150)            # grain below the stretched input's
        x0 = int((40 + 10 * 39 + 15) * sx)
        obj = out[int(170 * 400 / 400):230, x0:x0 + 40].astype(float)
        self.assertGreater(obj.mean(), 200)                              # moving object: shown, not smeared
        trail = out[170:230, int((40 + 10 * 30) * sx):int((40 + 10 * 30 + 40) * sx)].astype(float)
        self.assertLess(trail.mean(), 200)                               # and no ghost where it was


def washed_out(top=255, span=50, seed=0):
    """A scene squeezed into the top levels, as when sun on the window fades the picture to white."""
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 256, (90, 160), dtype=np.uint8).astype(np.float32)
    small = np.repeat(np.repeat(base, 8, 0), 8, 1)  # coarse texture, 1280x720
    img = top - span + small * (span / 255.0)
    return np.clip(np.dstack([img] * 3), 0, 255).astype(np.uint8)


class AutoLevelsTests(unittest.TestCase):
    def test_washed_out_picture_is_stretched(self):
        a = AutoLevels()
        frame = washed_out(span=60)
        out = a.process(frame)
        self.assertIsNotNone(out)
        self.assertGreater(a.gain, 3.0)
        self.assertGreater(float(out.std()), 2.5 * float(frame.std()))  # contrast restored
        self.assertLess(float(out.mean()), float(frame.mean()) - 60)     # and no longer near white
        self.assertFalse(a.blinded)

    def test_good_dim_and_flat_white_pictures_are_left_alone(self):
        good = washed_out(top=255, span=255, seed=1)                     # already uses the full range
        dim = (good // 4).astype(np.uint8)                                # dark: night vision's job
        for frame in (good, dim):
            a = AutoLevels()
            self.assertIsNone(a.process(frame))
            self.assertEqual(a.gain, 1.0)
        a = AutoLevels()
        self.assertIsNone(a.process(flat(253)))                           # nothing left to recover
        self.assertTrue(a.blinded)
        self.assertGreater(a.white, 0.99)

    def test_measures_without_correcting_and_adapts_smoothly(self):
        a = AutoLevels()
        self.assertIsNone(a.process(washed_out(span=60), correct=False))  # auto brightness off
        self.assertEqual(a.gain, 1.0)
        a = AutoLevels()
        a.process(washed_out(span=60))
        g0 = a.gain
        a.process(washed_out(span=120, seed=1))                           # the scene changes in one frame...
        self.assertLess(abs(a.gain - g0), 0.25 * g0)                      # ...the stretch follows gradually


class _Cfg:
    def __init__(self):
        self.data = {"detect": {"fps": 15}, "events": {"preroll_seconds": 2}}


class NightClipperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_builds_clip_for_window_with_preroll(self):
        c = NightClipper(_Cfg())
        c.start()
        done = {}
        t0 = time.time()
        frames = [np.full((VIEW_SIZE[1], VIEW_SIZE[0], 3), (i * 7) % 255, np.uint8) for i in range(45)]
        for i, f in enumerate(frames[:15]):                          # one second before the event...
            c.push(t0 + i / FPS, f)
        out = self.root / "e" / "clip_night.mp4"
        c.request_clip(7, t0, t0 + 2.9, out, lambda eid, ok: done.update(eid=eid, ok=ok))
        for i, f in enumerate(frames[15:], 15):                      # ...and two after it
            c.push(t0 + i / FPS, f)
            time.sleep(0.005)
        deadline = time.time() + 30
        while "ok" not in done and time.time() < deadline:
            time.sleep(0.1)
        c.stop()
        self.assertEqual(done, {"eid": 7, "ok": True})
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                                "-show_entries", "stream=width,height,nb_read_frames", "-of", "csv=p=0", str(out)],
                               capture_output=True, text=True)
        w, h, n = (int(v) for v in probe.stdout.strip().split(","))
        self.assertEqual((w, h), VIEW_SIZE)
        self.assertGreaterEqual(n, 42)                               # the whole window, pre-roll included

    def test_too_little_footage_fails_cleanly(self):
        c = NightClipper(_Cfg())
        done = {}
        c.request_clip(3, 0.0, 30.0, self.root / "x.mp4", lambda eid, ok: done.update(ok=ok))
        c.stop()                                                     # never started: builds pending jobs
        self.assertEqual(done, {"ok": False})
        self.assertFalse((self.root / "x.mp4").exists())


class StorageMigrationTests(unittest.TestCase):
    def test_old_database_gains_night_columns(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,"
                       " start_ts REAL NOT NULL, trigger_ts REAL NOT NULL, end_ts REAL NOT NULL, last_seen_ts REAL,"
                       " top_category TEXT NOT NULL, categories TEXT NOT NULL DEFAULT '{}',"
                       " labels TEXT NOT NULL DEFAULT '{}', max_score REAL NOT NULL DEFAULT 0, snapshot TEXT,"
                       " thumb TEXT, clip TEXT, clip_status TEXT NOT NULL DEFAULT 'pending',"
                       " snap_version INTEGER NOT NULL DEFAULT 0)")
            db.execute("INSERT INTO events(day, start_ts, trigger_ts, end_ts, top_category) VALUES"
                       " ('2026-10-30', 1, 2, 31, 'vehicle')")
            db.commit()
            db.close()
            s = Storage(path)
            ev = event_json(s.get_event(1))
            self.assertEqual((ev["night"], ev["clip_night_url"], ev["clip_night_status"]), (False, None, None))
            s.update_event(1, night=1, clip_night="2026-10-30/1/clip_night.mp4", clip_night_status="pending")
            s.fail_pending_night_clips()
            ev = event_json(s.get_event(1))
            self.assertEqual((ev["night"], ev["clip_night_status"], ev["clip_night_url"]), (True, "failed", None))
            s.close()


class _Clipper:
    def __init__(self, active):
        self.active = active
        self.jobs = []

    def request_clip(self, event_id, t0, t1, out_path, callback):
        self.jobs.append((event_id, t0, t1, out_path, callback))


class NightEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.storage = Storage(root / "t.db")
        self.media = root / "media"
        self.clipper = _Clipper(active=True)
        self.events = EventManager(FakeConfig(), self.storage, FakeRecorder(), self.media, FakeBus(),
                                   night_clipper=self.clipper)
        self.tracker = Tracker(16 / 9)
        self.t = 1_800_000_000.0

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def walk(self):
        frame = np.zeros((VIEW_SIZE[1], VIEW_SIZE[0], 3), np.uint8)  # snapshots come from the night view
        for i in range(45):
            self.t += 1 / FPS
            tracks, removed = self.tracker.update([det(0.1 + 0.012 * i, 0.5)], self.t)
            self.events.process(self.t, frame, tracks, removed, True)

    def test_night_event_records_night_clip(self):
        self.events.night = True
        self.walk()
        ev = self.storage.list_events()[0][0]
        self.assertEqual((ev["night"], ev["clip_night_status"]), (1, "pending"))
        eid, t0, t1, out, cb = self.clipper.jobs[0]
        self.assertAlmostEqual(t1 - t0, 30.0)
        self.assertEqual(out, self.media / ev["clip_night"])
        cb(eid, True)
        self.assertEqual(event_json(self.storage.get_event(eid))["clip_night_url"], f"/media/{ev['clip_night']}")

    def test_day_event_has_no_night_clip(self):
        self.walk()
        ev = self.storage.list_events()[0][0]
        self.assertEqual((ev["night"], ev["clip_night"]), (0, None))
        self.assertEqual(self.clipper.jobs, [])

    def test_clip_for_deleted_event_removes_its_files(self):
        self.events.night = True
        self.walk()
        ev = self.storage.list_events()[0][0]
        eid, *_, cb = self.clipper.jobs[0]
        d = (self.media / ev["snapshot"]).parent
        self.assertTrue(d.is_dir())
        self.storage.delete_event(eid)
        cb(eid, True)
        self.assertFalse(d.exists())


if __name__ == "__main__":
    unittest.main()
