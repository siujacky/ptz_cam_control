"""Pipeline end to end with fake cameras, recorders and detector: who counts, who is main, and failover."""
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from ptzcam import failover, pipeline
from ptzcam.config import ConfigStore
from ptzcam.detector import Detection
from ptzcam.ptz import EYE_ID_BASE
from ptzcam.sync import SyncMap
from ptzcam.storage import Storage
from tests.helpers import FakeBus

W, H = 1280, 720


class FakeSource:
    """Serves frames from `scene(t)` at 15 fps in its own thread, like FrameSource."""

    def __init__(self, scene):
        self.scene, self.fps = scene, 15.0
        self._cond = threading.Condition()
        self._frame = None
        self._id, self._ts = 0, 0.0
        self._stop = threading.Event()
        self.ring = FakeRecorder()
        self.anti_flicker, self.crop, self.node = -1, {}, None  # as UsbCapture has, for the eye

    def start(self, recover=()):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        t0 = time.time()
        while not self._stop.is_set():
            frame = self.scene(time.time() - t0) if self.scene is not None else None  # None: offline
            if frame is not None:
                with self._cond:
                    self._frame, self._ts = frame, time.time()
                    self._id += 1
                    self._cond.notify_all()
            time.sleep(1 / 15)

    def stop(self):
        self._stop.set()

    @property
    def online(self):
        return self._ts > 0 and time.time() - self._ts < 5

    @property
    def last_frame_age(self):
        return round(time.time() - self._ts, 2) if self._ts else None

    def latest(self):
        with self._cond:
            return (self._id, self._ts, self._frame) if self._frame is not None else None

    def wait_frame(self, last_id, timeout=1.0):
        with self._cond:
            self._cond.wait_for(lambda: self._id != last_id or self._stop.is_set(), timeout)
            if self._frame is None or self._id == last_id:
                return None
            return self._id, self._ts, self._frame


class FakeRecorder:
    online = True

    def __init__(self, *a, **k):
        self.jobs = []

    def start(self, recover=()):
        pass

    def stop(self):
        pass

    def buffer_seconds(self):
        return 120.0

    def request_clip(self, eid, t0, t1, out, cb):
        self.jobs.append((eid, out.name))


class FakeDetector:
    """A 'person' wherever the frame has a bright square on the dark background."""
    enhancer = None
    device, infer_ms = "FAKE", 1.0

    def __init__(self, *a, **k):
        pass

    def detect(self, frame, enhanced=None):
        g = frame[:, :, 1]
        if g.mean() > 200:  # a washed-out picture shows nothing
            return []
        ys, xs = np.nonzero(g > 200)
        if len(xs) == 0:
            return []
        box = np.array([xs.min() / W, ys.min() / H, (xs.max() + 1) / W, (ys.max() + 1) / H])
        return [Detection(box, 0.9, 0, "person")]

    def close(self):
        pass


def walker(t, white=False):
    """A person walking left to right; or a flat white picture (the sun on the window)."""
    if white:
        return np.full((H, W, 3), 255, np.uint8)
    f = np.full((H, W, 3), 40, np.uint8)
    f[::8, :, :] = 60  # some texture, so the picture is not flat
    x = int(100 + 160 * t) % (W - 120)
    f[300:520, x:x + 90] = 230
    return f


