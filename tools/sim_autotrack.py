"""Closed-loop check of auto-follow (ptzcam.ptz.AutoTracker) against a simulated PTZ camera.

    .venv/bin/python tools/sim_autotrack.py [-v] [--calibrated] [--compare OLD_PTZ_PY] [--eye]

The camera pans at k deg/s per VISCA speed step; its zoom moves at `rate` positions per second (zoom direct,
or the continuous zoom at speed 7) along a lens whose true magnification at full zoom is `ratio`, and its
position readout lags 0.6 s. The detector sees the camera `lat` seconds late. A person walks across at w deg/s,
starting 8 degrees left of centre. Each combination is scored on how much of the time the person stays in
frame, how far off centre it sits, how big the zoom gets it (box height; target 0.61), the overshoot (largest
height / target) and how often the zoom reverses. Without --calibrated the controller assumes a 12x lens, so
the 8x and 20x lenses show how a wrong lens model is corrected. --compare runs an older ptz.py the same way.
Measured on this camera: zoom speed 7 doubles the picture in about half a second, the picture lags about half
a second, and a tilt at step 8 moved the picture about 0.3 frame heights per second.

--eye also runs eye mode: a fixed 90-degree eye camera (0.15 s latency) sees the walker, the sync map aims the PTZ
with absolute pan/tilt at its speed limit, and the PTZ's own (late) picture refines the aim. `bias` is a pointing
error of the sync map (calibration or parallax) that the refinement must remove. Eye mode's PTZ picture is late
as a whole (scene and pose `lat` ago); the PTZ-only runs keep the original model (the scene now, the pose `lat` ago)
so their numbers stay comparable with earlier results.
"""
import argparse
import importlib.util
import itertools
import math
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ptzcam import ptz  # noqa: E402
from ptzcam.sync import SyncMap  # noqa: E402
from tests.helpers import FakeConfig  # noqa: E402

HFOV = 55.0  # degrees at 1x
TARGET = 0.61
GRID = {"k": (0.5, 1.0, 2.0), "lat": (0.3, 0.6), "walk": (1.5, 3.0, 6.0), "rate": (4000, 8000, 16000),
        "ratio": (8.0, 12.0, 20.0)}
EYE_GRID = {"k": (0.5, 1.0, 2.0), "lat": (0.3, 0.6), "walk": (1.5, 3.0, 6.0), "ratio": (8.0, 12.0, 20.0),
            "bias": (0.0, 1.0)}
UNITS = 14.4  # pan/tilt units per degree
EYE_HFOV, EYE_LAT = 90.0, 0.15
VFOV = math.degrees(2 * math.atan(math.tan(math.radians(HFOV / 2)) * 9 / 16))
EYE_VFOV = math.degrees(2 * math.atan(math.tan(math.radians(EYE_HFOV / 2)) * 9 / 16))
PERSON_DEG = 0.1 * VFOV  # a tenth of the PTZ's frame height at 1x


class Clock:
    t = 0.0

    def monotonic(self):
        return self.t

    def time(self):
        return self.t


