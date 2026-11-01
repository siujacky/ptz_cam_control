"""Multi-object tracker: ByteTrack-style two-stage IoU association with constant-velocity prediction.

Positions are normalized (0..1). Motion tests use "iso" coordinates where x is scaled by the frame
aspect ratio, so distances are isotropic and measured in frame heights.
"""
from __future__ import annotations

from collections import deque

import numpy as np

from .config import COCO_LABELS

TRACK_THRESH = 0.35   # detections at/above this start tracks and match in stage 1
IOU_STAGE1 = 0.3
IOU_STAGE2 = 0.4      # stage 2: low-score detections may only rescue confirmed tracks
DIST_FALLBACK = 0.6   # centre distance / object size, for small fast objects with no overlap
MAX_LOST = 2.0        # seconds a confirmed track survives unseen
TENTATIVE_MAX_MISSES = 3
MIN_MOVE_ABS = 0.015  # minimum travel, in frame heights, before anything counts as moving
MOVE_SUSTAIN = 3      # the travel test must hold this many updates in a row (rejects occlusion glitches)
AREA_MOVE_RATIO = 2.0  # approaching/leaving: box area differs from the anchor by this factor...
AREA_MOVE_MIN_S = 1.0  # ...continuously for at least this long


def iso(box, aspect: float):
    """Centre and size of a normalized box in isotropic units (x scaled by the frame aspect ratio)."""
    c = np.array([(box[0] + box[2]) / 2 * aspect, (box[1] + box[3]) / 2])
    return c, float(max((box[2] - box[0]) * aspect, box[3] - box[1]))


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    x1 = np.maximum(a[:, None, 0], b[None, :, 0])
    y1 = np.maximum(a[:, None, 1], b[None, :, 1])
    x2 = np.minimum(a[:, None, 2], b[None, :, 2])
    y2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / np.maximum(area_a[:, None] + area_b[None, :] - inter, 1e-9)


class Track:
    def __init__(self, tid: int, det, ts: float, aspect: float):
        self.id = tid
        self.category = det.category
        self.votes = {det.cls: det.score}
        self.box = np.array(det.box, dtype=np.float64)
        self.vel = np.zeros(2)  # centre velocity, normalized units per second
        self.score = det.score
        self.max_score = det.score
        self.hits = 1
        self.misses = 0
        self.first_ts = ts
        self.last_ts = ts
        self.confirmed = False
        self.moving = False
        self.counted = False
        self.in_zone = False
        self.cam_moved = False  # updated while the PTZ moved: image motion is not object motion
        self.event_id = None
        self.db_id = None
        self.best = None  # best frame for snapshots, managed by EventManager
        self.trail = deque(maxlen=40)
        self._aspect = aspect
        self.reanchor(ts)
        self.trail.append(self.center)

    @property
    def label(self) -> str:
        return COCO_LABELS[max(self.votes, key=self.votes.get)]

    @property
    def center(self):
        b = self.box
        return (float(b[0] + b[2]) / 2, float(b[1] + b[3]) / 2)

    @property
    def area(self) -> float:
        b = self.box
        return max(float((b[2] - b[0]) * (b[3] - b[1])), 1e-9)

    def reanchor(self, ts: float):
        """Restart motion measurement from the current position (used after the camera moves)."""
        self.anchor, self.anchor_size = iso(self.box, self._aspect)
        self.anchor_area = self.area
        self.anchor_ts = ts
        self._move_hits = 0
        self._area_since = None

    def predict(self, ts: float) -> np.ndarray:
        dt = min(max(ts - self.last_ts, 0.0), 1.0)
        dx, dy = self.vel * dt
        return self.box + np.array([dx, dy, dx, dy])

    def update(self, det, ts: float, min_hits: int, min_move: float, motion_frozen: bool):
        dt = max(ts - self.last_ts, 1e-3)
        pred = self.predict(ts)
        old_c = np.array(self.center)
        self.box = (pred + 0.7 * (det.box - pred)).clip(0.0, 1.0)  # blend prediction and measurement
        inst = (np.array(self.center) - old_c) / dt
        self.vel = 0.5 * inst if self.hits == 1 else 0.6 * self.vel + 0.4 * inst
        self.votes[det.cls] = self.votes.get(det.cls, 0.0) + det.score
        self.score = det.score
        self.max_score = max(self.max_score, det.score)
        self.hits += 1
        self.misses = 0
        self.last_ts = ts
        if not self.confirmed and self.hits >= min_hits:
            self.confirmed = True
        self.trail.append(self.center)
        if motion_frozen:
            self.cam_moved = True
        elif not self.moving:
            self._check_moving(ts, min_move)

    def _check_moving(self, ts: float, min_move: float):
        c, sz = iso(self.box, self._aspect)
        disp = float(np.hypot(*(c - self.anchor)))
        size = 0.5 * (self.anchor_size + sz)
        if disp >= max(min_move * size, MIN_MOVE_ABS):
            self._move_hits += 1
            if self._move_hits >= MOVE_SUSTAIN:
                self.moving = True
                return
        else:
            self._move_hits = 0
        r = self.area / max(self.anchor_area, 1e-9)
        if r >= AREA_MOVE_RATIO or r <= 1 / AREA_MOVE_RATIO:
            if self._area_since is None:
                self._area_since = ts
            elif ts - self._area_since >= AREA_MOVE_MIN_S:
                self.moving = True
        else:
            self._area_since = None

    def inherit(self, old: "Track"):
        """`old` was lost and this track is the same object found again (e.g. after the camera turned): take over
        its count and event, so the object is not counted twice and its event keeps getting snapshots."""
        if old.counted and not self.counted:
            self.counted, self.event_id, self.db_id = True, old.event_id, old.db_id
            old.db_id = None  # this track now writes the final state
        self.in_zone = self.in_zone or old.in_zone
        self.cam_moved = True  # keep the count-time direction
        self.max_score = max(self.max_score, old.max_score)
        for cls, score in old.votes.items():
            self.votes[cls] = self.votes.get(cls, 0.0) + score

    def direction(self) -> str:
        c, sz = iso(self.box, self._aspect)
        d = c - self.anchor
        size = max(0.5 * (self.anchor_size + sz), 1e-6)
        ratio = self.area / max(self.anchor_area, 1e-9)
        if abs(d[0]) >= 0.5 * size and abs(d[0]) >= 0.8 * abs(d[1]):
            return "ltr" if d[0] > 0 else "rtl"
        if ratio >= 1.4 or d[1] >= 0.5 * size:
            return "approach"
        if ratio <= 0.7 or d[1] <= -0.5 * size:
            return "leave"
        return "unknown"

    def to_json(self, target_id=None) -> dict:
        return {
            "id": self.id, "category": self.category, "label": self.label, "score": round(self.score, 3),
            "box": [round(float(v), 4) for v in self.box], "moving": self.moving, "counted": self.counted,
            "target": self.id == target_id,
        }

    def overlay_info(self) -> dict:
        return {
            "id": self.id, "category": self.category, "label": self.label, "score": self.score,
            "box": self.box.copy(), "moving": self.moving, "counted": self.counted, "trail": list(self.trail),
        }


