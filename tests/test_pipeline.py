"""Pipeline end to end with fake cameras, recorders and detector: who counts, who is main, and failover."""
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from ptzcam import failover, pipeline
from ptzcam.config import ConfigStore
from ptzcam.detector import Detection
from ptzcam.ptz import EYE_ID_BASE
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

    def start(self, recover=()):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        t0 = time.time()
        while not self._stop.is_set():
            frame = self.scene(time.time() - t0)
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


if __name__ == "__main__":
    unittest.main()
