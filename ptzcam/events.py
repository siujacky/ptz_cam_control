"""Turns counted tracks into Ring-style events: snapshot, thumbnail and a 30 s clip."""
from __future__ import annotations

import logging
import math
import shutil
import threading
import time
from collections import Counter
from pathlib import Path

import numpy as np

from .config import CATEGORY_PRIORITY
from .draw import draw_box, thumb_crop, write_jpeg
from .storage import event_json, local_day

SNAPSHOT_IMPROVE = 1.2      # rewrite the snapshot when a frame is this much better...
SNAPSHOT_MIN_INTERVAL = 2.0  # ...but at most this often
BEST_MIN_INTERVAL = 0.25     # throttle best-frame bookkeeping per track


class Best:
    __slots__ = ("quality", "ts", "frame", "boxes", "box", "track_id")

    def __init__(self, quality, ts, frame, boxes, box, track_id):
        self.quality, self.ts, self.frame, self.boxes, self.box, self.track_id = quality, ts, frame, boxes, box, track_id


class ActiveEvent:
    def __init__(self, eid, start, end, rel_dir):
        self.id, self.start, self.end, self.rel_dir = eid, start, end, rel_dir
        self.categories = Counter()
        self.labels = Counter()
        self.max_score = 0.0
        self.snap_quality = 0.0
        self.snap_written = 0.0
        self.snap_camera = None  # the camera whose picture the photo is; None: an unusable one (washed out)
        self.last_seen = start


def quality(track, w, h) -> float:
    """Snapshot quality: confident, large, fully visible objects win."""
    b = track.box
    area_px = float((b[2] - b[0]) * w * (b[3] - b[1]) * h)
    edge = 0.6 if b[0] < 0.01 or b[1] < 0.01 or b[2] > 0.99 or b[3] > 0.99 else 1.0
    return track.score * math.sqrt(max(area_px, 1.0)) * edge


def point_in_polygon(x, y, poly) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