class SimCamera:
    def __init__(self, clock, k, rate, ratio, pos=1428, readout_lag=0.6):
        self.clock, self.k, self.rate, self.ratio, self.readout_lag = clock, k, rate, ratio, readout_lag
        self.enabled, self.at_home = True, True
        self.pan, self.pos = 0.0, float(pos)
        self.vstep = self.zstep = 0
        self.zoom_goal, self.home_zoom = None, pos
        self.reversals, self._zdir = 0, 0
        self._readout = deque([(0.0, float(pos))])
        self.goal, self.goal_speed, self.can_point, self.aims = None, 0.0, True, 0

    def mag(self):
        return self.ratio ** (self.pos / ptz.ZOOM_RANGE)

    @property
    def zoom_pos(self):
        t = self.clock.t - self.readout_lag
        while len(self._readout) > 1 and self._readout[1][0] <= t:
            self._readout.popleft()
        return int(self._readout[0][1])

    @property
    def zoom_arrived(self):
        return self.zoom_goal is None or abs(self.zoom_pos - self.zoom_goal) <= ptz.ZOOM_ARRIVED

    def _direction(self, d):
        if d and self._zdir and d != self._zdir:
            self.reversals += 1
        self._zdir = d or self._zdir

    def pan_tilt_to(self, pan, tilt, speed=1.0):  # absolute pan/tilt (eye mode): the camera drives there itself
        self.goal, self.goal_speed, self.vstep = pan / UNITS, speed, 0
        self.aims += 1
        return True

    def pose_estimate(self):
        return self.pan * UNITS, 0.0

    @property
    def pt_arrived(self):
        return self.goal is None or abs(self.pan - self.goal) < 0.1

    def move(self, pan, tilt, speed=0.5, hold=1.0):
        self.goal = None
        s = round(abs(pan) * speed * ptz.PAN_MAX)
        self.vstep = int(math.copysign(max(1, s), pan)) if abs(pan) > 1e-3 else 0

    def stop_pantilt(self):
        self.vstep = 0

    def stop(self):
        self.vstep = self.zstep = 0

    def zoom(self, direction, speed=0.5, hold=1.0):  # continuous zoom (older controllers)
        p = round(speed * 7)
        self.zstep = p if direction == "in" else -p if direction == "out" else 0
        self.zoom_goal = None
        self._direction((self.zstep > 0) - (self.zstep < 0))

    def zoom_to(self, pos):
        self.zoom_goal, self.zstep = int(pos), 0
        self._direction((pos > self.pos) - (pos < self.pos))

    def home(self):
        pass

    def is_moving(self):
        return bool(self.vstep or self.zstep) or (self.zoom_goal is not None and abs(self.pos - self.zoom_goal) > 1)

    def quiet_since(self):
        return 0.0

    def step(self, dt):
        if self.goal is not None:
            d = self.goal - self.pan
            self.pan += math.copysign(min(abs(d), self.goal_speed * ptz.PAN_MAX * self.k * dt), d)
        self.pan += self.k * self.vstep * dt
        if self.zoom_goal is not None:
            d = self.zoom_goal - self.pos
            self.pos += math.copysign(min(abs(d), self.rate * dt), d)
        elif self.zstep:
            self.pos += self.rate * self.zstep / 7 * dt
        self.pos = min(max(self.pos, 0.0), ptz.ZOOM_RANGE)
        self._readout.append((self.clock.t, self.pos))


def run(mod, k, lat, walk, rate, ratio, calibrated=False, seconds=25.0, fps=15):
    clock = Clock()
    with mock.patch.object(mod, "time", clock):
        curve = [[p, round(ratio ** (p / ptz.ZOOM_RANGE), 4)] for p in range(0, ptz.ZOOM_RANGE + 1, 1024)]
        cfg = FakeConfig(ptz={"autotrack": True, "return_home_seconds": 3.0, "target_size": TARGET,
                              "max_zoom_pos": 12800, "max_pan_speed": 14, "max_tilt_speed": 12,
                              "zoom_speed": 7, "zoom_curve": curve if calibrated else [], "zoom_rate": rate})
        cam = SimCamera(clock, k, rate, ratio)
        at = mod.AutoTracker(cam, cfg)
        past, seen = deque(), (0.0, cam.pan, cam.mag())
        vel, prev = np.zeros(2), None
        inside = lost = 0
        errs, heights = [], []
        reach_t = None
        dt = 1.0 / fps
        for i in range(int(seconds * fps)):
            t = clock.t = i * dt
            cam.step(dt)
            past.append((t, cam.pan, cam.mag()))
            while past and past[0][0] <= t - lat:
                seen = past.popleft()
            _, pan, mag = seen
            x = 0.5 + (walk * t - 8.0 - pan) / (HFOV / mag)
            h = min(0.95, 0.1 * mag)
            tracks = []
            if 0.0 < x < 1.0:
                c = np.array([x, 0.5])
                if prev is not None:
                    vel = 0.6 * vel + 0.4 * (c - prev) / dt
                prev, w = c, 0.4 * h
                tracks = [SimpleNamespace(
                    id=1, category="person", label="person", confirmed=True, last_ts=t, moving=True, score=0.8,
                    max_score=0.8, counted=True, in_zone=False, center=(x, 0.5), area=w * h, vel=vel.copy(),
                    first_ts=0.0, box=(max(0.0, x - w / 2), 0.5 - h / 2, min(1.0, x + w / 2), 0.5 + h / 2))]
                if reach_t is None and h >= 0.8 * min(TARGET, 0.1 * ratio ** (12800 / ptz.ZOOM_RANGE)):
                    reach_t = t
            else:
                prev = None
            at.update(tracks, t)
            if t > 3.0:
                if tracks:
                    inside += 1
                    errs.append(abs(x - 0.5))
                    heights.append(h)
                else:
                    lost += 1
        return {"in_frame": inside / max(1, inside + lost), "err": float(np.mean(errs)) if errs else 1.0,
                "height": float(np.median(heights)) if heights else 0.0,
                "overshoot": max(heights) / TARGET if heights else 0.0,
                "reach": reach_t if reach_t is not None else float("inf"), "reversals": cam.reversals}


