"""Camera sync: where the PTZ camera must point, and how far it must zoom, to look at something the eye sees.

The eye is a fixed wide camera beside the PTZ camera at the same window. A calibration (tools/calibrate_sync.py) moves
the PTZ over a grid of absolute pan/tilt positions at one zoom and matches image features between the two cameras. For
each position it records where the centre of the PTZ's picture lands in the eye's picture (u, v, as fractions of the
eye frame) and how big the PTZ's picture is there (fw, fh, the same units). From those samples:

- `to_ptz(u, v)`: the pan/tilt (VISCA units) that centres the PTZ on eye position (u, v);
- `to_eye(pan, tilt)`: the reverse, to draw the PTZ's view on the eye's picture;
- `zoom_for(u, v, box_h, target, curve)`: the zoom position that makes an object `box_h` tall in the eye's picture
  `target` tall in the PTZ's picture. The eye is fixed, so its measurement does not lag or move with the PTZ.

Both directions are smooth 2-D polynomials fitted by least squares. Their degree follows the number of samples, and
they absorb the eye lens's distortion and the small offset between the cameras. Parallax is not modelled: the cameras
are a few centimetres apart and the street is metres away, so the PTZ's own picture corrects the small remainder
when it can see the target.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .ptz import mag_at, pos_for

VERSION = 1
MIN_SAMPLES = 6  # a quadratic surface has 6 terms


def _terms(u: np.ndarray, v: np.ndarray, degree: int) -> np.ndarray:
    """Polynomial features u^i v^j (i + j <= degree) of coordinates centred on the frame (better conditioned)."""
    x, y = 2.0 * u - 1.0, 2.0 * v - 1.0
    return np.stack([x ** i * y ** (d - i) for d in range(degree + 1) for i in range(d, -1, -1)], axis=-1)


def _degree_for(n: int) -> int:
    return 3 if n >= 16 else 2 if n >= MIN_SAMPLES else 1


@dataclass
class _Poly:
    degree: int
    coef: np.ndarray  # (terms, outputs)

    @classmethod
    def fit(cls, u, v, out, degree: int, ridge: float = 1e-6):
        a = _terms(np.asarray(u, float), np.asarray(v, float), degree)
        reg = ridge * len(a) * np.eye(a.shape[1])
        reg[0, 0] = 0.0  # never shrink the constant term
        coef = np.linalg.solve(a.T @ a + reg, a.T @ np.asarray(out, float))
        return cls(degree, coef)

    def __call__(self, u, v):
        return _terms(np.asarray(u, float), np.asarray(v, float), self.degree) @ self.coef


@dataclass
class SyncMap:
    """The fitted eye <-> PTZ mapping. `samples` rows: u, v, pan, tilt, fw, fh (fw/fh may be NaN)."""
    samples: np.ndarray
    ptz_zoom: int
    eye_size: tuple[int, int]
    tilt_limits: tuple[float, float] | None = None  # the PTZ's reachable tilt range, as found by the calibration
    created: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        s = np.asarray(self.samples, float).reshape(-1, 6)
        if len(s) < 3:
            raise ValueError(f"sync needs at least 3 samples, got {len(s)}")
        self.samples = s
        d = _degree_for(len(s))
        self._to_ptz = _Poly.fit(s[:, 0], s[:, 1], s[:, 2:4], d)
        # The reverse map takes pan/tilt in VISCA units: scale them into roughly 0..1 for the same polynomial code.
        self._pt_lo, self._pt_span = s[:, 2:4].min(0), np.maximum(np.ptp(s[:, 2:4], 0), 1.0)
        pt = (s[:, 2:4] - self._pt_lo) / self._pt_span
        self._to_eye = _Poly.fit(pt[:, 0], pt[:, 1], s[:, 0:2], d)
        ok = np.isfinite(s[:, 4:6]).all(1)
        self._footprint = _Poly.fit(s[ok, 0], s[ok, 1], s[ok, 4:6], _degree_for(int(ok.sum()))) if ok.sum() >= 3 else None
        self._hull = _hull(s[:, 0:2])

    # ---- the three questions ----
    def to_ptz(self, u: float, v: float) -> tuple[int, int]:
        pan, tilt = (float(x) for x in self._to_ptz(u, v))
        if self.tilt_limits:
            tilt = min(max(tilt, self.tilt_limits[0]), self.tilt_limits[1])
        return round(pan), round(tilt)

    def to_eye(self, pan: float, tilt: float) -> tuple[float, float]:
        p = (np.array([pan, tilt], float) - self._pt_lo) / self._pt_span
        u, v = (float(x) for x in self._to_eye(p[0], p[1]))
        return u, v

    def footprint(self, u: float, v: float) -> tuple[float, float] | None:
        """The PTZ picture's width and height in the eye's picture at (u, v), at the calibration zoom."""
        if self._footprint is None:
            return None
        fw, fh = (float(x) for x in self._footprint(u, v))
        return (fw, fh) if fw > 0 and fh > 0 else None

    def zoom_for(self, u: float, v: float, box_h: float, target: float, curve=None, max_pos: int | None = None):
        """Zoom position that shows an object `box_h` tall (fraction of the eye frame) at `target` of the PTZ frame.
        None when the footprint is unknown or the box is empty."""
        fp = self.footprint(u, v)
        if fp is None or box_h <= 0:
            return None
        mag = mag_at(self.ptz_zoom, curve or []) * fp[1] * target / box_h
        pos = pos_for(max(mag, 1.0), curve or [])
        return min(pos, max_pos) if max_pos is not None else pos

    def ptz_view(self, pan: float, tilt: float, zoom: int, curve=None):
        """The PTZ picture's outline in the eye's picture: (u0, v0, u1, v1), for drawing it on the wide view."""
        u, v = self.to_eye(pan, tilt)
        fp = self.footprint(u, v)
        if fp is None:
            return None
        k = mag_at(self.ptz_zoom, curve or []) / mag_at(zoom, curve or [])
        w, h = fp[0] * k, fp[1] * k
        return u - w / 2, v - h / 2, u + w / 2, v + h / 2

    def covers(self, u: float, v: float, margin: float = 0.05) -> bool:
        """Whether (u, v) lies inside the calibrated area (plus a margin): outside it the map extrapolates."""
        return _inside(self._hull, u, v, margin)

    # ---- quality ----
    def residuals(self) -> dict:
        """Leave-one-out errors: how far off a point the calibration never saw would be (pan/tilt units, eye frame)."""
        s = self.samples
        n = len(s)
        if n <= MIN_SAMPLES:
            return {}
        d = _degree_for(n - 1)
        errs_pt, errs_uv = [], []
        for i in range(n):
            keep = np.arange(n) != i
            f = _Poly.fit(s[keep, 0], s[keep, 1], s[keep, 2:4], d)
            errs_pt.append(np.abs(f(s[i, 0], s[i, 1]) - s[i, 2:4]))
            pt = (s[keep, 2:4] - self._pt_lo) / self._pt_span
            g = _Poly.fit(pt[:, 0], pt[:, 1], s[keep, 0:2], d)
            errs_uv.append(np.abs(g(*((s[i, 2:4] - self._pt_lo) / self._pt_span)) - s[i, 0:2]))
        e_pt, e_uv = np.array(errs_pt), np.array(errs_uv)
        return {"pan_median": float(np.median(e_pt[:, 0])), "pan_max": float(e_pt[:, 0].max()),
                "tilt_median": float(np.median(e_pt[:, 1])), "tilt_max": float(e_pt[:, 1].max()),
                "eye_median": float(np.median(np.hypot(e_uv[:, 0], e_uv[:, 1]))),
                "eye_max": float(np.hypot(e_uv[:, 0], e_uv[:, 1]).max())}

    # ---- storage ----
    def to_json(self) -> dict:
        return {"version": VERSION, "created": self.created, "ptz_zoom": self.ptz_zoom, "eye_size": list(self.eye_size),
                "tilt_limits": list(self.tilt_limits) if self.tilt_limits else None, "meta": self.meta,
                "samples": [[None if not math.isfinite(x) else round(float(x), 6) for x in row] for row in self.samples]}

    @classmethod
    def from_json(cls, d: dict) -> "SyncMap":
        if d.get("version") != VERSION:
            raise ValueError(f"unsupported sync file version {d.get('version')}")
        samples = np.array([[math.nan if x is None else x for x in row] for row in d["samples"]], float)
        return cls(samples, int(d["ptz_zoom"]), tuple(d["eye_size"]),
                   tuple(d["tilt_limits"]) if d.get("tilt_limits") else None, d.get("created", 0.0), d.get("meta", {}))

    def save(self, path: Path):
        tmp = Path(path).with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=1))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "SyncMap | None":
        try:
            return cls.from_json(json.loads(Path(path).read_text()))
        except FileNotFoundError:
            return None


def _hull(pts: np.ndarray) -> np.ndarray:
    """Convex hull (Andrew's monotone chain), counter-clockwise."""
    p = sorted(map(tuple, np.round(pts, 6)))
    if len(p) <= 2:
        return np.array(p)

    def half(points):
        h = []
        for q in points:
            while len(h) >= 2 and (h[-1][0] - h[-2][0]) * (q[1] - h[-2][1]) - (h[-1][1] - h[-2][1]) * (q[0] - h[-2][0]) <= 0:
                h.pop()
            h.append(q)
        return h

    lower, upper = half(p), half(reversed(p))
    return np.array(lower[:-1] + upper[:-1])


def _inside(hull: np.ndarray, u: float, v: float, margin: float) -> bool:
    if len(hull) < 3:
        return False
    c = hull.mean(0)
    grown = c + (hull - c) * (1.0 + 2.0 * margin)  # grow about the centroid: a margin as a share of the size
    n = len(grown)
    for i in range(n):
        a, b = grown[i], grown[(i + 1) % n]
        if (b[0] - a[0]) * (v - a[1]) - (b[1] - a[1]) * (u - a[0]) < 0:
            return False
    return True