class EventManager:
    def __init__(self, cfg_store, storage, recorder, media_dir, bus, log=None, night_clipper=None,
                 wide_recorder=None):
        self.cfg = cfg_store
        self.storage = storage
        self.recorder = recorder  # the PTZ camera's ring: clip.mp4
        self.wide_recorder = wide_recorder  # the eye camera's ring, if there is one: clip_wide.mp4
        self.night_clipper = night_clipper
        self.night = False  # night vision is on (set by the pipeline every frame, from the main camera)
        self.main = "ptz"  # which camera the live view and photos come from (set by the pipeline)
        self._wide_marked = set()  # events already marked clip_wide_keep (the mark itself is in the database)
        # The wide camera's latest usable picture and its boxes, while it is main: the photo when the PTZ counts and
        # the two cannot be matched (no sync map yet), or when the PTZ's own picture is unusable.
        self.main_frame = None
        self.photo_from_main = False  # set by the pipeline while that is so
        self._media_lock = threading.Lock()  # the two clip builders decide on the wide clip one at a time
        self.media_dir = Path(media_dir)
        self.bus = bus
        self.log = log or logging.getLogger("ptzcam.events")
        self.active: ActiveEvent | None = None
        self._lock = threading.Lock()  # guards `active` against deletions from the web thread

    def process(self, ts, frame, tracks, removed, counting_allowed: bool, zone=None, usable: bool = True,
                camera: str = "ptz"):
        """`frame` is what snapshots show (the night-vision view when that is on). `counting_allowed` also
        means the zone is valid: the PTZ zone is drawn on the Home view, so image positions mean nothing while
        that camera is away or moving. With two cameras each calls this for its own tracks: `zone` is that
        camera's zone (default detect.zone), and `usable` is false while its picture is washed out, so no
        snapshot is taken from it. The photo comes from the main camera's picture when it has one of the object
        (its tracks join the event: the pipeline links them to the counting camera's), else from the counter's."""
        cfg = self.cfg.data
        h, w = frame.shape[:2]
        zone = cfg["detect"]["zone"] if zone is None else zone
        visible = [t for t in tracks if t.confirmed and t.last_ts == ts]
        if zone and counting_allowed:
            for t in visible:
                if not t.in_zone and point_in_polygon((t.box[0] + t.box[2]) / 2, t.box[3], zone):
                    t.in_zone = True  # ground-contact point entered the zone

        keep = max(float(cfg["events"]["preroll_seconds"]), 1.0)  # a best frame older than the pre-roll is stale
        with self._lock:
            if usable and camera == self.main:
                self.main_frame = (ts, frame, [(o.box.copy(), o.category, o.label, o.score, o.id) for o in visible])
            a = self.active
            for t in visible if usable else ():
                if t.counted and not (a and t.event_id == a.id):
                    continue
                q = quality(t, w, h)
                if t.best is None or ts - t.best.ts > keep or \
                        (q > t.best.quality * 1.05 and ts - t.best.ts >= BEST_MIN_INTERVAL):
                    boxes = [(o.box.copy(), o.category, o.label, o.score, o.id) for o in visible]
                    t.best = Best(q, ts, frame, boxes, t.box.copy(), t.id)

            if counting_allowed:
                cats = cfg["detect"]["categories"]
                for t in visible:
                    if t.counted or not t.moving:
                        continue
                    c = cats.get(t.category)
                    if not c or not c["enabled"] or t.max_score < c["min_score"]:
                        continue
                    if zone and not t.in_zone:
                        continue
                    self._count(t, ts, frame, cfg, camera, usable)

            a = self.active
            if a is not None:
                attached = [t for t in visible if t.event_id == a.id]
                if attached:
                    a.last_seen = ts
                if usable:
                    self._maybe_improve_snapshot(a, attached, camera)
                if ts >= a.end:
                    self._close_active()

        for t in removed:
            self._finalize_track(t)

    def close(self):
        with self._lock:
            if self.active is not None:
                self._close_active()

    def on_deleted(self, eid: int):
        with self._lock:
            if self.active is not None and self.active.id == eid:
                self.active = None

    # ---- internals (called with the lock held) ----
    def _count(self, t, ts, frame, cfg, camera="ptz", usable=True):
        t.counted = True
        a = self.active
        new = a is None or ts >= a.end
        if new:
            if a is not None:
                self._close_active()
            a = self._open_event(t, ts, cfg)
        t.event_id = a.id
        a.categories[t.category] += 1
        a.labels[t.label] += 1
        a.max_score = max(a.max_score, t.max_score)
        a.last_seen = ts
        t.db_id = self.storage.insert_track(a.id, t.id, t.category, t.label, t.first_ts, ts, t.last_ts,
                                            t.max_score, t.direction(), camera)
        top = min(a.categories, key=CATEGORY_PRIORITY.index)
        self.storage.update_event(a.id, top_category=top, categories=dict(a.categories), labels=dict(a.labels),
                                  max_score=round(a.max_score, 3), last_seen_ts=ts)
        if new:
            # The wide camera's view holds the PTZ's, so its picture of the moment shows what the PTZ counted; the
            # PTZ's view need not show what the wide camera counted (then its own picture of it is the photo).
            shot = self._main_shot(t, ts) if camera == "ptz" and self.main == "eye" else None
            if shot is not None and (self.photo_from_main or not usable):
                best, src = shot, self.main  # the main camera's own picture of the moment
            elif t.best is not None and t.best.ts >= a.start:
                best, src = t.best, camera
            else:  # a photo of this frame, which may be washed out: then any usable picture replaces it
                best, src = self._fallback_best(t, ts, frame), camera if usable else None
            self._write_snapshot(a, best, src)
        row = self.storage.get_event(a.id)
        if row is not None:
            self.bus.publish("event_new" if new else "event_update", event_json(row))
        self.bus.publish("counts", self.storage.counts_today())
        self.log.info("counted %s #%d (%s) -> event %d", t.label, t.id, t.direction(), a.id)

    def _open_event(self, t, ts, cfg) -> ActiveEvent:
        ev = cfg["events"]
        start = ts - ev["preroll_seconds"]
        end = start + ev["clip_seconds"]
        eid = self.storage.insert_event(start, ts, end, t.category, {}, {}, t.max_score)
        rel = f"{local_day(ts)}/{eid}"
        (self.media_dir / rel).mkdir(parents=True, exist_ok=True)
        self.storage.update_event(eid, snapshot=f"{rel}/snapshot.jpg", thumb=f"{rel}/thumb.jpg", clip=f"{rel}/clip.mp4")
        self.recorder.request_clip(eid, start, end, self.media_dir / rel / "clip.mp4", self._clip_done)
        self.storage.update_event(eid, main_camera=self.main)
        if self.wide_recorder is not None and cfg["eye"]["clips"] != "never":
            self.storage.update_event(eid, clip_wide=f"{rel}/clip_wide.mp4", clip_wide_status="pending")
            self.wide_recorder.request_clip(eid, start, end, self.media_dir / rel / "clip_wide.mp4",
                                            self._wide_clip_done)
        if self.night:
            self.storage.update_event(eid, night=1)
            if self.night_clipper is not None and self.night_clipper.active:  # its frames are being kept
                self.storage.update_event(eid, clip_night=f"{rel}/clip_night.mp4", clip_night_status="pending")
                self.night_clipper.request_clip(eid, start, end, self.media_dir / rel / "clip_night.mp4",
                                                self._night_clip_done)
        self.active = ActiveEvent(eid, start, end, rel)
        return self.active

    def _close_active(self):
        a = self.active
        self.active = None
        if a is not None:
            self.storage.update_event(a.id, last_seen_ts=a.last_seen)

    def _main_shot(self, t, ts):
        """The main camera's latest usable picture (at most a second old) as a photo, framed on its biggest box of
        the counted kind (it cannot tell which one is the counted object without the sync map)."""
        m = self.main_frame
        if m is None or ts - m[0] > 1.0:
            return None
        mts, frame, boxes = m
        same = [b for b in boxes if b[1] == t.category]
        if same:
            box, _, _, _, tid = max(same, key=lambda b: float((b[0][2] - b[0][0]) * (b[0][3] - b[0][1])))
        else:
            box, tid = np.array([0.3, 0.2, 0.7, 0.8]), None
        return Best(0.0, mts, frame, boxes, box.copy(), tid)

    def _fallback_best(self, t, ts, frame):
        boxes = [(t.box.copy(), t.category, t.label, t.score, t.id)]
        return Best(0.0, ts, frame, boxes, t.box.copy(), t.id)

    def _write_snapshot(self, a: ActiveEvent, best: Best, camera) -> bool:
        img = best.frame.copy()
        for box, cat, label, score, tid in best.boxes:
            draw_box(img, box, cat, f"{label} {score:.0%}", thickness=3 if tid == best.track_id else 2)
        d = self.media_dir / a.rel_dir
        try:
            write_jpeg(d / "snapshot.jpg", img, 85)
            write_jpeg(d / "thumb.jpg", thumb_crop(img, best.box), 82)
        except (OSError, RuntimeError) as e:  # e.g. the event was deleted meanwhile
            self.log.warning("snapshot for event %d not written: %s", a.id, e)
            return False
        a.snap_quality = best.quality
        a.snap_written = time.time()
        a.snap_camera = camera
        return True

    def _maybe_improve_snapshot(self, a: ActiveEvent, attached, camera="ptz"):
        """A better picture of the event's objects replaces the photo. The main camera's picture replaces the other
        camera's at once and is never replaced by it; a photo from a washed-out picture gives way to any other."""
        main = self.main
        if a.snap_camera == main and camera != main:
            return
        takeover = a.snap_camera is None or (camera == main and a.snap_camera != main)
        if not takeover and time.time() - a.snap_written < SNAPSHOT_MIN_INTERVAL:
            return
        need = 0.0 if takeover else a.snap_quality * SNAPSHOT_IMPROVE
        cands = [t for t in attached if t.best is not None and t.best.ts >= a.start and t.best.quality > need]
        if not cands:
            return
        best = max(cands, key=lambda t: t.best.quality).best
        if self._write_snapshot(a, best, camera):
            self.storage.bump_snapshot(a.id)
            row = self.storage.get_event(a.id)
            if row is not None:
                self.bus.publish("event_update", event_json(row))

    def _finalize_track(self, t):
        t.best = None
        if t.db_id is not None:
            # Keep the count-time label (it is what the event lists) and, if the camera moved meanwhile,
            # the count-time direction: image motion after a pan says nothing about the object.
            fields = {"last_ts": t.last_ts, "max_score": round(t.max_score, 3)}
            if not t.cam_moved:
                fields["direction"] = t.direction()
            self.storage.update_track(t.db_id, **fields)

    def _clip_done(self, eid: int, ok: bool, lead: float = 0.0):
        """Called from the PTZ ring's clip builder thread."""
        with self._media_lock:
            self._media_done(eid, clip_status="ready" if ok else "failed", clip_lead=round(lead, 3))
            if ok and self.cfg.data["eye"]["clips"] == "auto":
                row = self.storage.get_event(eid)  # a wide clip kept while this one was still being cut
                if row is not None and row["clip_wide_status"] == "ready" and _wide_wanted(row) is False:
                    self._drop_wide(eid, row)

    def wide_needed(self):
        """The PTZ is away from its Home view, offline or not main: the open event's PTZ clip does not show the whole
        scene, so (with eye.clips "auto") its wide clip is kept. Saved with the event, so it survives a restart."""
        with self._lock:
            a = self.active
            if a is None or a.id in self._wide_marked:
                return
            self._wide_marked.add(a.id)
            self.storage.update_event(a.id, clip_wide_keep=1)

    def _wide_clip_done(self, eid: int, ok: bool, lead: float = 0.0):
        """Called from the eye ring's clip builder thread. With eye.clips "auto", a wide clip of an event the PTZ
        filmed from its Home view the whole time, as main, adds nothing, so it is dropped to save storage. That is
        only known once the PTZ clip is ready: until then the wide clip is kept (see _clip_done)."""
        with self._media_lock:
            if ok and self.cfg.data["eye"]["clips"] == "auto":
                row = self.storage.get_event(eid)
                if row is not None and _wide_wanted(row) is False:
                    self._drop_wide(eid, row)
                    return
            self._media_done(eid, clip_wide_status="ready" if ok else "failed", clip_wide_lead=round(lead, 3))

    def _drop_wide(self, eid: int, row):
        if row["clip_wide"]:
            (self.media_dir / row["clip_wide"]).unlink(missing_ok=True)
        self._media_done(eid, clip_wide=None, clip_wide_status=None, clip_wide_lead=None)
        self.log.debug("event %d: wide clip not needed (the PTZ filmed it from Home)", eid)

    def _night_clip_done(self, eid: int, ok: bool, lead: float = 0.0):
        """Called from the night clip thread."""
        self._media_done(eid, clip_night_status="ready" if ok else "failed")

    def _media_done(self, eid: int, **fields):
        if self.storage.get_event(eid) is not None:
            self.storage.update_event(eid, **fields)
            row = self.storage.get_event(eid)
            if row is not None:
                self.bus.publish("event_update", event_json(row))
                return
        # Deleted while the clip was pending: drop the files (event ids are never reused).
        for day_dir in self.media_dir.iterdir() if self.media_dir.is_dir() else ():
            shutil.rmtree(day_dir / str(eid), ignore_errors=True)


def _wide_wanted(row) -> bool | None:
    """eye.clips "auto": whether an event needs its wide clip. True: the PTZ left its Home view, was offline or
    was not main during the event, or its own clip failed (the wide clip may be all there is). False: the PTZ clip
    shows it all. None: not known yet, the PTZ clip is still being cut."""
    if row["clip_wide_keep"] or (row["main_camera"] or "ptz") != "ptz" or row["clip_status"] == "failed":
        return True
    if row["clip_status"] != "ready":
        return None
    return False
