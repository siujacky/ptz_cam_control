"""Measuring the sync between the eye camera and the PTZ camera (see sync.py), from the pictures themselves.

`match_views` finds where the PTZ's picture lies inside the eye's picture by matching SIFT features (the cameras differ
in sensor, colour and exposure, so the pictures are equalised first) and fitting a homography with RANSAC.
`calibrate_sync` moves the PTZ over a grid of absolute pan/tilt positions that covers the eye's view and records one
sample per position. It needs daylight, and tools/calibrate_sync.py runs it with the service stopped.
"""
from __future__ import annotations

import time

import cv2
import numpy as np

from .sync import MIN_SAMPLES, SyncMap

MATCH_PTZ_W, MATCH_EYE_W = 960, 1280
MIN_INLIERS = 25
PROBE_PAN, PROBE_TILT = 150, 80  # first small moves, to learn which way the eye's coordinates run


def _prep(img: np.ndarray, width: int, blur: float = 0.0) -> np.ndarray:
    h, w = img.shape[:2]
    g = cv2.cvtColor(cv2.resize(img, (width, round(h * width / w)), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
    if blur:
        g = cv2.GaussianBlur(g, (0, 0), blur)
    return cv2.createCLAHE(2.0, (8, 8)).apply(g)  # the two cameras expose and colour the same scene differently


def match_views(ptz_img: np.ndarray, eye_img: np.ndarray, min_inliers: int = MIN_INLIERS):
    """Where the PTZ's picture lies in the eye's: (u, v, fw, fh, inliers), all fractions of the eye frame (centre,
    width, height), or None when the pictures don't match well enough. If the sharp pictures do not match, they are
    tried again lightly blurred: the fly screen in front of the PTZ adds a fine grid of features of its own."""
    for blur in (0.0, 1.5):
        m = _match(ptz_img, eye_img, min_inliers, blur)
        if m is not None:
            return m
    return None


def _match(ptz_img, eye_img, min_inliers, blur):
    a, b = _prep(ptz_img, MATCH_PTZ_W, blur), _prep(eye_img, MATCH_EYE_W, blur)
    sift = cv2.SIFT_create(4000)
    ka, da = sift.detectAndCompute(a, None)
    kb, db = sift.detectAndCompute(b, None)
    if da is None or db is None or len(ka) < min_inliers or len(kb) < min_inliers:
        return None
    good = [p[0] for p in cv2.BFMatcher().knnMatch(da, db, k=2) if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(good) < min_inliers:
        return None
    h, mask = cv2.findHomography(np.float32([ka[m.queryIdx].pt for m in good]),
                                 np.float32([kb[m.trainIdx].pt for m in good]), cv2.RANSAC, 4.0)
    if h is None or mask is None or int(mask.sum()) < min_inliers:
        return None
    ah, aw = a.shape
    bh, bw = b.shape
    quad = cv2.perspectiveTransform(np.float32([[[0, 0]], [[aw, 0]], [[aw, ah]], [[0, ah]], [[aw / 2, ah / 2]]]), h)
    quad = quad.reshape(-1, 2) / [bw, bh]
    c0, c1, c2, c3, centre = quad
    fw = (np.linalg.norm(c1 - c0) + np.linalg.norm(c2 - c3)) / 2
    fh = (np.linalg.norm(c3 - c0) + np.linalg.norm(c2 - c1)) / 2
    # Sanity: a convex quad of the PTZ's shape, mostly inside the eye's picture. A wrong homography fails this.
    edges = [quad[(i + 1) % 4] - quad[i] for i in range(4)]
    cross = [edges[i][0] * edges[(i + 1) % 4][1] - edges[i][1] * edges[(i + 1) % 4][0] for i in range(4)]
    ratio = (fw * bw) / max(fh * bh, 1e-9) / (aw / ah)
    if not (all(c > 0 for c in cross) or all(c < 0 for c in cross)) or not 0.75 < ratio < 1.33 \
            or not (-0.1 < centre[0] < 1.1 and -0.1 < centre[1] < 1.1) or fw <= 0.01 or fh <= 0.01:
        return None
    return float(centre[0]), float(centre[1]), float(fw), float(fh), int(mask.sum())


def _serpentine(us, vs):
    order = []
    for j, v in enumerate(vs):
        row = list(us) if j % 2 == 0 else list(reversed(us))
        order += [(u, v) for u in row]
    return order


def calibrate_sync(ptz, ptz_frame, eye_frame, *, zoom: int = 0, grid=(7, 4), cover=(0.08, 0.92, 0.12, 0.88),
                   settle_s: float = 1.5, log=print) -> SyncMap:
    """Sweep the PTZ over the eye's view and fit the map.

    `ptz` provides move(pan, tilt) (absolute; returns when the camera has stopped), position() -> (pan, tilt, zoom),
    zoom_to(pos) (returns when arrived) and home(). `ptz_frame()` / `eye_frame()` return fresh BGR frames."""

    def measure(pan, tilt):
        ptz.move(pan, tilt)
        time.sleep(settle_s)  # autofocus and stream lag
        p, t, _ = ptz.position()
        m = match_views(ptz_frame(), eye_frame())
        return (p, t, m) if m else (p, t, None)

    ptz.home()
    pan0, tilt0, _ = ptz.position()
    ptz.zoom_to(zoom)
    rows, reached_tilts = [], []

    def add(p, t, m, why):
        reached_tilts.append(t)
        if m is None:
            log(f"  pan {p:5d} tilt {t:5d}: no match ({why})")
            return
        u, v, fw, fh, n = m
        rows.append([u, v, p, t, fw, fh])
        log(f"  pan {p:5d} tilt {t:5d}: eye ({u:.3f}, {v:.3f}), PTZ view {fw:.3f} x {fh:.3f} of the eye, {n} inliers ({why})")

    p, t, m0 = measure(pan0, tilt0)
    add(p, t, m0, "Home")
    if m0 is None:
        raise RuntimeError("the PTZ's Home view was not found in the eye's picture: is it light enough, and do both "
                           "cameras look through the same window?")
    # Which way, and how fast, the eye's coordinates move per pan/tilt unit (a local Jacobian).
    pp, pt_, mp = measure(pan0 + PROBE_PAN, tilt0)
    add(pp, pt_, mp, "probe pan")
    tp, tt, mt = measure(pan0, tilt0 + PROBE_TILT)
    add(tp, tt, mt, "probe tilt")
    if mp is None or mt is None or pp == pan0 or tt == tilt0:
        raise RuntimeError("the probing moves could not be measured; nothing to aim the grid with")
    jac = np.array([[(mp[0] - m0[0]) / (pp - p), (mt[0] - m0[0]) / (tt - t)],
                    [(mp[1] - m0[1]) / (pp - p), (mt[1] - m0[1]) / (tt - t)]])
    if abs(np.linalg.det(jac)) < 1e-12:
        raise RuntimeError("pan and tilt moved the PTZ's view the same way in the eye's picture")
    inv = np.linalg.inv(jac)

    us = np.linspace(cover[0], cover[1], grid[0])
    vs = np.linspace(cover[2], cover[3], grid[1])
    for u, v in _serpentine(us, vs):
        if len(rows) >= MIN_SAMPLES:  # aim with the map fitted so far: better than the local Jacobian far from Home
            fit = SyncMap(np.array(rows), zoom, (1, 1))
            aim_p, aim_t = fit.to_ptz(u, v)
        else:
            d = inv @ (np.array([u, v]) - np.array(m0[:2]))
            aim_p, aim_t = round(p + d[0]), round(t + d[1])
        rp, rt, m = measure(aim_p, aim_t)
        add(rp, rt, m, f"aim ({u:.2f}, {v:.2f})")

    ptz.home()
    if len(rows) < MIN_SAMPLES:
        raise RuntimeError(f"only {len(rows)} positions matched; at least {MIN_SAMPLES} are needed")
    s = np.array(rows)
    m = SyncMap(s, zoom, (1, 1))
    # One wrong match bends the whole surface: drop samples far off the others' fit, once.
    res = []
    for i in range(len(s)):
        keep = np.arange(len(s)) != i
        if keep.sum() >= MIN_SAMPLES:
            f = SyncMap(s[keep], zoom, (1, 1))
            res.append(max(abs(f.to_ptz(s[i, 0], s[i, 1])[0] - s[i, 2]), abs(f.to_ptz(s[i, 0], s[i, 1])[1] - s[i, 3])))
    if res:
        res = np.array(res)
        bad = res > max(4 * np.median(res), 15)
        if bad.any() and (~bad).sum() >= MIN_SAMPLES:
            log(f"  dropping {int(bad.sum())} sample(s) that disagree with the rest")
            m = SyncMap(s[~bad], zoom, (1, 1))
    tilts = reached_tilts or [tilt0]
    m.tilt_limits = (float(min(tilts)), float(max(tilts)))
    return m
