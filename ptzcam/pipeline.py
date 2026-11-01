"""Wires camera -> detector -> tracker -> events/auto-follow, and keeps the live view state."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import cv2

from .camera import FrameSource
from .detector import HailoDetector
from .draw import draw_overlay, offline_frame
from .events import EventManager
from .lowlight import AutoLevels, LightMeter, NightView
from .nightclip import NightClipper
from .ptz import AutoTracker, PTZController
from .recorder import ClipJob, Recorder
from .tracker import TRACK_THRESH, Tracker

TRACKS_INTERVAL = 0.2     # SSE `tracks` rate (5 Hz)
DETECTOR_RETRY_S = 30.0
OVERLAY_HOLD_S = 0.5      # keep drawing a box this long after its last detection (avoids flicker)


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
        self._cache = {}

    def update(self, fid, ts, frame, tracks, target_id, zone):
        with self._lock:
            self.frame_id, self._ts, self._frame = fid, ts, frame
            self._tracks, self._target, self._zone = tracks, target_id, zone
            self._cache = {}

    def jpeg(self, overlay=True, width=960, quality=75) -> bytes:
        with self._lock:
            fid, frame, ts = self.frame_id, self._frame, self._ts
            stale = frame is None or time.time() - ts > 5.0
            key = (fid, stale, overlay, width, quality)  # staleness in the key: a frozen frame turns into "offline"
            if key in self._cache:
                return self._cache[key]
            tracks, target, zone = self._tracks, self._target, self._zone
        if stale:
            img = offline_frame(width, int(round(width * 9 / 16 / 2) * 2))
        else:
            h, w = frame.shape[:2]
            height = int(round(width * h / w / 2) * 2)
            interp = cv2.INTER_AREA if width < w else cv2.INTER_LINEAR  # the night view is upscaled
            img = cv2.resize(frame, (width, height), interpolation=interp) if width != w else frame.copy()
            if overlay:
                draw_overlay(img, tracks, target, zone, ts)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            ok, buf = cv2.imencode(".jpg", offline_frame(width, int(round(width * 9 / 16 / 2) * 2)))
        data = buf.tobytes()
        with self._lock:
            if self.frame_id == fid:
                self._cache[key] = data
        return data


class Pipeline:
    def __init__(self, cfg_store, storage, bus, media_dir, log=None):
        self.cfg = cfg_store
        self.storage = storage
        self.bus = bus
        self.media_dir = Path(media_dir)
        self.log = log or logging.getLogger("ptzcam.pipeline")
        self.live = LiveView()
        self.ptz = PTZController(cfg_store)
        self.autotracker = AutoTracker(self.ptz, cfg_store)
        self.lightmeter = LightMeter()
        self.night_view = NightView()
        self.levels = AutoLevels()
        self.night = False
        self._night_broken = False  # night vision failed: carry on without it until the detector is rebuilt
        self.night_clipper = NightClipper(cfg_store)  # outlives pipeline restarts, like the PTZ worker
        self.source = self.recorder = self.detector = self.tracker = self.events = None
        self.detector_error = None
        self.fps = 0.0
        self._detector_tried = 0.0
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._first_start = True

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
        d, s, url = cfg["detect"], cfg["storage"], cfg["camera"]["rtsp_url"]
        self.fps = 0.0
        self.source = FrameSource(url, d["width"], d["height"], d["fps"])
        self.recorder = Recorder(url, s["segment_dir"], s["segment_seconds"], s["buffer_seconds"])
        first_id = self.tracker._next_id if self.tracker is not None else 1
        self.tracker = Tracker(aspect=d["width"] / d["height"], min_hits=d["min_hits"], min_move=d["min_move"],
                               first_id=first_id)
        self.autotracker.reset_target()  # the old target id means nothing to the new tracker
        self.events = EventManager(self.cfg, self.storage, self.recorder, self.media_dir, self.bus,
                                   night_clipper=self.night_clipper)
        self._init_detector()
        recover = []
        if self._first_start:  # clips a previous run left pending can still be cut from the RAM buffer
            self._first_start = False
            self.storage.fail_pending_night_clips()
            for row in self.storage.pending_clips():
                if not row["clip"]:
                    self.storage.update_event(row["id"], clip_status="failed")
                    continue
                recover.append(ClipJob(row["id"], row["start_ts"], row["end_ts"], self.media_dir / row["clip"],
                                       self.events._clip_done))
        self.recorder.start(recover)
        self.source.start()
        self._stop = threading.Event()  # one Event per loop, so a late old loop can never resume
        self._thread = threading.Thread(target=self._loop, args=(self._stop,), name="pipeline", daemon=True)
        self._thread.start()

    def _stop_all(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(10)
            if self._thread.is_alive():  # stuck in a native call: never release the detector under it
                self.log.error("pipeline loop did not stop in time; abandoning its detector")
                self.detector = None
        if self.events is not None:
            self.events.close()
        if self.source is not None:
            self.source.stop()
        if self.recorder is not None:
            self.recorder.stop()  # cuts pending clips from what is buffered
        self._close_detector()

    def _init_detector(self):
        self._detector_tried = time.time()
        self._night_broken = False
        try:
            model = self.cfg.data["lowlight"]["model"]
            self.detector = HailoDetector(self.cfg.resolve(self.cfg.data["detect"]["model"]),
                                          enhancer_hef=self.cfg.resolve(model) if model else None)
            self.detector_error = None
        except Exception as e:
            self.detector = None
            self.detector_error = str(e) or e.__class__.__name__
            self.log.error("Hailo detector unavailable: %s", self.detector_error)

    def _close_detector(self):
        if self.detector is not None:
            try:
                self.detector.close()
            except Exception:
                self.log.exception("closing detector failed")
            self.detector = None

    # ---- main loop ----
    def _loop(self, stop: threading.Event):
        source, tracker, events = self.source, self.tracker, self.events
        last_id, was_frozen, last_pub, last_t = 0, False, 0.0, None
        failures = 0
        while not stop.is_set():
            item = source.wait_frame(last_id, timeout=1.0)
            if item is None:
                continue
            fid, ts, frame = item
            last_id = fid
            try:
                cfg = self.cfg.data
                if self.detector is None and time.time() - self._detector_tried > DETECTOR_RETRY_S:
                    self._init_detector()
                det, ll = self.detector, cfg["lowlight"]

                # Night vision: Zero-DCE on the Hailo; its view feeds the live stream, snapshots and night
                # clips, and (if enabled) the detector.
                enhanced = view = None
                if self.lightmeter.update(frame, ts, ll) and det is not None and det.enhancer is not None \
                        and not self._night_broken:
                    try:
                        enhanced = det.enhancer.enhance(frame)
                        view = self.night_view.process(enhanced)  # 712x400: the model's real resolution
                    except Exception:  # never let night vision take detection down with it
                        self.log.exception("night vision failed; continuing without it")
                        self._night_broken = True
                        enhanced = view = None
                # By day: auto-brightness stretches a washed-out picture (sun on the window) back to full range.
                corrected = None
                if enhanced is None:
                    corrected = self.levels.process(frame, correct=cfg["brightness"]["auto"])
                else:
                    self.levels.reset()
                src = corrected if corrected is not None and cfg["brightness"]["detect"] else frame
                dets = det.detect(src, enhanced if ll["detect"] else None) if det is not None else []
                if view is not None:
                    if ll["save_clips"]:
                        self.night_clipper.push(ts, view)
                else:
                    if self.night:
                        self.night_view.reset()  # its temporal filter must not blend a stale scene in later
                    view = corrected if corrected is not None else frame
                self.night = enhanced is not None
                events.night = self.night
                failures = 0

                # Camera motion must never look like object motion: freeze motion tests while the PTZ
                # moves and settles, then restart every track's motion measurement.
                frozen = self.ptz.is_moving() or ts - self.ptz.quiet_since() < cfg["ptz"]["settle_seconds"]
                if was_frozen and not frozen:
                    tracker.reanchor(ts)
                was_frozen = frozen
                tracker.motion_frozen = frozen
                tracker.min_hits = cfg["detect"]["min_hits"]
                tracker.min_move = cfg["detect"]["min_move"]
                thresh = {c: min(TRACK_THRESH, v["min_score"]) for c, v in cfg["detect"]["categories"].items()}
                tracks, removed = tracker.update(dets, ts, thresh)

                zone = cfg["detect"]["zone"]
                # The zone is drawn on the Home view. With PTZ control off the app never moves the camera, so
                # the view is assumed to be the configured one.
                counting = not frozen and (not zone or not cfg["ptz"]["enabled"] or self.ptz.at_home)
                events.process(ts, view, tracks, removed, counting)
                self.autotracker.update(tracks, ts)

                target = self.autotracker.target_id if self.autotracker.enabled else None
                shown = [t for t in tracks if t.confirmed and ts - t.last_ts < OVERLAY_HOLD_S]
                self.live.update(fid, ts, view, [t.overlay_info() for t in shown], target, zone)
                if ts - last_pub >= TRACKS_INTERVAL:
                    self.bus.publish("tracks", {"ts": ts, "tracks": [t.to_json(target) for t in shown]})
                    last_pub = ts
                if last_t is not None:
                    inst = 1.0 / max(1e-3, ts - last_t)
                    self.fps = inst if self.fps == 0 else 0.9 * self.fps + 0.1 * inst
                last_t = ts
            except Exception:
                failures += 1
                self.log.exception("pipeline step failed")
                if failures >= 10 and self.detector is not None:
                    self.log.error("detector keeps failing; reinitialising in %.0f s", DETECTOR_RETRY_S)
                    self._close_detector()
                    self.detector_error = "inference failed repeatedly"
                    failures = 0
                time.sleep(0.2)

    # ---- status ----
    def status(self) -> dict:
        cfg = self.cfg.data
        src, det, rec, at = self.source, self.detector, self.recorder, self.autotracker
        return {
            "camera": {
                "name": cfg["camera"]["name"], "online": bool(src and src.online),
                "fps": round(src.fps, 1) if src else 0.0,
                "width": cfg["detect"]["width"], "height": cfg["detect"]["height"],
                "last_frame_age": src.last_frame_age if src else None,
            },
            "detector": {
                "online": det is not None, "model": Path(cfg["detect"]["model"]).stem,
                "device": det.device if det else None, "fps": round(self.fps, 1) if det else 0.0,
                "infer_ms": round(det.infer_ms, 1) if det else None, "error": self.detector_error,
            },
            "recorder": {
                "online": bool(rec and rec.online),
                "buffer_seconds": round(rec.buffer_seconds(), 1) if rec else 0.0,
            },
            "ptz": {
                "enabled": cfg["ptz"]["enabled"], "connected": self.ptz.connected, "moving": self.ptz.is_moving(),
                "autotrack": at.enabled, "autozoom": cfg["ptz"]["autozoom"],
                "target_id": at.target_id if at.enabled else None, "zoom": self.ptz.zoom_pos,
                "home_preset": cfg["ptz"]["home_preset"], "autofocus": cfg["ptz"]["autofocus"],
                "paused_for": round(at.paused_for(), 1),
            },
            "brightness": {
                "auto": cfg["brightness"]["auto"], "detect": cfg["brightness"]["detect"],
                "gain": round(self.levels.gain, 2), "white": round(self.levels.white * 100, 1),
                "blinded": self.levels.blinded and not self.night,
            },
            "lowlight": {
                "mode": cfg["lowlight"]["mode"], "active": self.night,
                "luma": round(self.lightmeter.luma, 1) if self.lightmeter.luma is not None else None,
                "model": det.enhancer.name if det and det.enhancer else None,
                "enhance_ms": round(det.enhancer.ms, 1) if det and det.enhancer else None,
                "detect": cfg["lowlight"]["detect"], "save_clips": cfg["lowlight"]["save_clips"],
            },
        }
