"""Wires cameras -> detector -> trackers -> events/auto-follow, and keeps the live view state.

There is the PTZ camera (RTSP video, VISCA control) and, optionally, the eye: a fixed USB camera beside it at the
same window (README, "Two cameras"). Each camera is a CameraUnit with its own frames, recording ring, tracker and
picture state, run by its own thread. The detector (one inference lane per thread), the events, PTZ control,
auto-follow and the choice of main camera are shared.

The eye counts, because its view never moves, so counting never pauses while the PTZ follows someone. Through the
sync map (data/sync.json, tools/calibrate_sync.py) it also picks targets and aims the PTZ. It becomes the main
camera, the one the live view, photos and default clips come from, while the PTZ picture is washed out or offline
(see failover.py). Without an eye, everything works as a single-camera app.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import threading
import time
from pathlib import Path

import cv2

from .camera import FrameSource
from .detector import HailoDetector
from .draw import draw_overlay, offline_frame
from .events import EventManager, point_in_polygon
from .failover import Health, MainSelector
from .lowlight import AutoLevels, LightMeter, NightView
from .nightclip import NightClipper
from .ptz import EYE_ID_BASE, RIDER, AutoTracker, PTZController
from .recorder import ClipJob, Recorder
from .sync import SyncMap
from .tracker import TRACK_THRESH, Tracker

TRACKS_INTERVAL = 0.2     # SSE `tracks` rate (5 Hz)
DETECTOR_RETRY_S = 30.0
OVERLAY_HOLD_S = 0.5      # keep drawing a box this long after its last detection (avoids flicker)
WHITE_TAU_S = 10.0        # the white share that failover uses is smoothed over about this long
SELECT_EVERY_S = 1.0
STARTUP_GRACE_S = 15.0    # a camera that has not sent its first picture yet is starting, not offline
SYNC_CHECK_S = 10.0       # look for a new data/sync.json this often
UVC_CHECK_S = 30.0        # put a camera's anti-flicker (and the eye's crop) back this often, if it was reset
CAMERAS = ("ptz", "eye")
# Matching one object across the two cameras, in the eye's picture (fractions of its frame), through the sync map:
LINK_RADIUS = 0.04        # a main-camera track this close to one the other camera counted joins that event
HANDOVER_RADIUS = 0.06    # when counting moves to the other camera, a track this close to a counted one is counted
HANDOVER_RECENT_S = 3.0   # counted tracks the old counter saw this long before it stopped working are still in view
COUNTED_MEMORY_S = 30.0   # each camera's counted tracks are remembered this long after it last saw them
BAD_LEAD_S = 10.0         # a whiting-out PTZ may have stopped detecting this long before its picture was flagged


class LiveView:
    """Latest frame plus overlay state. JPEGs are encoded lazily, once per frame per variant."""

    def __init__(self):
        self._lock = threading.Lock()
        self.frame_id = 0
        self._frame = None
        self._ts = 0.0
        self._tracks = []
        self._target = None
        self._zone = []
        self._rect = None
        self._cache = {}

    def update(self, fid, ts, frame, tracks, target_id, zone, rect=None):
        """`rect` (u0, v0, u1, v1): where the PTZ camera is looking, drawn on the eye's picture."""
        with self._lock:
            self.frame_id, self._ts, self._frame = fid, ts, frame
            self._tracks, self._target, self._zone, self._rect = tracks, target_id, zone, rect
            self._cache = {}

    @property
    def online(self) -> bool:
        return self._frame is not None and time.time() - self._ts <= 5.0

    def jpeg(self, overlay=True, width=960, quality=75) -> bytes:
        with self._lock:
            fid, frame, ts = self.frame_id, self._frame, self._ts
            stale = frame is None or time.time() - ts > 5.0
            key = (fid, stale, overlay, width, quality)  # staleness in the key: a frozen frame turns into "offline"
            if key in self._cache:
                return self._cache[key]
            tracks, target, zone, rect = self._tracks, self._target, self._zone, self._rect
        if stale:
            img = offline_frame(width, int(round(width * 9 / 16 / 2) * 2))
        else:
            h, w = frame.shape[:2]
            height = int(round(width * h / w / 2) * 2)
            interp = cv2.INTER_AREA if width < w else cv2.INTER_LINEAR  # the night view is upscaled
            img = cv2.resize(frame, (width, height), interpolation=interp) if width != w else frame.copy()
            if overlay:
                draw_overlay(img, tracks, target, zone, ts)
                if rect is not None:
                    x0, y0 = int(rect[0] * width), int(rect[1] * height)
                    x1, y1 = int(rect[2] * width), int(rect[3] * height)
                    cv2.rectangle(img, (x0, y0), (x1, y1), (255, 255, 255), 2, cv2.LINE_AA)
                    cv2.putText(img, "PTZ", (x0 + 6, max(y0 + 20, 20)), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                                (255, 255, 255), 2, cv2.LINE_AA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            ok, buf = cv2.imencode(".jpg", offline_frame(width, int(round(width * 9 / 16 / 2) * 2)))
        data = buf.tobytes()
        with self._lock:
            if self.frame_id == fid:
                self._cache[key] = data
        return data


class _View:
    """One camera's picture state, kept across pipeline restarts: live view, night vision and brightness."""

    def __init__(self):
        self.live = LiveView()
        self.lightmeter = LightMeter()
        self.night_view = NightView()
        self.levels = AutoLevels()
        self.night = False
        self.white = 0.0  # share burnt white, smoothed (failover)


class CameraUnit:
    def __init__(self, role, name, source, recorder, tracker, view):
        self.role, self.name = role, name
        self.source, self.recorder, self.tracker, self.view = source, recorder, tracker, view
        self.fps = 0.0
        self.thread = None
        self.stop = threading.Event()

    @property
    def online(self) -> bool:
        return bool(self.source.online)

    def health(self) -> Health:
        v = self.view
        return Health(online=self.online, white=v.white, blinded=v.levels.blinded and not v.night)


class _DetectorSlot:
    """The shared detector. One thread per camera uses it at once (each has its own inference lane); it is only
    closed once no thread is inside it, so a native call never runs on a released device."""

    def __init__(self):
        self._cond = threading.Condition()
        self.det = None
        self._users = 0

    @contextlib.contextmanager
    def use(self):
        with self._cond:
            self._users += 1
            det = self.det
        try:
            yield det
        finally:
            with self._cond:
                self._users -= 1
                self._cond.notify_all()

    def set(self, det):
        with self._cond:
            self.det = det

    def retire(self, timeout=10.0):
        """Take the detector out of use and return it once nobody is using it (None if a call is stuck)."""
        with self._cond:
            old, self.det = self.det, None
            if not self._cond.wait_for(lambda: self._users == 0, timeout):
                return None
        return old


class Pipeline:
    def __init__(self, cfg_store, storage, bus, media_dir, log=None):
        self.cfg = cfg_store
        self.storage = storage
        self.bus = bus
        self.media_dir = Path(media_dir)
        self.log = log or logging.getLogger("ptzcam.pipeline")
        self.ptz = PTZController(cfg_store)
        self.autotracker = AutoTracker(self.ptz, cfg_store)
        self.selector = MainSelector()
        self.night_clipper = NightClipper(cfg_store)  # outlives pipeline restarts, like the PTZ worker
        self._views = {c: _View() for c in CAMERAS}
        self.units: dict[str, CameraUnit] = {}
        self.events = None
        self._slot = _DetectorSlot()
        self.detector_error = None
        self._detector_tried = 0.0
        self._det_lock = threading.Lock()
        self._house_lock = threading.Lock()
        self._night_broken = False  # night vision failed: carry on without it until the detector is rebuilt
        self._next_ids = {"ptz": 1, "eye": EYE_ID_BASE}
        self.counting = "ptz"
        self.sync = None
        self._sync_mtime = None
        self._sync_t = self._uvc_t = self._select_t = 0.0
        self._eye_zone_key = None
        self._eye_zone = []
        self._uvc_refused = set()
        self._sync_pending = None  # tools/sync_when_clear.py's progress, for the status
        self._counted = {c: {} for c in CAMERAS}  # role -> track id -> _Seen: what each camera counted, lately
        self._last_det = {}  # role -> when it last detected anything (a whiting-out picture stops detecting first)
        self._counted_lock = threading.Lock()
        self._started_at = 0.0
        self._lock = threading.Lock()
        self._first_start = True

    # ---- compatibility: the live view, detector and sources that web.py and status() read ----
    @property
    def live(self) -> LiveView:
        return self.live_for("main")

    def live_for(self, camera: str = "main") -> LiveView:
        if camera == "main":
            camera = self.selector.main if self.selector.main in self.units else "ptz"
        return self._views[camera if camera in self._views else "ptz"].live

    @property
    def detector(self):
        return self._slot.det

    # ---- lifecycle ----
    def start(self):
        with self._lock:
            if not self.ptz._thread.is_alive():
                self.ptz.start()
                self.night_clipper.start()
            self._start()

    def restart(self):
        with self._lock:
            self.log.info("restarting pipeline")
            self._stop_all()
            self._start()

    def shutdown(self):
        with self._lock:
            self._stop_all()
        self.night_clipper.stop()
        self.ptz.close()

    def _start(self):
        cfg = self.cfg.data
        d, s = cfg["detect"], cfg["storage"]
        url = cfg["camera"]["rtsp_url"]
        aspect = d["width"] / d["height"]

        def tracker(role):
            return Tracker(aspect=aspect, min_hits=d["min_hits"], min_move=d["min_move"], first_id=self._next_ids[role])

        ptz_rec = Recorder(url, s["segment_dir"], s["segment_seconds"], s["buffer_seconds"])
        units = {"ptz": CameraUnit("ptz", cfg["camera"]["name"], FrameSource(url, d["width"], d["height"], d["fps"],
                                   name="ptz"), ptz_rec, tracker("ptz"), self._views["ptz"])}
        eye = cfg["eye"]
        if eye["enabled"]:
            from .usbcam import UsbCapture  # only needed with an eye camera
            cap = UsbCapture(eye["device"], eye["width"], eye["height"], eye["fps"], d["width"], d["height"],
                             Path(s["segment_dir"]) / "eye", s["segment_seconds"], s["buffer_seconds"],
                             anti_flicker=eye["anti_flicker"], name="eye")
            units["eye"] = CameraUnit("eye", eye["name"], cap, cap.ring, tracker("eye"), self._views["eye"])
        self.units = units
        self._started_at = time.time()
        self.autotracker.reset_target()  # old target ids mean nothing to the new trackers
        self.events = EventManager(self.cfg, self.storage, ptz_rec, self.media_dir, self.bus,
                                   night_clipper=self.night_clipper,
                                   wide_recorder=units["eye"].recorder if "eye" in units else None)
        self.events.main = self.selector.main if self.selector.main in units else "ptz"
        self._init_detector()
        recover_ptz, recover_eye = [], []
        if self._first_start:  # clips a previous run left pending can still be cut from the RAM buffers
            self._first_start = False
            self.storage.fail_pending_night_clips()
            for row in self.storage.pending_clips():
                if not row["clip"]:
                    self.storage.update_event(row["id"], clip_status="failed")
                    continue
                recover_ptz.append(ClipJob(row["id"], row["start_ts"], row["end_ts"], self.media_dir / row["clip"],
                                           self.events._clip_done))
            for row in self.storage.pending_wide_clips():
                if "eye" not in units or not row["clip_wide"]:
                    self.storage.update_event(row["id"], clip_wide_status="failed")
                    continue
                recover_eye.append(ClipJob(row["id"], row["start_ts"], row["end_ts"],
                                           self.media_dir / row["clip_wide"], self.events._wide_clip_done))
        ptz_rec.start(recover_ptz)
        units["ptz"].source.start()
        if "eye" in units:
            units["eye"].source.start(recover_eye)  # its ring first, then the capture
        try:
            self._load_sync(force=True)
        except Exception:  # a broken sync.json must never keep the cameras from starting
            self.log.exception("could not load the sync map; carrying on without it")
            self.sync = self.autotracker.sync = None
        for unit in units.values():
            unit.stop = threading.Event()  # one Event per loop, so a late old loop can never resume
            unit.thread = threading.Thread(target=self._loop, args=(unit, unit.stop), name=f"pipeline-{unit.role}",
                                           daemon=True)
            unit.thread.start()

    def _stop_all(self):
        for unit in self.units.values():
            unit.stop.set()
        stuck = False
        for unit in self.units.values():
            if unit.thread is not None:
                unit.thread.join(10)
                stuck = stuck or unit.thread.is_alive()
            self._next_ids[unit.role] = unit.tracker._next_id
        if stuck:  # stuck in a native call: never release the detector under it
            self.log.error("a pipeline loop did not stop in time; abandoning its detector")
            self._slot.set(None)
        if self.events is not None:
            self.events.close()
        for unit in self.units.values():
            unit.source.stop()  # the eye's capture also stops its ring, cutting pending clips
            if unit.role == "ptz":
                unit.recorder.stop()  # cuts pending clips from what is buffered
        self._close_detector()

    def _init_detector(self):
        with self._det_lock:
            if self._slot.det is not None:
                return
            self._detector_tried = time.time()
            self._night_broken = False
            try:
                model = self.cfg.data["lowlight"]["model"]
                self._slot.set(HailoDetector(self.cfg.resolve(self.cfg.data["detect"]["model"]),
                                             enhancer_hef=self.cfg.resolve(model) if model else None))
                self.detector_error = None
            except Exception as e:
                self.detector_error = str(e) or e.__class__.__name__
                self.log.error("Hailo detector unavailable: %s", self.detector_error)

    def _close_detector(self):
        old = self._slot.retire()
        if old is not None:
            try:
                old.close()
            except Exception:
                self.log.exception("closing detector failed")

    # ---- main loop, one per camera ----
    def _loop(self, unit: CameraUnit, stop: threading.Event):
        role, source, tracker, view = unit.role, unit.source, unit.tracker, unit.view
        events = self.events
        last_id, was_frozen, last_pub, last_t = 0, False, 0.0, None
        failures = 0
        while not stop.is_set():
            item = source.wait_frame(last_id, timeout=1.0)
            if item is None:
                try:
                    self._housekeeping()  # keep failover deciding while a camera is offline
                except Exception:  # never let it end this camera's thread: nothing would restart it
                    self.log.exception("housekeeping failed (%s camera)", role)
                    time.sleep(0.2)
                continue
            fid, ts, frame = item
            last_id = fid
            try:
                cfg = self.cfg.data
                if self._slot.det is None and time.time() - self._detector_tried > DETECTOR_RETRY_S:
                    self._init_detector()
                main = role == self.selector.main or len(self.units) == 1
                ll = cfg["lowlight"]
                with self._slot.use() as det:
                    # Night vision: Zero-DCE on the Hailo, for the main camera only (one night stream, one night
                    # clip ring). Its view feeds the live stream, snapshots and night clips, and maybe the detector.
                    enhanced = shown = None
                    is_night = view.lightmeter.update(frame, ts, ll)
                    if is_night and main and det is not None and det.enhancer is not None and not self._night_broken:
                        try:
                            enhanced = det.enhancer.enhance(frame)
                            shown = view.night_view.process(enhanced)  # 712x400: the model's real resolution
                        except Exception:  # never let night vision take detection down with it
                            self.log.exception("night vision failed; continuing without it")
                            self._night_broken = True
                            enhanced = shown = None
                    # By day: auto-brightness stretches a washed-out picture (sun on the window) back to full range.
                    corrected = None
                    if not is_night:
                        corrected = view.levels.process(frame, correct=cfg["brightness"]["auto"])
                    else:
                        view.levels.reset()
                    src = corrected if corrected is not None and cfg["brightness"]["detect"] else frame
                    dets = det.detect(src, enhanced if ll["detect"] else None) if det is not None else []
                if dets:
                    self._last_det[role] = ts
                dt = min(max(ts - last_t, 0.0), 1.0) if last_t is not None else 0.0
                view.white += (view.levels.white - view.white) * min(1.0, dt / WHITE_TAU_S)
                if shown is not None:
                    if ll["save_clips"]:
                        self.night_clipper.push(ts, shown)
                else:
                    if view.night:
                        view.night_view.reset()  # its temporal filter must not blend a stale scene in later
                    shown = corrected if corrected is not None else frame
                view.night = enhanced is not None
                if main:
                    events.night = view.night
                failures = 0

                # Camera motion must never look like object motion: freeze the PTZ tracker's motion tests while
                # it moves and settles, then restart every track's motion measurement. The eye never moves.
                frozen = False
                if role == "ptz":
                    frozen = self.ptz.is_moving() or ts - self.ptz.quiet_since() < cfg["ptz"]["settle_seconds"]
                    if was_frozen and not frozen:
                        tracker.reanchor(ts)
                    was_frozen = frozen
                tracker.motion_frozen = frozen
                tracker.min_hits = cfg["detect"]["min_hits"]
                tracker.min_move = cfg["detect"]["min_move"]
                thresh = {c: min(TRACK_THRESH, v["min_score"]) for c, v in cfg["detect"]["categories"].items()}
                tracks, removed = tracker.update(dets, ts, thresh)

                # One camera counts. The PTZ zone is drawn on the Home view, so it only counts there (with PTZ
                # control off the app never moves the camera, so the view is assumed to be the configured one).
                if role == "ptz":
                    zone = cfg["detect"]["zone"]
                    counting = self.counting == "ptz" and not frozen and (
                        not zone or not cfg["ptz"]["enabled"] or self.ptz.at_home)
                else:
                    counting = self.counting == "eye"
                    # Where something can be counted, in the eye's picture: its own zone when it counts, else the
                    # PTZ's Home view and zone mapped onto it. The eye never moves, so entries are marked whether
                    # or not it counts now (eye-follow picks only what can become an event).
                    zone = self._zone_for_eye(cfg) if counting else self._ptz_zone_in_eye(cfg)
                    if zone:
                        for t in tracks:
                            if t.confirmed and t.last_ts == ts and not t.in_zone and \
                                    point_in_polygon((t.box[0] + t.box[2]) / 2, t.box[3], zone):
                                t.in_zone = True
                    self.autotracker.eye_zone = zone
                    self.autotracker.eye_counted_only = self.counting != "eye"
                usable = not view.levels.blinded or view.night  # it can see (aim corrections use this)
                photo_ok = usable
                if role == "ptz" and "eye" in self.units and self.selector.main == "eye" \
                        and self.selector.ptz_unfit:
                    photo_ok = False  # the wide camera took over from this picture: no photos from it until clear
                if role != self.counting and self.counting in self.units and (main or role == "eye"):
                    # The main camera's pictures become photos; and while the PTZ counts, the eye knows whom it
                    # counted (eye-follow only leaves Home for them).
                    self._link_to_counter(role, tracks, ts, cfg)
                events.process(ts, shown, tracks, removed, counting, zone=zone, usable=photo_ok, camera=role)
                self._remember_counted(role, tracks, ts)
                if role == "ptz":
                    self.autotracker.ptz_usable = usable
                    self.autotracker.update(tracks, ts)
                else:
                    self.autotracker.eye_active = source.online and not (view.levels.blinded and not view.night)
                    self.autotracker.update_eye(tracks, ts)

                target = self._target_for(role)
                visible = [t for t in tracks if t.confirmed and ts - t.last_ts < OVERLAY_HOLD_S]
                view.live.update(fid, ts, shown, [t.overlay_info() for t in visible], target, zone,
                                 rect=self._ptz_rect(cfg) if role == "eye" else None)
                if ts - last_pub >= TRACKS_INTERVAL:  # tagged by camera: the page shows the camera it is displaying
                    self.bus.publish("tracks", {"ts": ts, "camera": role, "main": main,
                                                "tracks": [t.to_json(target) for t in visible]})
                    last_pub = ts
                if last_t is not None:
                    inst = 1.0 / max(1e-3, ts - last_t)
                    unit.fps = inst if unit.fps == 0 else 0.9 * unit.fps + 0.1 * inst
                last_t = ts
                self._housekeeping()
            except Exception:
                failures += 1
                self.log.exception("pipeline step failed (%s camera)", role)
                if failures >= 10 and self._slot.det is not None:
                    self.log.error("detector keeps failing; reinitialising in %.0f s", DETECTOR_RETRY_S)
                    self._close_detector()
                    self.detector_error = "inference failed repeatedly"
                    failures = 0
                time.sleep(0.2)

    def _target_for(self, role):
        at = self.autotracker
        if not at.enabled:
            return None
        if at.mode == "eye":
            return at.target_id if role == "eye" else at.ptz_match_id
        return at.target_id if role == "ptz" else None

    # ---- shared decisions, at most once a second from whichever loop gets there ----
    def _housekeeping(self):
        now = time.time()
        if now - self._select_t < SELECT_EVERY_S:
            return
        with self._house_lock:
            if now - self._select_t < SELECT_EVERY_S:
                return
            self._select_t = now
        cfg = self.cfg.data
        units = self.units
        eye = units.get("eye")
        if eye is None or not eye.online:
            self.autotracker.eye_active = False  # only the eye's own frames would clear it otherwise
        # Right after a start the cameras take a moment to send their first picture: that is not an outage (it would
        # hand the view, and counting, to whichever camera was quicker, for return_after).
        starting = now - self._started_at < STARTUP_GRACE_S and not all(u.online for u in units.values())
        if not starting and self.selector.tick(now, cfg["failover"], units["ptz"].health(),
                                               eye.health() if eye else None):
            self.log.info("main camera: %s (%s)", self.selector.main, self.selector.reason)
            self.bus.publish("main", {"camera": self.selector.main, "reason": self.selector.reason,
                                      "since": self.selector.since})
        if self.events is not None:
            self.events.main = self.selector.main if self.selector.main in units else "ptz"
            # Another camera counts and, without the sync map, its tracks cannot be matched to the main camera's:
            # the main camera's own picture of the moment is the photo.
            self.events.photo_from_main = eye is not None and self.counting != self.events.main and self.sync is None
        self._update_counting(cfg)
        self._mark_wide_needed(cfg)
        if now - self._sync_t >= SYNC_CHECK_S:
            self._sync_t = now
            self._load_sync()
            self._read_sync_pending()
        self._eye_controls(cfg, check=now - self._uvc_t >= UVC_CHECK_S)
        if now - self._uvc_t >= UVC_CHECK_S:
            self._uvc_t = now
            self._keep_anti_flicker(cfg)

    def _mark_wide_needed(self, cfg):
        """eye.clips "auto" keeps an event's wide clip when the PTZ clip does not show the whole scene: the eye was
        main, the PTZ was offline, or it left its Home view (moving or following) during the event. Decided here,
        whichever camera sends frames, so an offline PTZ still counts."""
        events, ptz_unit = self.events, self.units.get("ptz")
        a = events.active if events is not None else None
        if a is None or "eye" not in self.units or ptz_unit is None:
            return
        ptz = self.ptz
        away = cfg["ptz"]["enabled"] and (not ptz.at_home or ptz.quiet_since() >= a.start)
        if self.selector.main != "ptz" or not ptz_unit.online or away:
            events.wide_needed()

    def _update_counting(self, cfg):
        """The eye counts when it can (its view never moves) and knows where: its own zone, or the PTZ's Home view
        mapped through the sync map. Otherwise the PTZ counts on its Home view, as with one camera. While the PTZ
        is washed out or offline the eye counts its whole view rather than nobody counting."""
        eye = self.units.get("eye")
        eye_ok = eye is not None and eye.online and not eye.health().blinded
        if eye_ok and cfg["eye"]["count"] and self._zone_for_eye(cfg):
            want = "eye"
        elif eye_ok and cfg["eye"]["count"] and self.selector.main == "eye":
            want = "eye"
        else:
            want = "ptz"
        if want == self.counting:
            return
        n = self._hand_over(self.counting, want, cfg)
        self.log.info("counting on the %s camera%s", "wide" if want == "eye" else "PTZ",
                      f" ({n} already counted in view)" if n else "")
        self.counting = want

    def _hand_over(self, old: str, new: str, cfg) -> int:
        """Counting moves from camera `old` to `new`. What the new counter sees now that the old one already counted
        must not count again, but everything else must still count (a washed-out PTZ counted nobody for a while).
        Through the sync map, with the PTZ still, tracks are matched by position; otherwise, per kind of object, as
        many as the old counter has counted in view (the longest-seen first). Returns how many were matched."""
        nu, ou = self.units.get(new), self.units.get(old)
        if nu is None or ou is None:
            return 0
        now = time.time()
        # Recent as of when the old counter last worked: its last picture if it went offline (seen as offline only
        # 5 s later), or when the PTZ picture went bad if it washed out (its tracks fade out with the detections).
        ref = now
        age = ou.source.last_frame_age
        if not ou.online and age is not None:
            ref = now - age
        if old == "ptz" and self.selector.bad_since is not None:
            # Its detections fade out before the white/blinded flags trip: from when it last detected anything, but
            # no further back than BAD_LEAD_S before the flags.
            last = self._last_det.get(old, ref)
            ref = min(ref, self.selector.bad_since, max(last, self.selector.bad_since - BAD_LEAD_S))
        with self._counted_lock:
            done = [s for s in self._counted[old].values() if s.last_ts >= ref - HANDOVER_RECENT_S]
        fresh = [t for t in list(nu.tracker.tracks) if t.confirmed and not t.counted and now - t.last_ts < 1.0]
        if not fresh or not done:
            return 0
        # Positions that old say little about where they are now: then match by number.
        view = self._ptz_in_eye(cfg) if now - ref <= HANDOVER_RECENT_S else None
        pairs = []
        if view is not None:
            cand = []
            for n in fresh:
                for o in done:
                    p, e = (o, n) if old == "ptz" else (n, o)
                    d = self._eye_dist(view, p, e)
                    if d is not None and d < HANDOVER_RADIUS:
                        cand.append((d, n, o))
            used = set()
            for _, n, o in sorted(cand, key=lambda c: c[0]):
                if id(n) not in used and id(o) not in used:
                    used.update((id(n), id(o)))
                    pairs.append((n, o))
        else:  # only among what the new counter could count (a parked car must not take a passer-by's count)
            left = {}
            for o in sorted(done, key=lambda t: t.first_ts):
                left.setdefault(o.category, []).append(o)
            for n in sorted((t for t in fresh if self._could_count(t, new, cfg)), key=lambda t: t.first_ts):
                if left.get(n.category):
                    pairs.append((n, left[n.category].pop(0)))
        for n, o in pairs:
            n.counted, n.event_id = True, o.event_id
        return len(pairs)

    def _remember_counted(self, role, tracks, ts):
        """Keep what this camera counted for a while after its tracker lets go (a hand-over comes seconds later)."""
        with self._counted_lock:
            mem = self._counted[role]
            for t in tracks:
                if t.counted and t.last_ts == ts:
                    mem[t.id] = _Seen(t)
            for k in [k for k, s in mem.items() if ts - s.last_ts > COUNTED_MEMORY_S]:
                del mem[k]

    def _could_count(self, t, role, cfg) -> bool:
        """Whether camera `role` could count track `t`: moving, of an enabled kind, and inside its zone."""
        c = cfg["detect"]["categories"].get(t.category)
        if not c or not c["enabled"] or t.max_score < c["min_score"] or not t.moving:
            return False
        zone = self._zone_for_eye(cfg) if role == "eye" else cfg["detect"]["zone"]
        return not zone or t.in_zone or point_in_polygon((t.box[0] + t.box[2]) / 2, t.box[3], zone)

    def _link_to_counter(self, role, tracks, ts, cfg):
        """Photos come from the main camera. While the other camera counts, the main camera's tracks of objects it
        counted into the open event join that event, matched by position through the sync map, so their pictures can
        become the photo. They are taken as counted: never counted again by this camera."""
        events, other = self.events, self.units.get(self.counting)
        a = events.active if events is not None else None
        if a is None or other is None or other.role == role:
            return
        theirs = [t for t in list(other.tracker.tracks) if t.event_id == a.id and ts - t.last_ts < 1.0]
        if not theirs:
            return
        view = self._ptz_in_eye(cfg)
        if view is None:
            return
        for p in tracks:
            if not p.confirmed or p.last_ts != ts or p.event_id is not None:
                continue
            best = None
            for o in theirs:
                d = self._eye_dist(view, p, o) if role == "ptz" else self._eye_dist(view, o, p)
                if d is not None and d < LINK_RADIUS and (best is None or d < best):
                    best = d
            if best is not None:
                p.event_id, p.counted = a.id, True

    def _eye_dist(self, view, p, e):
        """Distance in the eye's picture between PTZ track `p` and eye track `e`, at the moment p's picture was
        taken (the PTZ picture lags more than the eye's), or None if they cannot be the same object."""
        if not (p.category == e.category or (p.category in RIDER and e.category in RIDER)):
            return None
        cu, cv, fw, fh = view
        ph = float(p.box[3] - p.box[1]) * fh
        eh = float(e.box[3] - e.box[1])
        if not 0.5 <= ph / max(eh, 1e-3) <= 2.0:
            return None
        at = self.autotracker
        dt = (p.last_ts - at.PTZ_LAG_S) - (e.last_ts - at.EYE_LAT_S)
        eu, ev = e.center[0] + float(e.vel[0]) * dt, e.center[1] + float(e.vel[1]) * dt
        pu, pv = cu + (p.center[0] - 0.5) * fw, cv + (p.center[1] - 0.5) * fh
        return float(((pu - eu) ** 2 + (pv - ev) ** 2) ** 0.5)

    def _ptz_in_eye(self, cfg):
        """Where the PTZ's picture is in the eye's, (u, v, width, height), while the PTZ is still; else None."""
        sync, ptz = self.sync, self.ptz
        if sync is None:
            return None
        if cfg["ptz"]["enabled"]:
            if ptz.is_moving() or time.time() - ptz.quiet_since() < cfg["ptz"]["settle_seconds"]:
                return None
            pose, zp = ptz.pose_estimate(), ptz.zoom_pos
            if pose is None or zp is None:
                return None
        else:  # the app never moves it: it shows its Home view
            home = self._home_pose()
            if home is None:
                return None
            pose, zp = home[:2], home[2]
        cu, cv = sync.to_eye(*pose)
        fld = sync.field(cu, cv, zp, cfg["ptz"]["zoom_curve"])
        return (cu, cv, fld[0], fld[1]) if fld else None

    def _home_pose(self):
        """The PTZ's Home view (pan, tilt, zoom): as read at Home, else as the sync calibration found it."""
        ptz = self.ptz
        pan, tilt, zoom = ptz.home_pan, ptz.home_tilt, ptz.home_zoom
        if pan is not None and tilt is not None and zoom is not None:
            return pan, tilt, zoom
        home = self.sync.meta.get("ptz_home") if self.sync is not None else None
        if isinstance(home, (list, tuple)) and len(home) == 3 and \
                all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in home):
            return tuple(home)
        return None

    def _zone_for_eye(self, cfg):
        """The eye's counting zone: its own, or else the PTZ's zone (or its whole Home view) mapped into the eye's
        picture through the sync map, so the counted area matches the counts made before. [] if neither is known."""
        return cfg["eye"]["zone"] or self._ptz_zone_in_eye(cfg)

    def _ptz_zone_in_eye(self, cfg):
        """The PTZ's counting zone, or its whole Home view, in the eye's picture; [] without a sync map or a Home."""
        sync, home = self.sync, self._home_pose()
        if sync is None or home is None:
            return []
        curve = cfg["ptz"]["zoom_curve"]
        key = (id(sync), home, tuple(map(tuple, cfg["detect"]["zone"])), tuple(map(tuple, curve)))
        if key != self._eye_zone_key:
            u, v = sync.to_eye(home[0], home[1])
            fld = sync.field(u, v, home[2], curve)
            pts = cfg["detect"]["zone"] or [[0, 0], [1, 0], [1, 1], [0, 1]]
            zone = [] if fld is None else [
                [min(max(u + (x - 0.5) * fld[0], 0.0), 1.0), min(max(v + (y - 0.5) * fld[1], 0.0), 1.0)] for x, y in pts]
            self._eye_zone, self._eye_zone_key = zone, key
        return self._eye_zone

    def _ptz_rect(self, cfg):
        sync, ptz = self.sync, self.ptz
        if sync is None or "eye" not in self.units:
            return None
        pose, zp = ptz.pose_estimate(), ptz.zoom_pos
        if pose is None or zp is None:
            return None
        return sync.ptz_view(pose[0], pose[1], zp, cfg["ptz"]["zoom_curve"])

    def _load_sync(self, force=False):
        """data/sync.json, measured by tools/calibrate_sync.py for this eye camera."""
        path = self.cfg.resolve(self.cfg.data["storage"]["data_dir"]) / "sync.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        if not force and mtime == self._sync_mtime:
            return
        self._sync_mtime = mtime
        m = None
        if mtime is not None and "eye" in self.units:
            try:
                m = SyncMap.load(path)
                want = self.cfg.data["eye"]["device"].lower()
                if m is not None and want not in str(m.meta.get("eye_device", want)).lower():
                    self.log.warning("sync.json was made for another eye camera (%s); ignoring it", m.meta.get("eye_device"))
                    m = None
            except Exception as e:
                self.log.error("cannot read %s: %s", path, e)
                m = None
        if "eye" in self.units:
            try:
                self._set_eye_crop(m)
            except Exception:  # only the crop is lost, not the map
                self.log.exception("could not set the eye camera's crop from sync.json")
        if (m is None) != (self.sync is None) or force:
            self.log.info("sync map %s", f"loaded ({len(m.samples)} positions)" if m is not None else "not available")
        self.sync = self.autotracker.sync = m
        self._eye_zone_key = None

    def _read_sync_pending(self):
        """tools/sync_when_clear.py, waiting for a clear moment to sync the cameras, writes its progress to
        data/sync_pending.json (and removes it once synced). Shown while it runs, and a failure for a day."""
        path = self.cfg.resolve(self.cfg.data["storage"]["data_dir"]) / "sync_pending.json"
        d = None
        try:
            d = json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        if not isinstance(d, dict) or d.get("state") not in ("waiting", "syncing", "failed"):
            self._sync_pending = None
            return
        age = time.time() - float(d.get("updated") or 0)
        live = _pid_alive(d.get("pid")) and age < 600
        if d["state"] == "failed" and age > 86400 or d["state"] != "failed" and not live:
            self._sync_pending = None  # gone (stopped, rebooted) or long ago
            return
        self._sync_pending = {k: d.get(k) for k in ("state", "since", "clear_for", "need", "error", "updated")}

    def _counting_note(self, cfg) -> str:
        """Why the wide camera does not count now ("" when it does, or when there is none)."""
        eye = self.units.get("eye")
        if eye is None or self.counting == "eye":
            return ""
        if not cfg["eye"]["count"]:
            return "counting on the wide camera is off"
        if not eye.online:
            return "the wide camera is offline"
        if eye.health().blinded:
            return "the wide camera is blinded"
        if not cfg["eye"]["zone"] and self.sync is None:
            return "the wide camera needs the camera sync, or a zone drawn on its picture"
        if not self._zone_for_eye(cfg):
            return "waiting to read where the PTZ's Home view is"
        return ""

    def _set_eye_crop(self, m):
        """The map holds only for the eye's crop (digital pan/tilt/zoom) at calibration. The capture puts it back on
        every connect (a re-plug resets it) and _eye_controls checks it, so the map always fits the picture."""
        eptz = (m.meta.get("eye_eptz") if m is not None else None) or {}
        if not isinstance(eptz, dict):
            self.log.warning("sync.json has an unreadable eye crop (%r); ignoring it", eptz)
            eptz = {}
        crop = {}
        if eptz:
            for name, value in eptz.items():
                if isinstance(name, str) and isinstance(value, (int, float)) and not isinstance(value, bool) \
                        and math.isfinite(value):
                    crop[name] = int(value)
        if eptz and len(crop) != len(eptz):
            self.log.warning("sync.json has an unreadable eye crop (%r); using what can be read", eptz)
        src = self.units["eye"].source
        src.crop = crop
        if crop:
            self._apply_uvc("eye camera", self.cfg.data["eye"]["device"], crop)

    def _eye_controls(self, cfg, check: bool):
        """The eye's anti-flicker follows the settings at once (no restart needed); with `check`, it and the crop are
        also read back and put back if the camera reset them."""
        eye = self.units.get("eye")
        if eye is None:
            return
        src, want = eye.source, cfg["eye"]["anti_flicker"]
        changed = src.anti_flicker != want
        src.anti_flicker = want  # also what the capture sets on its next connect
        if not (changed or check):
            return
        values = dict(getattr(src, "crop", None) or {})
        if want is not None and want >= 0:
            values["power_line_frequency"] = want
        if values:  # by name: after a re-plug its old node number may belong to the other camera
            self._apply_uvc("eye camera", cfg["eye"]["device"], values)

    def _apply_uvc(self, what, device, values: dict):
        from . import uvc
        try:
            dev = uvc.find_device(device)
            for name, value in values.items():
                try:
                    if uvc.read_control(dev, name) != value:
                        old, new = uvc.write_control(dev, name, value)
                        self.log.info("%s %s put back to %s (was %s)", what, name, new, old)
                    self._uvc_refused.discard((what, name, value))
                except (OSError, LookupError, KeyError, ValueError) as e:
                    if (what, name, value) not in self._uvc_refused:  # once, not every check
                        self._uvc_refused.add((what, name, value))
                        self.log.warning("%s %s not set to %s: %s", what, name, value, e)
        except Exception as e:  # unplugged, or anything else: try again next time
            self.log.debug("%s controls not checked: %s", what, e)

    def _keep_anti_flicker(self, cfg):
        """A camera restart resets its anti-flicker; put back the configured one on the PTZ camera's USB link
        (the eye's is handled by _eye_controls and by its capture on every connect)."""
        name, want = cfg["camera"]["usb_device"], cfg["camera"]["anti_flicker"]
        if not name or want < 0:
            return
        self._apply_uvc(name, name, {"power_line_frequency": want})

    # ---- status ----
    def _name(self, role: str) -> str:
        cfg = self.cfg.data
        return cfg["eye"]["name"] if role == "eye" else cfg["camera"]["name"]  # live: a rename shows at once

    def _camera_status(self, unit: CameraUnit) -> dict:
        v, src, rec = unit.view, unit.source, unit.recorder
        return {
            "name": self._name(unit.role), "online": unit.online, "fps": round(src.fps, 1),
            "last_frame_age": src.last_frame_age, "white": round(v.white * 100, 1),
            "blinded": v.levels.blinded and not v.night, "night": v.night,
            "luma": round(v.lightmeter.luma, 1) if v.lightmeter.luma is not None else None,
            "recorder": {"online": bool(rec and rec.online),
                         "buffer_seconds": round(rec.buffer_seconds(), 1) if rec else 0.0},
        }

    def status(self) -> dict:
        cfg = self.cfg.data
        units, at, det = self.units, self.autotracker, self._slot.det
        main = self.selector.main if self.selector.main in units else "ptz"
        mu = units.get(main) or units.get("ptz")
        ptz_unit = units.get("ptz")
        view = self._views[main]
        rec = ptz_unit.recorder if ptz_unit else None
        fps = max((u.fps for u in units.values()), default=0.0)
        sync = self.sync
        return {
            "camera": {  # what the live view shows: the main camera
                "name": self._name(mu.role) if mu else cfg["camera"]["name"], "online": bool(mu and mu.online),
                "fps": round(mu.source.fps, 1) if mu else 0.0,
                "width": cfg["detect"]["width"], "height": cfg["detect"]["height"],
                "last_frame_age": mu.source.last_frame_age if mu else None,
            },
            "cameras": {role: self._camera_status(u) for role, u in units.items()},
            "main": {"camera": main, "reason": self.selector.reason, "since": self.selector.since,
                     "mode": cfg["failover"]["mode"]},
            "counting": self.counting,
            "counting_note": self._counting_note(cfg),
            "counting_area": ("wide zone" if cfg["eye"]["zone"] else "PTZ Home view" if self._ptz_zone_in_eye(cfg)
                              else "whole wide view") if self.counting == "eye" else "PTZ Home view",
            "sync": {"calibrated": sync is not None, "created": sync.created if sync else None,
                     "scheduled": self._sync_pending,
                     "positions": len(sync.samples) if sync else 0,
                     "residuals": (sync.meta.get("residuals") or {}) if sync else {}},
            "detector": {
                "online": det is not None, "model": Path(cfg["detect"]["model"]).stem,
                "device": det.device if det else None, "fps": round(fps, 1) if det else 0.0,
                "infer_ms": round(det.infer_ms, 1) if det else None, "error": self.detector_error,
            },
            "recorder": {  # the PTZ camera's ring (each camera's is under cameras.*.recorder)
                "online": bool(rec and rec.online),
                "buffer_seconds": round(rec.buffer_seconds(), 1) if rec else 0.0,
            },
            "ptz": {
                "enabled": cfg["ptz"]["enabled"], "connected": self.ptz.connected, "moving": self.ptz.is_moving(),
                "autotrack": at.enabled, "autozoom": cfg["ptz"]["autozoom"],
                "target_id": at.target_id if at.enabled else None, "zoom": self.ptz.zoom_pos,
                "home_preset": cfg["ptz"]["home_preset"], "autofocus": cfg["ptz"]["autofocus"],
                "paused_for": round(at.paused_for(), 1), "mode": at.mode,
                "match_id": at.ptz_match_id if at.mode == "eye" else None,
            },
            "brightness": {  # the main camera's picture
                "auto": cfg["brightness"]["auto"], "detect": cfg["brightness"]["detect"],
                "gain": round(view.levels.gain, 2), "white": round(view.levels.white * 100, 1),
                "blinded": view.levels.blinded and not view.night, "camera": main,
            },
            "lowlight": {
                "mode": cfg["lowlight"]["mode"], "active": view.night,
                "luma": round(view.lightmeter.luma, 1) if view.lightmeter.luma is not None else None,
                "model": det.enhancer.name if det and det.enhancer else None,
                "enhance_ms": round(det.enhancer.ms, 1) if det and det.enhancer else None,
                "detect": cfg["lowlight"]["detect"], "save_clips": cfg["lowlight"]["save_clips"],
            },
        }


def _pid_alive(pid) -> bool:
    """Whether process `pid` exists (it may belong to another user, such as root)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class _Seen:
    """What a camera's counted track looked like when last seen (for a later hand-over)."""
    __slots__ = ("id", "category", "box", "center", "vel", "last_ts", "first_ts", "event_id")

    def __init__(self, t):
        self.id, self.category, self.box, self.center = t.id, t.category, t.box.copy(), t.center
        self.vel, self.last_ts, self.first_ts, self.event_id = t.vel.copy(), t.last_ts, t.first_ts, t.event_id