class PipelineTest(unittest.TestCase):
    def make(self, eye=False, ptz_scene=walker, eye_scene=walker, **cfg_patch):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "models").mkdir()
        (root / "models" / "yolov11m.hef").write_bytes(b"")
        raw = {"lowlight": {"model": ""}, "ptz": {"enabled": False, "autotrack": False},
               "storage": {"data_dir": str(root / "data"), "segment_dir": str(root / "seg")},
               "eye": {"enabled": eye, "zone": [[0, 0], [1, 0], [1, 1], [0, 1]]},
               "failover": {"switch_after": 1.0, "return_after": 1.0}, "detect": {"min_hits": 2}}
        for sec, vals in cfg_patch.items():
            raw.setdefault(sec, {}).update(vals)
        import json
        (root / "config.json").write_text(json.dumps(raw))
        cfg = ConfigStore(root / "config.json")
        (root / "data").mkdir()
        self.storage = Storage(root / "data" / "ptzcam.db")
        self.addCleanup(self.storage.close)
        sources = {"ptz": FakeSource(ptz_scene), "eye": FakeSource(eye_scene)}
        usbcam = types.ModuleType("ptzcam.usbcam")
        usbcam.UsbCapture = lambda *a, **k: sources["eye"]
        patches = [mock.patch.object(pipeline, "FrameSource", lambda *a, **k: sources["ptz"]),
                   mock.patch.object(pipeline, "Recorder", FakeRecorder),
                   mock.patch.object(pipeline, "HailoDetector", FakeDetector),
                   mock.patch.dict(sys.modules, {"ptzcam.usbcam": usbcam}),
                   mock.patch.object(failover, "MIN_DWELL_S", 0.0),
                   mock.patch.object(failover, "BLINDED_FOR_S", 0.5)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.bus = FakeBus()
        self.p = pipeline.Pipeline(cfg, self.storage, self.bus, root / "data" / "media")
        self.p._start()  # without the PTZ worker: no camera to talk to
        self.addCleanup(self.p._stop_all)
        return self.p

    def wait(self, cond, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def counted(self):
        return self.storage._all("SELECT track_id, camera FROM tracks")

    def test_one_camera_counts_on_the_ptz_as_before(self):
        p = self.make()
        self.assertTrue(self.wait(lambda: self.counted()))
        tid, cam = self.counted()[0]["track_id"], self.counted()[0]["camera"]
        self.assertEqual(cam, "ptz")
        self.assertLess(tid, EYE_ID_BASE)
        st = p.status()
        self.assertEqual(st["main"]["camera"], "ptz")
        self.assertEqual(set(st["cameras"]), {"ptz"})
        self.assertIs(p.live, p.live_for("ptz"))

    def test_with_an_eye_the_eye_counts_and_its_ids_do_not_clash(self):
        p = self.make(eye=True, ptz_scene=lambda t: walker(t + 100))
        self.assertTrue(self.wait(lambda: any(r["camera"] == "eye" for r in self.counted())))
        self.assertEqual(p.counting, "eye")
        eye_rows = [r for r in self.counted() if r["camera"] == "eye"]
        self.assertGreaterEqual(eye_rows[0]["track_id"], EYE_ID_BASE)
        self.assertFalse(any(r["camera"] == "ptz" for r in self.counted()))  # one camera counts
        self.assertEqual(p.status()["counting"], "eye")

    def test_a_washed_out_ptz_hands_the_view_to_the_eye(self):
        state = {"white": False}
        p = self.make(eye=True, ptz_scene=lambda t: walker(t, white=state["white"]))
        self.assertTrue(self.wait(lambda: p.units["ptz"].fps > 0 and p.units["eye"].fps > 0))
        self.assertEqual(p.selector.main, "ptz")
        state["white"] = True
        self.assertTrue(self.wait(lambda: p.selector.main == "eye", timeout=15))
        self.assertIs(p.live, p.live_for("eye"))
        st = p.status()
        self.assertEqual(st["main"]["camera"], "eye")
        self.assertEqual(st["main"]["reason"], "PTZ picture washed out")
        self.assertIn("main", self.bus.names())
        state["white"] = False
        self.assertTrue(self.wait(lambda: p.selector.main == "ptz", timeout=20))


def linear_sync(home=None):
    """A sync map for a PTZ whose Home view is the middle of the eye's picture (half its size, at zoom 0)."""
    rows = [[u, v, -1000 + 2000 * u, 300 - 900 * v, 0.5, 0.5]
            for u in np.linspace(0.05, 0.95, 7) for v in np.linspace(0.05, 0.95, 5)]
    m = SyncMap(np.array(rows), ptz_zoom=0, eye_size=(1920, 1080), tilt_limits=(-432.0, 400.0))
    if home is not None:
        m.meta["ptz_home"] = list(home)
    return m


def track(tid, cat, x, y, ts, h=0.2, counted=False, first_ts=0.0, event_id=None, moving=True):
    w = h * 0.4
    return SimpleNamespace(id=tid, category=cat, confirmed=True, counted=counted, last_ts=ts, first_ts=first_ts,
                           box=np.array([x - w / 2, y - h / 2, x + w / 2, y + h / 2]), center=(x, y),
                           vel=np.zeros(2), event_id=event_id, moving=moving, max_score=0.9, in_zone=False)


def counted_by(p, role, tracks):
    """What camera `role` counted, as its loop remembers it."""
    for t in tracks:
        p._remember_counted(role, [t], t.last_ts)


class ReviewFixesTest(unittest.TestCase):
    """Behaviour the dual-camera review asked for."""
    make, wait, counted = PipelineTest.make, PipelineTest.wait, PipelineTest.counted

    def stopped(self, **kw):
        """A pipeline whose loops are stopped, so tests can set the trackers' tracks themselves."""
        p = self.make(**kw)
        for u in p.units.values():
            u.stop.set()
        for u in p.units.values():
            u.thread.join(10)
        return p

    def test_an_offline_ptz_keeps_every_events_wide_clip(self):
        patcher = mock.patch.object(pipeline, "STARTUP_GRACE_S", 1.0)  # it never starts: no need to wait 15 s
        patcher.start()
        self.addCleanup(patcher.stop)
        p = self.make(eye=True, ptz_scene=None)  # the PTZ stream is down
        self.assertTrue(self.wait(lambda: p.selector.main == "eye", timeout=10))
        self.assertTrue(self.wait(lambda: any(r["camera"] == "eye" for r in self.counted()), timeout=15))
        self.assertTrue(self.wait(lambda: all(r["clip_wide_keep"] for r in self.storage._all(
            "SELECT clip_wide_keep FROM events WHERE start_ts > 0"))))

    def test_the_eye_counts_on_the_ptz_view_only_once_it_knows_where_that_is(self):
        p = self.make(eye=True)
        p.cfg.data["eye"]["zone"] = []  # count on the PTZ's Home view, mapped
        self.assertTrue(self.wait(lambda: p.units["eye"].fps > 0))
        p.sync = p.autotracker.sync = linear_sync()  # mapped, but the PTZ has not been read at Home
        time.sleep(1.5)
        self.assertEqual(p.counting, "ptz")  # not the whole wide view
        self.assertEqual(p._zone_for_eye(p.cfg.data), [])
        p.sync = p.autotracker.sync = linear_sync(home=(0, -150, 0))  # as the calibration found Home
        self.assertTrue(self.wait(lambda: p.counting == "eye", timeout=5))
        zone = p._zone_for_eye(p.cfg.data)
        self.assertAlmostEqual(zone[0][0], 0.25, delta=0.02)  # the Home view: the middle half of the eye's
        self.assertAlmostEqual(zone[2][0], 0.75, delta=0.02)

    def test_a_handover_counts_what_the_old_camera_never_counted(self):
        p = self.stopped(eye=True)
        now, cfg = time.time(), p.cfg.data
        eye = p.units["eye"].tracker
        # No sync map: as many as the PTZ has counted in view, the longest seen first.
        counted_by(p, "ptz", [track(5, "person", 0.5, 0.5, now, counted=True, event_id=3)])
        eye.tracks = [track(EYE_ID_BASE + 1, "person", 0.4, 0.5, now, first_ts=now - 9),
                      track(EYE_ID_BASE + 2, "person", 0.6, 0.5, now, first_ts=now - 2)]
        self.assertEqual(p._hand_over("ptz", "eye", cfg), 1)
        self.assertEqual([(t.counted, t.event_id) for t in eye.tracks], [(True, 3), (False, None)])
        # A washed-out PTZ that has counted nobody lately: everyone the eye sees still counts.
        p._counted["ptz"].clear()
        counted_by(p, "ptz", [track(6, "person", 0.5, 0.5, now - 20, counted=True)])  # long gone
        eye.tracks = [track(EYE_ID_BASE + 3, "person", 0.4, 0.5, now)]
        self.assertEqual(p._hand_over("ptz", "eye", cfg), 0)
        self.assertFalse(eye.tracks[0].counted)

    def test_a_handover_after_an_outage_remembers_what_the_old_camera_counted(self):
        p = self.stopped(eye=True, ptz_scene=None)
        now, cfg = time.time(), p.cfg.data
        # The PTZ counted a walker 6 s before counting moved (its stream dropped; seen as offline 5 s later). Its
        # tracker has let go of the track, but the pipeline remembers it.
        counted_by(p, "ptz", [track(5, "person", 0.5, 0.5, now - 6, counted=True, event_id=3)])
        p.units["ptz"].source._ts = now - 6.0  # its last picture: offline (none for 5 s), last seen working then
        eye = p.units["eye"].tracker
        eye.tracks = [track(EYE_ID_BASE + 1, "person", 0.4, 0.5, now, first_ts=now - 9)]
        self.assertEqual(p._hand_over("ptz", "eye", cfg), 1)
        self.assertEqual((eye.tracks[0].counted, eye.tracks[0].event_id), (True, 3))

    def test_a_handover_after_a_white_out_remembers_what_the_ptz_counted_before_it_faded(self):
        p = self.stopped(eye=True)
        now, cfg = time.time(), p.cfg.data
        # The PTZ's detections faded 3.7 s before its picture was flagged blinded (3 s ago): its last count is 6.7 s old.
        counted_by(p, "ptz", [track(5, "person", 0.5, 0.5, now - 6.7, counted=True, event_id=3)])
        p._last_det["ptz"] = {"person": now - 6.7}
        p.selector._blind_since = now - 3.0
        eye = p.units["eye"].tracker
        eye.tracks = [track(EYE_ID_BASE + 1, "person", 0.4, 0.5, now, first_ts=now - 9)]
        self.assertEqual(p._hand_over("ptz", "eye", cfg), 1)
        self.assertTrue(eye.tracks[0].counted)
        # Long before (more than BAD_LEAD_S before the flags): someone who has gone; nothing to carry over.
        p._counted["ptz"].clear()
        eye.tracks[0].counted = False
        counted_by(p, "ptz", [track(6, "person", 0.5, 0.5, now - 25, counted=True, event_id=2)])
        p._last_det["ptz"] = {"person": now - 25}
        self.assertEqual(p._hand_over("ptz", "eye", cfg), 0)
        self.assertFalse(eye.tracks[0].counted)

    def test_a_handover_after_the_wide_camera_whites_out_remembers_what_it_counted(self):
        p = self.stopped(eye=True)
        now, cfg = time.time(), p.cfg.data
        # The eye counted a walker; its picture went flat white and is flagged blinded 3.8 s after it last detected.
        counted_by(p, "eye", [track(EYE_ID_BASE + 1, "person", 0.5, 0.5, now - 3.8, counted=True, event_id=4)])
        p._last_det["eye"] = {"person": now - 3.8}
        p.units["eye"].view.levels.blinded = True
        ptz = p.units["ptz"].tracker
        ptz.tracks = [track(7, "person", 0.4, 0.5, now, first_ts=now - 9)]
        self.assertEqual(p._hand_over("eye", "ptz", cfg), 1)
        self.assertEqual((ptz.tracks[0].counted, ptz.tracks[0].event_id), (True, 4))

    def test_nobody_who_came_after_the_old_camera_stopped_takes_its_count(self):
        p = self.stopped(eye=True)
        now, cfg = time.time(), p.cfg.data
        # The PTZ stopped detecting 15 s before its white share tripped (3 s ago); the walker it counted then left.
        counted_by(p, "ptz", [track(5, "person", 0.5, 0.5, now - 18, counted=True, event_id=3)])
        p._last_det["ptz"] = {"person": now - 18, "vehicle": now}  # a parked car still detected
        p.selector._white_since = now - 3.0
        eye = p.units["eye"].tracker
        eye.tracks = [track(EYE_ID_BASE + 2, "person", 0.6, 0.5, now, first_ts=now - 5)]  # someone new
        self.assertEqual(p._hand_over("ptz", "eye", cfg), 0)
        self.assertFalse(eye.tracks[0].counted)
        eye.tracks = [track(EYE_ID_BASE + 1, "person", 0.4, 0.5, now, first_ts=now - 30)]  # still there all along
        self.assertEqual(p._hand_over("ptz", "eye", cfg), 1)
        self.assertTrue(eye.tracks[0].counted)

    def test_a_count_handover_never_pairs_something_that_could_not_be_counted(self):
        p = self.stopped(eye=True)
        now, cfg = time.time(), p.cfg.data
        counted_by(p, "ptz", [track(5, "person", 0.5, 0.5, now, counted=True, event_id=3)])
        eye = p.units["eye"].tracker
        eye.tracks = [track(EYE_ID_BASE + 1, "person", 0.2, 0.5, now, first_ts=now - 60, moving=False),  # waiting
                      track(EYE_ID_BASE + 2, "person", 0.6, 0.5, now, first_ts=now - 4)]  # the walker
        self.assertEqual(p._hand_over("ptz", "eye", cfg), 1)
        self.assertEqual([t.counted for t in eye.tracks], [False, True])

    def test_a_handover_matches_by_position_through_the_sync_map(self):
        p = self.stopped(eye=True)
        p.sync = linear_sync(home=(0, -150, 0))  # PTZ control is off in these tests: it shows its Home view
        now, cfg = time.time(), p.cfg.data
        # The PTZ counted someone a quarter of its frame right of centre: in the eye, at u = 0.5 + 0.25 * 0.5.
        counted_by(p, "ptz", [track(5, "person", 0.75, 0.5, now, h=0.2, counted=True, event_id=3)])
        eye = p.units["eye"].tracker
        eye.tracks = [track(EYE_ID_BASE + 1, "person", 0.2, 0.5, now, h=0.1, first_ts=now - 9),  # outside its view
                      track(EYE_ID_BASE + 2, "person", 0.625, 0.5, now, h=0.1)]
        self.assertEqual(p._hand_over("ptz", "eye", cfg), 1)
        self.assertEqual([t.counted for t in eye.tracks], [False, True])

    def test_the_main_cameras_tracks_join_the_event_the_other_camera_counted(self):
        p = self.stopped(eye=True)
        p.sync = linear_sync(home=(0, -150, 0))
        p.counting = "eye"
        now, cfg = time.time(), p.cfg.data

        class Active:
            id = 9
        p.events.active = Active()
        p.units["eye"].tracker.tracks = [track(EYE_ID_BASE + 1, "person", 0.625, 0.5, now, h=0.1, counted=True,
                                               event_id=9)]
        mine = [track(5, "person", 0.75, 0.5, now), track(6, "person", 0.2, 0.5, now)]
        p._link_to_counter("ptz", mine, now, cfg)
        p.events.active = None
        self.assertEqual([(t.event_id, t.counted) for t in mine], [(9, True), (None, False)])

    def test_renames_and_the_eyes_anti_flicker_apply_without_a_restart(self):
        p = self.stopped(eye=True)
        p.cfg.data["camera"]["name"], p.cfg.data["eye"]["name"] = "Street", "Garden"
        st = p.status()
        self.assertEqual((st["cameras"]["ptz"]["name"], st["cameras"]["eye"]["name"]), ("Street", "Garden"))
        self.assertEqual(st["camera"]["name"], "Street")
        p.cfg.data["eye"]["anti_flicker"] = 0
        with mock.patch("ptzcam.uvc.find_device", return_value="/dev/video9"), \
                mock.patch("ptzcam.uvc.read_control", return_value=1), \
                mock.patch("ptzcam.uvc.write_control", return_value=(1, 0)) as write:
            p._eye_controls(p.cfg.data, check=False)
            p._eye_controls(p.cfg.data, check=False)  # once: the capture now holds the new value
        write.assert_called_once_with("/dev/video9", "power_line_frequency", 0)
        self.assertEqual(p.units["eye"].source.anti_flicker, 0)  # what it sets on its next connect

    def test_the_status_says_why_the_eye_does_not_count(self):
        p = self.stopped(eye=True)
        cfg = p.cfg.data
        p.counting = "eye"
        self.assertEqual(p.status()["counting_note"], "")
        p.counting = "ptz"
        cfg["eye"]["zone"] = []
        self.assertIn("needs the camera sync", p.status()["counting_note"])
        p.sync = linear_sync()  # synced, but the PTZ's Home has not been read
        self.assertIn("Home view", p.status()["counting_note"])
        cfg["eye"]["count"] = False
        self.assertEqual(p.status()["counting_note"], "counting on the wide camera is off")

    def test_the_unattended_syncs_progress_is_shown_while_it_runs(self):
        import json as _json
        import os
        p = self.stopped(eye=True)
        path = p.cfg.resolve(p.cfg.data["storage"]["data_dir"]) / "sync_pending.json"
        now = time.time()
        for state, pid, updated, shown in (("waiting", os.getpid(), now, True),
                                           ("waiting", 999999, now, False),  # its process has gone
                                           ("waiting", os.getpid(), now - 3600, False),  # stopped writing
                                           ("failed", 999999, now - 3600, True),  # a failure is kept a day
                                           ("failed", 999999, now - 2 * 86400, False)):
            path.write_text(_json.dumps({"pid": pid, "state": state, "since": now - 60, "clear_for": 240,
                                         "need": 600, "updated": updated, "error": "x" if state == "failed" else None}))
            p._read_sync_pending()
            got = p.status()["sync"]["scheduled"]
            self.assertEqual(got is not None, shown, (state, pid, updated, got))
            if shown:
                self.assertEqual(got["state"], state)
        path.unlink()
        p._read_sync_pending()
        self.assertIsNone(p.status()["sync"]["scheduled"])

    def test_a_main_camera_switch_carries_its_time(self):
        state = {"white": False}
        p = self.make(eye=True, ptz_scene=lambda t: walker(t, white=state["white"]))
        self.assertTrue(self.wait(lambda: p.units["ptz"].fps > 0 and p.units["eye"].fps > 0))
        state["white"] = True
        self.assertTrue(self.wait(lambda: "main" in self.bus.names(), timeout=15))
        ev = [d for n, d in self.bus.messages if n == "main"][-1]
        self.assertEqual(ev["camera"], "eye")
        self.assertAlmostEqual(ev["since"], p.selector.since)
        self.assertGreater(ev["since"], 0)

    def test_a_camera_still_starting_is_not_taken_for_offline(self):
        slow = {"t0": None}

        def late(t):  # the PTZ stream sends its first picture 3 s after the start
            return walker(t) if t > 3.0 else None
        p = self.make(eye=True, ptz_scene=late)
        time.sleep(2.0)
        self.assertEqual(p.selector.main, "ptz")  # the eye was quicker, but the PTZ is only starting
        self.assertTrue(self.wait(lambda: p.units["ptz"].online, timeout=8))
        time.sleep(1.5)
        self.assertEqual(p.selector.main, "ptz")
        self.assertNotIn("main", self.bus.names())

    def test_a_broken_crop_in_the_sync_file_never_stops_the_start(self):
        import json as _json
        p = self.stopped(eye=True)
        m = linear_sync()
        for bad in (1, True, 2.5, "x", [1, 2]):
            with self.subTest(eye_eptz=bad):
                m.meta["eye_eptz"] = bad
                with mock.patch("ptzcam.uvc.find_device", side_effect=LookupError("no camera")):
                    p._set_eye_crop(m)
                self.assertEqual(p.units["eye"].source.crop, {})
        path = p.cfg.resolve(p.cfg.data["storage"]["data_dir"]) / "sync.json"
        d = m.to_json()
        d["meta"]["eye_eptz"] = 1
        path.write_text(_json.dumps(d))
        p._stop_all()
        with mock.patch("ptzcam.uvc.find_device", side_effect=LookupError("no camera")):
            p._start()  # must not raise
        self.assertTrue(all(u.thread.is_alive() for u in p.units.values()))

    def test_a_crop_value_that_is_not_a_number_keeps_the_map(self):
        p = self.stopped(eye=True)
        m = linear_sync()
        m.meta["eye_eptz"] = {"zoom_absolute": float("nan"), "pan_absolute": float("inf"), "tilt_absolute": 3600}
        with mock.patch("ptzcam.uvc.find_device", side_effect=LookupError("no camera")):
            p._set_eye_crop(m)
        self.assertEqual(p.units["eye"].source.crop, {"tilt_absolute": 3600})

    def test_an_error_in_housekeeping_does_not_end_a_cameras_loop(self):
        p = self.make(eye=True, ptz_scene=None, eye_scene=None)  # both offline: housekeeping runs on that path
        with mock.patch.object(p, "_update_counting", side_effect=RuntimeError("boom")):
            time.sleep(2.5)
        self.assertTrue(p.units["ptz"].thread.is_alive())
        self.assertTrue(p.units["eye"].thread.is_alive())


if __name__ == "__main__":
    unittest.main()