def eye_sync(bias):
    """The sync map a perfect calibration would measure for this pair, off by `bias` degrees of pan."""
    rows = []
    for u in np.linspace(0.05, 0.95, 9):
        for v in np.linspace(0.1, 0.9, 5):
            rows.append([u, v, ((u - 0.5) * EYE_HFOV + bias) * UNITS, (0.5 - v) * EYE_VFOV * UNITS,
                         HFOV / EYE_HFOV, VFOV / EYE_VFOV])
    return SyncMap(np.array(rows), ptz_zoom=0, eye_size=(1920, 1080), tilt_limits=(-2000.0, 2000.0))


def run_eye(mod, k, lat, walk, ratio, bias, calibrated=False, seconds=25.0, fps=15):
    clock = Clock()
    with mock.patch.object(mod, "time", clock):
        curve = [[p, round(ratio ** (p / ptz.ZOOM_RANGE), 4)] for p in range(0, ptz.ZOOM_RANGE + 1, 1024)]
        cfg = FakeConfig(ptz={"autotrack": True, "return_home_seconds": 3.0, "target_size": TARGET,
                              "max_zoom_pos": 12800, "zoom_curve": curve if calibrated else [], "zoom_rate": 8000,
                              "pt_rate": ptz.PAN_MAX * k * UNITS}, eye={"enabled": True, "follow": True})
        cam = SimCamera(clock, k, 8000, ratio)
        at = mod.AutoTracker(cam, cfg)
        at.sync, at.eye_active = eye_sync(bias), True
        ptz_past, eye_past = deque(), deque()
        seen, eye_seen = (0.0, cam.pan, cam.mag(), -8.0), (0.0, -8.0)
        inside = lost = 0
        errs, heights = [], []
        reach_t = None
        dt = 1.0 / fps
        for i in range(int(seconds * fps)):
            t = clock.t = i * dt
            cam.step(dt)
            ang = walk * t - 8.0
            eye_past.append((t, ang))
            while eye_past and eye_past[0][0] <= t - EYE_LAT:
                eye_seen = eye_past.popleft()
            u, h_eye = 0.5 + eye_seen[1] / EYE_HFOV, PERSON_DEG / EYE_VFOV
            eye_tracks = []
            if 0.0 < u < 1.0:
                eye_tracks = [SimpleNamespace(
                    id=ptz.EYE_ID_BASE + 1, category="person", label="person", confirmed=True, last_ts=t, moving=True,
                    score=0.8, max_score=0.8, counted=True, in_zone=True, center=(u, 0.5), area=0.4 * h_eye * h_eye,
                    vel=np.array([walk / EYE_HFOV, 0.0]), first_ts=0.0, event_id=None,
                    box=np.array([u - 0.2 * h_eye, 0.5 - h_eye / 2, u + 0.2 * h_eye, 0.5 + h_eye / 2]))]
            at.update_eye(eye_tracks, t)
            # A real picture is late as a whole: scene and pose from the same moment, `lat` ago.
            ptz_past.append((t, cam.pan, cam.mag(), ang))
            while ptz_past and ptz_past[0][0] <= t - lat:
                seen = ptz_past.popleft()
            _, pan, mag, ang_seen = seen
            x = 0.5 + (ang_seen - pan) / (HFOV / mag)
            h = min(0.95, 0.1 * mag)
            tracks = []
            if 0.0 < x < 1.0:
                w = 0.4 * h
                tracks = [SimpleNamespace(
                    id=1, category="person", label="person", confirmed=True, last_ts=t, moving=True, score=0.8,
                    max_score=0.8, counted=False, in_zone=False, center=(x, 0.5), area=w * h, vel=np.zeros(2),
                    first_ts=0.0, event_id=None, box=np.array([x - w / 2, 0.5 - h / 2, x + w / 2, 0.5 + h / 2]))]
                if reach_t is None and h >= 0.8 * min(TARGET, 0.1 * ratio ** (12800 / ptz.ZOOM_RANGE)):
                    reach_t = t
            at.update(tracks, t)
            # Scored only while the walker is inside the eye's view: both cameras look through the same window,
            # so the eye sees everything the PTZ can usefully reach.
            if t > 3.0 and 0.0 < 0.5 + ang / EYE_HFOV < 1.0:
                if tracks:
                    inside += 1
                    errs.append(abs(x - 0.5))
                    heights.append(h)
                else:
                    lost += 1
        return {"in_frame": inside / max(1, inside + lost), "err": float(np.mean(errs)) if errs else 1.0,
                "height": float(np.median(heights)) if heights else 0.0,
                "overshoot": max(heights) / TARGET if heights else 0.0,
                "reach": reach_t if reach_t is not None else float("inf"), "reversals": cam.reversals,
                "aims": cam.aims}