class Tracker:
    def __init__(self, aspect: float, min_hits: int = 4, min_move: float = 0.5, first_id: int = 1):
        self.aspect = aspect
        self.min_hits = min_hits
        self.min_move = min_move
        self.motion_frozen = False  # set while the PTZ moves: camera motion must not count as object motion
        self.tracks: list[Track] = []
        self._next_id = first_id  # ids never repeat within a process, even across restarts

    def reset(self):
        self.tracks = []

    def reanchor(self, ts: float):
        for t in self.tracks:
            t.reanchor(ts)

    def update(self, dets: list, ts: float, thresh: dict | None = None):
        """Associate detections with tracks. Returns (live_tracks, removed_confirmed_tracks).
        `thresh` optionally lowers the track-start threshold per category (night sensitivity)."""
        by_cat: dict[str, list] = {}
        for d in dets:
            by_cat.setdefault(d.category, []).append(d)
        matched: set[int] = set()
        born: list[Track] = []
        for cat in set(by_cat) | {t.category for t in self.tracks}:
            tracks = [t for t in self.tracks if t.category == cat]
            cdets = by_cat.get(cat, [])
            start = (thresh or {}).get(cat, TRACK_THRESH)
            high = [d for d in cdets if d.score >= start]
            low = [d for d in cdets if d.score < start]
            preds = np.array([t.predict(ts) for t in tracks]) if tracks else np.zeros((0, 4))

            pairs, free_t, free_d = self._match(preds, high, IOU_STAGE1, fallback=True)
            for ti, di in pairs:
                tracks[ti].update(high[di], ts, self.min_hits, self.min_move, self.motion_frozen)
                matched.add(tracks[ti].id)

            rescue = [i for i in free_t if tracks[i].confirmed]
            if rescue and low:
                pairs2, _, _ = self._match(preds[rescue], low, IOU_STAGE2, fallback=False)
                for ri, di in pairs2:
                    t = tracks[rescue[ri]]
                    t.update(low[di], ts, self.min_hits, self.min_move, self.motion_frozen)
                    matched.add(t.id)

            for di in free_d:
                born.append(Track(self._next_id, high[di], ts, self.aspect))
                self._next_id += 1

        alive, removed = [], []
        for t in self.tracks:
            if t.id not in matched:
                t.misses += 1
                if not t.confirmed:
                    if t.misses >= TENTATIVE_MAX_MISSES:
                        continue  # never confirmed: drop silently
                elif ts - t.last_ts > MAX_LOST:
                    removed.append(t)
                    continue
            alive.append(t)
        self.tracks = alive + born
        return self.tracks, removed

    def _match(self, preds: np.ndarray, dets: list, iou_thresh: float, fallback: bool):
        n, m = len(preds), len(dets)
        if n == 0 or m == 0:
            return [], list(range(n)), list(range(m))
        dboxes = np.array([d.box for d in dets])
        iou = iou_matrix(preds, dboxes)
        free_t, free_d = set(range(n)), set(range(m))
        pairs = []
        for flat in np.argsort(-iou, axis=None):
            ti, di = divmod(int(flat), m)
            if iou[ti, di] < iou_thresh:
                break
            if ti in free_t and di in free_d:
                pairs.append((ti, di))
                free_t.discard(ti)
                free_d.discard(di)
        if fallback and free_t and free_d:
            cand = []
            for ti in free_t:
                pc, ps = iso(preds[ti], self.aspect)
                for di in free_d:
                    dc, ds = iso(dboxes[di], self.aspect)
                    if not 0.5 <= ds / max(ps, 1e-6) <= 2.0:
                        continue
                    ratio = float(np.hypot(*(pc - dc))) / max(ps, ds, 1e-6)
                    if ratio < DIST_FALLBACK:
                        cand.append((ratio, ti, di))
            for _, ti, di in sorted(cand):
                if ti in free_t and di in free_d:
                    pairs.append((ti, di))
                    free_t.discard(ti)
                    free_d.discard(di)
        return pairs, sorted(free_t), sorted(free_d)

