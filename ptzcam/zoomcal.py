"""Measuring the lens: magnification at each VISCA zoom position, from the picture itself.

Auto-zoom works out the zoom position that makes the followed object `target_size` tall from the object's
height in the picture and this curve, and sends one absolute zoom command (VISCA zoom direct) that the camera
stops at exactly. The curve belongs to the lens, so it is measured once (tools/calibrate_zoom.py) and kept in
the config as `ptz.zoom_curve`, with the zoom-direct speed as `ptz.zoom_rate`.
"""
from __future__ import annotations

import math
import statistics
import time

import cv2
import numpy as np

from .ptz import ZOOM_ARRIVED, ZOOM_RANGE, _from_nibbles, _nibbles

MATCH_WIDTH = 640
MIN_INLIERS = 12


def _prep(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    small = cv2.resize(frame, (MATCH_WIDTH, round(h * MATCH_WIDTH / w)), interpolation=cv2.INTER_AREA)
    # Blur the window mesh's fine grid: its identical crossings would match anywhere.
    return cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (0, 0), 1.5)


def estimate_scale(prev: np.ndarray, cur: np.ndarray) -> float | None:
    """How much larger `cur` shows the scene than `prev` (above 1 when zoomed in), from matched ORB features and
    a RANSAC similarity fit (a zoom also shifts the picture a little: the optical axis is not exactly central).
    None when too few features agree."""
    orb = cv2.ORB_create(3000)
    ka, da = orb.detectAndCompute(_prep(prev), None)
    kb, db = orb.detectAndCompute(_prep(cur), None)
    if da is None or db is None or len(ka) < MIN_INLIERS or len(kb) < MIN_INLIERS:
        return None
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(good) < MIN_INLIERS:
        return None
    pa = np.float32([ka[m.queryIdx].pt for m in good])
    pb = np.float32([kb[m.trainIdx].pt for m in good])
    m, inliers = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=3.0)
    if m is None or inliers is None or int(inliers.sum()) < MIN_INLIERS:
        return None
    return float(math.hypot(m[0, 0], m[1, 0]))


def calibrate(visca, latest_frame, step: int = 1024, settle_s: float = 2.0, log=print):
    """Step zoom direct from wide to full zoom; returns (curve [[pos, mag], ...], zoom-direct speed).

    `visca` is a ptz.ViscaClient, `latest_frame()` returns the newest BGR frame. After each step the lens
    must read back at its goal and the picture is given `settle_s` for autofocus and stream lag."""

    def zoom_pos():
        r = visca.send(b"\x09\x04\x47")
        return _from_nibbles(r[2:6]) if r and len(r) >= 7 else None

    def drive(pos, timeout=10.0):
        t0 = time.monotonic()
        visca.send(b"\x01\x04\x47" + _nibbles(pos))
        last, steady = None, 0
        while time.monotonic() - t0 < timeout:
            time.sleep(0.1)
            p = zoom_pos()
            steady = steady + 1 if p is not None and p == last and abs(p - pos) <= ZOOM_ARRIVED else 0
            last = p
            if steady >= 2:  # positions read with lag while moving: wait until two readings agree at the goal
                return time.monotonic() - t0
        raise RuntimeError(f"the zoom did not reach {pos} (last read {last})")

    drive(0)
    time.sleep(settle_s)
    prev = latest_frame()
    curve, mag, rates = [[0, 1.0]], 1.0, []
    positions = list(range(step, ZOOM_RANGE, step)) + [ZOOM_RANGE]
    last_pos = 0
    for pos in positions:
        rates.append((pos - last_pos) / drive(pos))
        time.sleep(settle_s)
        cur = latest_frame()
        s = estimate_scale(prev, cur)
        if s is None or not 1.0 <= s <= 3.0:
            raise RuntimeError(f"could not measure the zoom from {last_pos} to {pos} (scale {s})")
        mag *= s
        curve.append([pos, round(mag, 4)])
        log(f"zoom {pos:5d}: x{mag:5.2f}  (step x{s:.3f})")
        prev, last_pos = cur, pos
    return curve, round(statistics.median(rates), 1)