def summary(name, rows):
    inf = np.array([m["in_frame"] for m in rows])
    hts = np.array([m["height"] for m in rows])
    ovs = np.array([m["overshoot"] for m in rows])
    rev = np.array([m["reversals"] for m in rows])
    reach = np.array([m["reach"] for m in rows])
    print(f"{name}: in frame {inf.mean():.0%} (losing the person: {int((inf < 0.9).sum())}/{len(rows)}), "
          f"median height {np.median(hts):.2f}, overshoot median {np.median(ovs):.2f} worst {ovs.max():.2f} "
          f"(over 1.2x: {int((ovs > 1.2).sum())}), zoom reversals median {int(np.median(rev))} max {rev.max()}, "
          f"reached in {np.median(reach[np.isfinite(reach)]):.1f} s median ({int(np.isinf(reach).sum())} never)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-v", "--verbose", action="store_true", help="print every case")
    ap.add_argument("--calibrated", action="store_true", help="give the controller the true lens curve")
    ap.add_argument("--compare", help="an older ptz.py to run through the same cases")
    ap.add_argument("--eye", action="store_true", help="also run eye mode (the fixed eye camera aims the PTZ)")
    args = ap.parse_args()
    mods = [("current", ptz)]
    if args.compare:
        spec = importlib.util.spec_from_file_location("ptzcam.ptz_old", args.compare)
        old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old)
        mods.insert(0, ("compare", old))
    keys = list(GRID)
    for name, mod in mods:
        rows = []
        for combo in itertools.product(*GRID.values()):
            m = run(mod, *combo, calibrated=args.calibrated)
            rows.append(m)
            if args.verbose:
                print(name, " ".join(f"{k}={v}" for k, v in zip(keys, combo)),
                      f"in frame {m['in_frame']:4.0%} height {m['height']:.2f} overshoot {m['overshoot']:.2f} "
                      f"reversals {m['reversals']} reach {m['reach']:.1f}s")
        summary(name, rows)
    if args.eye:
        rows = []
        for combo in itertools.product(*EYE_GRID.values()):
            m = run_eye(ptz, *combo, calibrated=args.calibrated)
            rows.append(m)
            if args.verbose:
                print("eye", " ".join(f"{k}={v}" for k, v in zip(EYE_GRID, combo)),
                      f"in frame {m['in_frame']:4.0%} err {m['err']:.2f} height {m['height']:.2f} "
                      f"overshoot {m['overshoot']:.2f} aims {m['aims']} reach {m['reach']:.1f}s")
        summary("eye mode", rows)


if __name__ == "__main__":
    main()
