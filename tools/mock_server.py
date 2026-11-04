#!/usr/bin/env python3
"""Mock backend for the PTZ Camera Web UI, for frontend development.

    .venv/bin/python tools/mock_server.py --port 8090

Implements every endpoint of ptz_camera_web.md section 9 with synthetic data:
  * a few days of events (all categories, multi-object events, a few failed clips)
    and 60 days of counted tracks so day / week / month stats look believable;
  * an in-memory config (section 11) with deep-merge PUT and strict validation, so
    UI mistakes (unknown keys, wrong types) surface as 400 errors;
  * MJPEG live view and single frames drawn over a real camera still, with 2-3
    mock objects drifting across it.  A small "digital PTZ" crops the still, so
    the pad, zoom, presets and auto-follow visibly do something;
  * SSE: status every 2 s, tracks at 5 Hz per camera, and every ~20 s a counts + event_new
    whose clip turns ready via event_update ~5 s later;
  * media (snapshots, thumbs and ffmpeg-generated H.264+AAC clips: the PTZ clip, a wide
    clip and a night-vision clip, each visibly different) served from a temp dir outside
    the project tree, with HTTP Range support.

Two cameras, as in ptzcam/pipeline.py (start with --no-eye for a single-camera server):
  * the "eye", a fixed wide camera, sees the whole still; the PTZ sees a crop of it. Both
    serve ?camera=main|ptz|eye on /api/live.mjpg and /api/frame.jpg, and both publish
    `tracks` ({camera, main}); the eye's track ids start at 2**40;
  * failover: the main camera turns to the eye while the PTZ is washed out (ptz_blinded)
    or offline, with an SSE `main` event; status has cameras, main, counting and sync;
  * auto-follow is aimed by the eye (ptz.mode "eye") while it is synced and eye.follow is on;
  * events carry clip_wide_url / clip_wide_status / main_camera (and the night clip fields).

Mock-only helpers (not part of the real API):
  POST /_mock/state {"camera_online": false, "detector_online": true,
                     "recorder_online": true, "ptz_connected": true,
                     "eye_online": true, "eye_recorder_online": true,
                     "ptz_blinded": false, "sync_calibrated": true, "sync_scheduled": false}
  POST /_mock/event {"hold": 60, "wide_hold": 90}   create an event now; its clip stays
                     pending `hold` s and its wide clip `wide_hold` s (default hold + 2)
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import copy
import json
import logging
import math
import random
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

log = logging.getLogger("mock")

CATEGORIES = ["person", "vehicle", "bicycle", "motorcycle", "animal"]
PRIORITY = ["person", "animal", "motorcycle", "bicycle", "vehicle"]
DIRECTIONS = ["ltr", "rtl", "approach", "leave", "unknown"]
CAT_HEX = {"person": "#4F9DFF", "vehicle": "#FFB020", "bicycle": "#2ED47A",
           "motorcycle": "#FF5C7A", "animal": "#B57BFF"}
CAT_BGR = {k: (int(v[5:7], 16), int(v[3:5], 16), int(v[1:3], 16)) for k, v in CAT_HEX.items()}
MODELS = ["models/yolov11m.hef", "models/yolov8m.hef"]
LOWLIGHT_MODELS = ["models/lowlight/zero_dce.hef", "models/lowlight/zero_dce_pp.hef"]
MASK = "********"
VISCA_ZOOM_MAX = 16384
LIVE_W, LIVE_FPS = 960, 12
VIEW_BASE = 0.8          # fraction of the still visible at zoom 0 (room to pan)
HOME = (0.5, 0.6)        # view centre at home: the street sits low in the still
EYE_ID_BASE = 1 << 40  # track ids of the wide camera start here (ptzcam.ptz.EYE_ID_BASE)
EYE_VIEW = (0.0, 0.0, 1.0)  # the eye is fixed and sees the whole still
WIDE_DAYS = 2            # generated events this recent were recorded by the eye too

# Mirrors ptzcam/config.py DEFAULTS (the UI's Settings form round-trips every key it shows).
DEFAULT_CONFIG = {
    "camera": {"name": "Front", "rtsp_url": "rtsp://192.168.1.87:554/1", "http_url": "http://192.168.1.87",
               "username": "admin", "password": "admin", "visca_host": "192.168.1.87",
               "visca_port": 5678, "visca_address": 1, "usb_device": "", "anti_flicker": -1},
    "eye": {"enabled": True, "name": "Wide", "device": "Avaya HC020", "width": 1920, "height": 1080, "fps": 15,
            "anti_flicker": -1, "zone": [], "count": True, "follow": True, "clips": "auto"},
    "failover": {"mode": "auto", "white": 0.5, "switch_after": 20.0, "return_after": 120.0},
    "detect": {"model": "models/yolov11m.hef", "fps": 15, "width": 1280, "height": 720,
               "min_move": 0.5, "min_hits": 4, "zone": [],
               "categories": {"person": {"enabled": True, "min_score": 0.5},
                              "vehicle": {"enabled": True, "min_score": 0.5},
                              "bicycle": {"enabled": True, "min_score": 0.45},
                              "motorcycle": {"enabled": True, "min_score": 0.45},
                              "animal": {"enabled": True, "min_score": 0.45}}},
    "events": {"clip_seconds": 30, "preroll_seconds": 5},
    "storage": {"data_dir": "data", "segment_dir": "/dev/shm/ptzcam", "segment_seconds": 2,
                "buffer_seconds": 120, "retention_days": 30, "max_storage_gb": 50},
    "ptz": {"enabled": True, "autotrack": False,
            "autotrack_categories": ["person", "vehicle", "bicycle", "motorcycle", "animal"],
            "deadzone": 0.12, "max_pan_speed": 12, "max_tilt_speed": 10, "return_home_seconds": 15,
            "home_preset": 0, "settle_seconds": 2.0, "autozoom": True, "target_size": 0.35,
            "max_zoom_pos": 8192, "autofocus": True, "zoom_curve": [], "zoom_rate": 0.0, "pt_rate": 0.0},
    "lowlight": {"mode": "auto", "model": "models/lowlight/zero_dce.hef", "detect": False, "save_clips": True,
                 "night_luma": 45, "day_luma": 70},
    "brightness": {"auto": True, "detect": True},
    "web": {"host": "0.0.0.0", "port": 8080, "live_width": 960, "live_fps": 12,
            "auth_user": "", "auth_password": "", "allowed_hosts": []},
}
RESTART_KEYS = ["camera.rtsp_url", "detect.model", "detect.fps", "detect.width", "detect.height", "lowlight.model",
                "eye.enabled", "eye.device", "eye.width", "eye.height", "eye.fps"]

# Per-category geometry for mock objects, in normalized still coordinates:
# (crossing time s, box width range, box h/w ratio, bottom-edge y range)
GEOM = {
    "vehicle": ((7, 11), (0.13, 0.20), 0.62, (0.86, 0.94)),
    "person": ((18, 28), (0.035, 0.05), 4.2, (0.86, 0.97)),
    "bicycle": ((10, 14), (0.075, 0.095), 1.05, (0.87, 0.95)),
    "motorcycle": ((6, 9), (0.085, 0.11), 1.0, (0.87, 0.94)),
    "animal": ((14, 22), (0.045, 0.06), 0.95, (0.90, 0.97)),
}
LABELS = {"person": [("person", 1.0)], "vehicle": [("car", 0.8), ("truck", 0.12), ("bus", 0.08)],
          "bicycle": [("bicycle", 1.0)], "motorcycle": [("motorcycle", 1.0)],
          "animal": [("dog", 0.6), ("cat", 0.25), ("bird", 0.15)]}
DIR_WEIGHTS = {"vehicle": [47, 47, 2, 2, 2], "person": [38, 38, 10, 10, 4], "bicycle": [45, 45, 3, 3, 4],
               "motorcycle": [46, 46, 3, 3, 2], "animal": [30, 30, 12, 12, 16]}
# Mean passers-by per hour of day (weekday); a street with commuter peaks.
HOURLY = [0.4, 0.3, 0.2, 0.2, 0.3, 0.8, 2.0, 5.0, 6.5, 4.5, 3.2, 3.0,
          3.4, 3.2, 3.0, 3.6, 5.2, 6.8, 6.0, 4.2, 2.8, 1.8, 1.2, 0.7]


# --------------------------------------------------------------------------- helpers

def local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def parse_day(day: str) -> datetime:
    return datetime.strptime(day, "%Y-%m-%d")


def poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def pick(rng: random.Random, weighted):
    items, weights = zip(*weighted)
    return rng.choices(items, weights)[0]


def cat_weights(hour: int, weekend: bool) -> dict:
    w = {"vehicle": 56, "person": 26, "bicycle": 8, "motorcycle": 4, "animal": 6}
    if hour < 6 or hour >= 22:
        w = {"vehicle": 50, "person": 15, "bicycle": 2, "motorcycle": 5, "animal": 28}
    elif weekend and 9 <= hour < 18:
        w = {"vehicle": 42, "person": 34, "bicycle": 14, "motorcycle": 3, "animal": 7}
    return w


def make_box(rng: random.Random, cat: str, cx: float | None = None) -> list:
    _, (w0, w1), ratio, (b0, b1) = GEOM[cat]
    w = rng.uniform(w0, w1)
    h = w * ratio
    cx = rng.uniform(0.12, 0.88) if cx is None else cx
    bottom = rng.uniform(b0, b1)
    return [cx - w / 2, bottom - h, cx + w / 2, bottom]


def rider_box(base: list) -> list:
    """A person box sitting on a bicycle / motorcycle box."""
    x1, y1, x2, y2 = base
    w = (x2 - x1) * 0.45
    h = (y2 - y1) * 1.35
    cx = (x1 + x2) / 2
    bottom = y1 + (y2 - y1) * 0.55
    return [cx - w / 2, bottom - h, cx + w / 2, bottom]


def r3(x: float) -> float:
    return round(x, 3)


# --------------------------------------------------------------------------- data store

class Store:
    """Counted tracks (stats) and events (media) in memory, shaped like the SQLite schema."""

    def __init__(self, rng: random.Random, cfg):
        self.rng = rng
        self.cfg = cfg            # callable returning the live config
        self.tracks: list[dict] = []
        self.track_by_id: dict[int, dict] = {}
        self.events: dict[int, dict] = {}
        self._next_track = 1
        self._next_event = 1

    # ---- tracks
    def add_track(self, ts: float, cat: str, box: list | None = None, direction: str | None = None,
                  label: str | None = None) -> dict:
        rng = self.rng
        tid = self._next_track
        self._next_track += 1
        t = {
            "track_id": tid, "event_id": None, "day": local_day(ts), "category": cat,
            "label": label or pick(rng, LABELS[cat]),
            "first_ts": ts - rng.uniform(1.5, 4.0), "counted_ts": ts, "last_ts": ts + rng.uniform(2.0, 12.0),
            "max_score": round(rng.uniform(0.55, 0.96), 2),
            "direction": direction or pick(rng, list(zip(DIRECTIONS, DIR_WEIGHTS[cat]))),
            "box": box or make_box(rng, cat),
        }
        self.tracks.append(t)
        self.track_by_id[tid] = t
        return t

    def add_group(self, ts: float, cat: str) -> list[dict]:
        """One passer-by plus the companions that often come with it (rider, dog walker...)."""
        rng = self.rng
        main = self.add_track(ts, cat)
        group = [main]
        if cat in ("bicycle", "motorcycle") and rng.random() < (0.75 if cat == "bicycle" else 0.65):
            group.append(self.add_track(ts + 0.3, "person", rider_box(main["box"]), main["direction"]))
        elif cat == "animal" and main["label"] == "dog" and rng.random() < 0.55:
            x1, _, x2, _ = main["box"]
            group.append(self.add_track(ts + rng.uniform(0.5, 2.5), "person",
                                        make_box(rng, "person", cx=min(0.9, x2 + 0.04)), main["direction"]))
        elif cat == "vehicle" and rng.random() < 0.12:
            group.append(self.add_track(ts + rng.uniform(2, 10), "vehicle"))
        elif cat == "person" and rng.random() < 0.1:
            x1, _, x2, _ = main["box"]
            group.append(self.add_track(ts + rng.uniform(0, 3), "person",
                                        make_box(rng, "person", cx=min(0.9, x2 + 0.03)), main["direction"]))
        return group

    # ---- events
    def new_event(self, trigger: float, status: str, wide: str | None = None, main: str = "ptz",
                  night: bool = False, night_status: str | None = None) -> dict:
        """`wide`: the wide clip's status (None without an eye); `main`: the camera that was main."""
        ev_cfg = self.cfg()["events"]
        pre, clip = ev_cfg["preroll_seconds"], ev_cfg["clip_seconds"]
        eid = self._next_event
        self._next_event += 1
        ev = {"id": eid, "day": local_day(trigger), "start_ts": trigger - pre, "trigger_ts": trigger,
              "end_ts": trigger - pre + clip, "last_seen_ts": trigger, "rows": [],
              "clip_status": status, "clip_wide_status": wide, "main_camera": main,
              "night": night, "clip_night_status": night_status if night else None, "version": 0}
        self.events[eid] = ev
        return ev

    def attach(self, t: dict, ev: dict):
        t["event_id"] = ev["id"]
        ev["rows"].append(t["track_id"])
        ev["last_seen_ts"] = min(ev["end_ts"], max(ev["last_seen_ts"], t["last_ts"]))

    def generate(self, now: float, history_days: int, event_days: int, wide_days: int = 0):
        rng = self.rng
        today = datetime.fromtimestamp(now).date()
        for back in range(history_days - 1, -1, -1):
            d = today - timedelta(days=back)
            weekend = d.weekday() >= 5
            factor = rng.uniform(0.8, 1.2) * (0.8 if weekend else 1.0)
            for hour in range(24):
                hs = datetime(d.year, d.month, d.day, hour).timestamp()
                if hs > now:
                    break
                for _ in range(poisson(rng, HOURLY[hour] * factor)):
                    ts = hs + rng.uniform(0, 3600)
                    if ts < now - 60:      # the last minute belongs to live events
                        self.add_group(ts, pick(rng, cat_weights(hour, weekend).items()))
        self.tracks.sort(key=lambda t: t["counted_ts"])
        first_event_day = today - timedelta(days=event_days - 1)
        cutoff = datetime(first_event_day.year, first_event_day.month, first_event_day.day).timestamp()
        first_wide_day = today - timedelta(days=wide_days - 1)
        wide_cutoff = (datetime(first_wide_day.year, first_wide_day.month, first_wide_day.day).timestamp()
                       if wide_days > 0 else math.inf)
        open_ev = None
        for t in self.tracks:
            if t["counted_ts"] < cutoff:
                continue
            if open_ev is None or t["counted_ts"] > open_ev["end_ts"]:
                ts = t["counted_ts"]
                hour = datetime.fromtimestamp(ts).hour
                night = hour < 6 or hour >= 21
                wide = None
                main = "ptz"
                if ts >= wide_cutoff:  # the eye was installed: failover around midday sun, and wide clips
                    main = "eye" if 10 <= hour < 15 and rng.random() < 0.3 else "ptz"
                    # eye.clips "auto" keeps a wide clip only when it adds something (the PTZ was away, or failover)
                    if main == "eye" or rng.random() < 0.45:
                        wide = "failed" if rng.random() < 0.04 else "ready"
                open_ev = self.new_event(ts, "failed" if rng.random() < 0.05 else "ready", wide=wide, main=main,
                                         night=night, night_status="ready")
            self.attach(t, open_ev)

    def event_json(self, ev: dict) -> dict:
        rows = [self.track_by_id[i] for i in ev["rows"]]
        cats = Counter(r["category"] for r in rows)
        labels = Counter(r["label"] for r in rows)
        base = f"/media/{ev['day']}/{ev['id']}"
        ready = ev["clip_status"] == "ready"
        wide = ev.get("clip_wide_status")
        night = ev.get("clip_night_status")
        return {
            "id": ev["id"], "day": ev["day"], "start_ts": r3(ev["start_ts"]), "trigger_ts": r3(ev["trigger_ts"]),
            "end_ts": r3(ev["end_ts"]), "last_seen_ts": r3(ev["last_seen_ts"]),
            "top_category": next(c for c in PRIORITY if c in cats),
            "categories": {c: cats[c] for c in CATEGORIES if c in cats},
            "labels": dict(labels),
            "max_score": max(r["max_score"] for r in rows),
            "snapshot_url": base + "/snapshot.jpg", "thumb_url": base + "/thumb.jpg",
            "clip_url": base + "/clip.mp4" if ready else None,
            "clip_status": ev["clip_status"],
            "night": bool(ev.get("night")),
            "clip_night_url": base + "/clip_night.mp4" if night == "ready" else None,
            "clip_night_status": night,
            "clip_wide_url": base + "/clip_wide.mp4" if wide == "ready" else None,
            "clip_wide_status": wide,
            "main_camera": ev.get("main_camera") or "ptz",
            # Seconds each camera clip starts before the event window (whole 2 s segments); night clips start on it.
            "clip_lead": round((ev["id"] % 4) * 0.45, 3) if ready else 0.0,
            "clip_wide_lead": round(((ev["id"] + 1) % 4) * 0.4, 3) if wide == "ready" else 0.0,
            "duration": round(ev["end_ts"] - ev["start_ts"], 1),
        }

    def objects(self, ev: dict) -> list[dict]:
        out = []
        for i in ev["rows"]:
            r = self.track_by_id[i]
            out.append({"track_id": r["track_id"], "category": r["category"], "label": r["label"],
                        "first_ts": r3(r["first_ts"]), "counted_ts": r3(r["counted_ts"]),
                        "last_ts": r3(r["last_ts"]), "max_score": r["max_score"],
                        "direction": r["direction"]})
        return out

    def delete_event(self, eid: int) -> bool:
        ev = self.events.pop(eid, None)
        if not ev:
            return False
        for i in ev["rows"]:               # counts are kept, only the link goes
            self.track_by_id[i]["event_id"] = None
        return True

    def counts_for_day(self, day: str) -> dict:
        c = {k: 0 for k in CATEGORIES}
        for t in self.tracks:
            if t["day"] == day:
                c[t["category"]] += 1
        return c

    def days(self, limit: int) -> list[dict]:
        per_day = Counter(ev["day"] for ev in self.events.values())
        out = []
        for day in sorted(per_day, reverse=True)[:limit]:
            out.append({"day": day, "events": per_day[day], "counts": self.counts_for_day(day)})
        return out

    def stats(self, rng_name: str, day: str, category: str | None) -> dict:
        n_days = {"day": 1, "week": 7, "month": 30}[rng_name]
        end_d = parse_day(day)
        start_d = end_d - timedelta(days=n_days - 1)
        start, end = start_d.timestamp(), (end_d + timedelta(days=1)).timestamp()
        prev_start = (start_d - timedelta(days=n_days)).timestamp()
        if rng_name == "day":
            buckets = [(datetime(end_d.year, end_d.month, end_d.day, h).timestamp(), f"{h:02d}:00")
                       for h in range(24)]
        else:
            buckets = []
            for i in range(n_days):
                d = start_d + timedelta(days=i)
                buckets.append((d.timestamp(), f"{d:%b} {d.day}"))
        edges = [b[0] for b in buckets]
        totals = {c: 0 for c in CATEGORIES}
        prev = {c: 0 for c in CATEGORIES}
        series = [{"ts": ts, "label": label, "counts": {c: 0 for c in CATEGORIES}} for ts, label in buckets]
        directions = {c: {d: 0 for d in DIRECTIONS} for c in CATEGORIES}
        heat = [[0] * 24 for _ in range(7)]
        by_hour = [0] * 24
        for t in self.tracks:
            ts = t["counted_ts"]
            if prev_start <= ts < start:
                prev[t["category"]] += 1
                continue
            if not start <= ts < end:
                continue
            cat = t["category"]
            totals[cat] += 1
            series[bisect.bisect_right(edges, ts) - 1]["counts"][cat] += 1
            directions[cat][t["direction"]] += 1
            if category in (None, cat):
                dt = datetime.fromtimestamp(ts)
                heat[dt.weekday()][dt.hour] += 1
                by_hour[dt.hour] += 1
        peak = max(range(24), key=lambda h: by_hour[h])
        busiest = ({"label": f"{peak:02d}:00", "count": by_hour[peak]} if by_hour[peak]
                   else {"label": None, "count": 0})
        return {"range": rng_name, "day": day, "start_ts": start, "end_ts": end,
                "bucket": "hour" if rng_name == "day" else "day", "categories": CATEGORIES,
                "totals": totals, "previous_totals": prev, "series": series,
                "directions": directions, "heatmap": heat, "busiest": busiest}


# --------------------------------------------------------------------------- live scene

class Actor:
    """A mock object crossing the scene, in normalized still ("world") coordinates."""

    def __init__(self, aid: int, rng: random.Random, now: float):
        self.id = aid
        self.cat = pick(rng, [("vehicle", 45), ("person", 33), ("bicycle", 10), ("motorcycle", 5),
                              ("animal", 7)])
        self.label = pick(rng, LABELS[self.cat])
        self.score = rng.uniform(0.62, 0.94)
        (d0, d1), _, _, _ = GEOM[self.cat]
        self.t0, self.dur = now, rng.uniform(d0, d1)
        box = make_box(rng, self.cat, cx=0.5)
        self.w, self.h, self.bottom = box[2] - box[0], box[3] - box[1], box[3]
        self.approach = self.cat == "person" and rng.random() < 0.25
        self.ltr = rng.random() < 0.5
        self.x0, self.x1 = (0.0, 1.0) if self.ltr else (1.0, 0.0)
        if self.approach:
            self.x0, self.x1 = rng.uniform(0.3, 0.7), rng.uniform(0.3, 0.7)

    def alive(self, t: float) -> bool:
        return t - self.t0 < self.dur

    def box(self, t: float) -> list:
        p = min(1.0, max(0.0, (t - self.t0) / self.dur))
        x = self.x0 + (self.x1 - self.x0) * p
        s = 0.55 + 0.9 * p if self.approach else 1.0
        w, h = self.w * s, self.h * s
        bottom = self.bottom - (1 - p) * 0.08 if self.approach else self.bottom
        return [x - w / 2, bottom - h, x + w / 2, bottom]


def to_view(box: list, view: tuple) -> list | None:
    """World box -> view box (normalized to the visible frame), clipped; None if invisible."""
    x0, y0, vs = view
    b = [(box[0] - x0) / vs, (box[1] - y0) / vs, (box[2] - x0) / vs, (box[3] - y0) / vs]
    if b[2] <= 0.01 or b[0] >= 0.99 or b[3] <= 0.01 or b[1] >= 0.99:
        return None
    return [min(1.0, max(0.0, v)) for v in b]


class Renderer:
    """Draws frames from the still: digital-PTZ crop, time-of-day light, silhouettes, boxes, OSD."""

    def __init__(self, still: np.ndarray, cam_name):
        self.still = still
        self.H, self.W = still.shape[:2]
        self.cam_name = cam_name

    @staticmethod
    def daylight(ts: float) -> float:
        dt = datetime.fromtimestamp(ts)
        h = dt.hour + dt.minute / 60
        return max(0.0, math.cos((h - 13) / 24 * 2 * math.pi))   # 1 at 13:00, 0 at night

    def light(self, img: np.ndarray, ts: float) -> np.ndarray:
        day = self.daylight(ts)
        if day < 0.08:                         # night: IR-style black and white
            g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            g = cv2.convertScaleAbs(g, alpha=1.25, beta=6)
            return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)
        return cv2.convertScaleAbs(img, alpha=0.8 + 1.0 * day ** 1.4, beta=10 * day)

    @staticmethod
    def silhouette(img, cat: str, b: tuple, seed: int, ltr: bool = True):
        x1, y1, x2, y2 = b
        w, h = max(2, x2 - x1), max(2, y2 - y1)
        rnd = random.Random(seed)
        tone = rnd.randint(55, 95)
        body = (tone + 10, tone + 4, tone)
        dark = (22, 22, 26)
        aa = cv2.LINE_AA
        if cat == "vehicle":
            cv2.rectangle(img, (x1, y1 + int(h * 0.38)), (x2, y2 - int(h * 0.18)), body, -1, aa)
            roof = np.array([(x1 + int(w * 0.18), y1 + int(h * 0.4)), (x1 + int(w * 0.3), y1),
                             (x1 + int(w * 0.72), y1), (x1 + int(w * 0.86), y1 + int(h * 0.4))], np.int32)
            cv2.fillConvexPoly(img, roof, body, aa)
            win = np.array([(x1 + int(w * 0.26), y1 + int(h * 0.36)), (x1 + int(w * 0.34), y1 + int(h * 0.08)),
                            (x1 + int(w * 0.68), y1 + int(h * 0.08)), (x1 + int(w * 0.78), y1 + int(h * 0.36))],
                           np.int32)
            cv2.fillConvexPoly(img, win, (40, 36, 34), aa)
            r = max(2, int(h * 0.2))
            for fx in (0.22, 0.78):
                cv2.circle(img, (x1 + int(w * fx), y2 - r), r, dark, -1, aa)
            hx = x2 - 3 if ltr else x1 + 3
            cv2.circle(img, (hx, y1 + int(h * 0.5)), max(2, int(h * 0.07)), (200, 240, 255), -1, aa)
        elif cat == "person":
            cv2.circle(img, (x1 + w // 2, y1 + int(h * 0.09)), max(2, int(h * 0.085)), body, -1, aa)
            cv2.rectangle(img, (x1 + int(w * 0.18), y1 + int(h * 0.19)), (x2 - int(w * 0.18), y1 + int(h * 0.58)),
                          body, -1, aa)
            lw = max(2, int(w * 0.22))
            cv2.line(img, (x1 + int(w * 0.38), y1 + int(h * 0.56)), (x1 + int(w * 0.3), y2), body, lw, aa)
            cv2.line(img, (x1 + int(w * 0.62), y1 + int(h * 0.56)), (x1 + int(w * 0.7), y2), body, lw, aa)
        elif cat in ("bicycle", "motorcycle"):
            r = max(3, int(h * 0.3))
            lw = max(1, int(h * 0.06))
            c1, c2 = (x1 + r, y2 - r), (x2 - r, y2 - r)
            for c in (c1, c2):
                cv2.circle(img, c, r, dark if cat == "motorcycle" else body, lw + (2 if cat == "motorcycle" else 0), aa)
            top = (x1 + w // 2, y1 + int(h * 0.42))
            cv2.line(img, c1, top, body, lw + 1, aa)
            cv2.line(img, top, c2, body, lw + 1, aa)
            cv2.line(img, c1, (x1 + w // 2, y2 - r), body, lw + 1, aa)
            if cat == "motorcycle":
                cv2.ellipse(img, (x1 + w // 2, y1 + int(h * 0.5)), (int(w * 0.28), int(h * 0.12)), 0, 0, 360,
                            body, -1, aa)
        else:  # animal
            cv2.ellipse(img, (x1 + w // 2, y1 + int(h * 0.48)), (int(w * 0.36), int(h * 0.2)), 0, 0, 360,
                        body, -1, aa)
            hx = x2 - int(w * 0.14) if ltr else x1 + int(w * 0.14)
            cv2.circle(img, (hx, y1 + int(h * 0.3)), max(2, int(h * 0.15)), body, -1, aa)
            lw = max(1, int(w * 0.08))
            for fx in (0.28, 0.4, 0.62, 0.74):
                cv2.line(img, (x1 + int(w * fx), y1 + int(h * 0.6)), (x1 + int(w * fx), y2), body, lw, aa)

    @staticmethod
    def draw_box(img, cat: str, label: str, score: float, b: tuple, target: bool = False):
        x1, y1, x2, y2 = b
        color = CAT_BGR[cat]
        th = max(2, img.shape[1] // 480)
        cv2.rectangle(img, (x1, y1), (x2, y2), (255, 255, 255) if target else color, th + (1 if target else 0),
                      cv2.LINE_AA)
        text = f"{label} {round(score * 100)}%"
        fs = max(0.42, img.shape[1] / 1900)
        (tw, tht), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, fs, 1)
        ty = y1 - 4 if y1 - tht - 8 > 0 else y2 + tht + 6
        cv2.rectangle(img, (x1 - th // 2, ty - tht - 5), (x1 + tw + 8, ty + base - 1), color, -1)
        cv2.putText(img, text, (x1 + 4, ty - 2), cv2.FONT_HERSHEY_SIMPLEX, fs, (23, 15, 11), 1, cv2.LINE_AA)

    def osd(self, img, ts: float, name: str | None = None):
        text = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        fs = img.shape[1] / 1500
        org = (int(img.shape[1] * 0.018), int(img.shape[0] * 0.06))
        cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, fs, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, fs, (235, 235, 235), 1, cv2.LINE_AA)
        name = name or self.cam_name()
        (tw, _), _ = cv2.getTextSize(name, cv2.FONT_HERSHEY_SIMPLEX, fs, 1)
        org2 = (img.shape[1] - tw - org[0], img.shape[0] - org[0])
        cv2.putText(img, name, org2, cv2.FONT_HERSHEY_SIMPLEX, fs, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(img, name, org2, cv2.FONT_HERSHEY_SIMPLEX, fs, (235, 235, 235), 1, cv2.LINE_AA)

    @staticmethod
    def offline(out_w: int, quality: int = 75) -> bytes:
        """What the real server sends for a camera without fresh frames (ptzcam.draw.offline_frame)."""
        out_h = int(round(out_w * 9 / 16 / 2) * 2)
        img = np.full((out_h, out_w, 3), 24, np.uint8)
        text, fs = "Camera offline", max(0.6, out_w / 960)
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, fs, 2)
        cv2.putText(img, text, ((out_w - tw) // 2, (out_h + th) // 2), cv2.FONT_HERSHEY_SIMPLEX, fs, (170, 170, 170), 2,
                    cv2.LINE_AA)
        return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])[1].tobytes()

    def render(self, view: tuple, objs: list, overlay: bool, ts: float, out_w: int,
               osd: bool = True, quality: int = 78, name: str | None = None, rect: tuple | None = None,
               washout: bool = False) -> bytes:
        """objs: [(id, cat, label, score, world_box, target, ltr)]; `rect`: the PTZ's view (x0, y0, size) in the
        still, outlined on the wide camera's picture; `washout`: sun on the window, nearly all white."""
        x0, y0, vs = view
        px0, py0 = int(round(x0 * self.W)), int(round(y0 * self.H))
        pw, ph = max(16, int(round(vs * self.W))), max(9, int(round(vs * self.H)))
        crop = self.still[py0:py0 + ph, px0:px0 + pw]
        out_h = int(round(out_w * 9 / 16))
        interp = cv2.INTER_AREA if pw > out_w else cv2.INTER_LINEAR
        img = cv2.resize(crop, (out_w, out_h), interpolation=interp)
        placed = []
        for oid, cat, label, score, wbox, target, ltr in objs:
            vb = to_view(wbox, view)
            if vb is None:
                continue
            pb = (int(vb[0] * out_w), int(vb[1] * out_h), int(vb[2] * out_w), int(vb[3] * out_h))
            self.silhouette(img, cat, pb, oid, ltr)
            placed.append((cat, label, score, pb, target))
        img = self.light(img, ts)
        if washout:
            img = cv2.addWeighted(img, 0.18, np.full_like(img, 250), 0.82, 0)
        if overlay:
            for cat, label, score, pb, target in placed:
                self.draw_box(img, cat, label, score, pb, target)
            if rect is not None:
                rx0, ry0, rs = rect
                p0 = (int((rx0 - x0) / vs * out_w), int((ry0 - y0) / vs * out_h))
                p1 = (int((rx0 + rs - x0) / vs * out_w), int((ry0 + rs - y0) / vs * out_h))
                cv2.rectangle(img, p0, p1, (255, 255, 255), 2, cv2.LINE_AA)
                cv2.putText(img, "PTZ", (p0[0] + 6, max(p0[1] + 20, 20)), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                            (255, 255, 255), 2, cv2.LINE_AA)
        if osd:
            self.osd(img, ts, name)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes()


# --------------------------------------------------------------------------- PTZ simulation

class PTZ:
    def __init__(self):
        self.cx, self.cy, self.zoom = HOME[0], HOME[1], 0.0
        self.pan_v = self.tilt_v = 0.0
        self.zoom_v = 0.0
        self.last_move = self.last_zoom = 0.0
        self.autotrack = False
        self.target_id = None
        self.steering = False
        self.lost_since = None
        self.presets: dict[int, tuple] = {}

    def view(self) -> tuple:
        vs = VIEW_BASE / (1 + 3 * self.zoom / VISCA_ZOOM_MAX)
        self.cx = min(1 - vs / 2, max(vs / 2, self.cx))
        self.cy = min(1 - vs / 2, max(vs / 2, self.cy))
        return (self.cx - vs / 2, self.cy - vs / 2, vs)

    @property
    def moving(self) -> bool:
        return bool(self.pan_v or self.tilt_v or self.zoom_v or self.steering)

    def stop(self):
        self.pan_v = self.tilt_v = self.zoom_v = 0.0

    def position(self) -> dict:
        return {"pan": int((self.cx - 0.5) * 4896), "tilt": int((0.5 - self.cy) * 2400), "zoom": int(self.zoom)}


# --------------------------------------------------------------------------- SSE hub

class Hub:
    def __init__(self):
        self.clients: set[asyncio.Queue] = set()

    @staticmethod
    def encode(event: str, data) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()

    def publish(self, event: str, data):
        msg = self.encode(event, data)
        for q in list(self.clients):
            if q.qsize() < 400:          # drop messages for a stuck client rather than grow forever
                q.put_nowait(msg)


# --------------------------------------------------------------------------- app state

class Mock:
    def __init__(self, args):
        self.args = args
        self.rng = random.Random(args.seed)
        self.started = time.time()
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config["eye"]["enabled"] = not args.no_eye
        self.flags = {"camera_online": True, "detector_online": True, "recorder_online": True,
                      "ptz_connected": True, "eye_online": True, "eye_recorder_online": True,
                      "ptz_blinded": False, "sync_calibrated": True, "sync_scheduled": False}
        self.store = Store(self.rng, lambda: self.config)
        self.store.generate(self.started, history_days=60, event_days=args.days,
                            wide_days=0 if args.no_eye else WIDE_DAYS)
        self.hub = Hub()
        self.ptz = PTZ()
        self.actors: list[Actor] = []
        self._next_actor = 100
        self._next_spawn = 0.0
        self.main, self.main_reason, self.main_since = "ptz", "PTZ camera", 0.0
        self.sync_created = self.started - 2 * 86400 - 3600 * 5
        self.tmp = Path(tempfile.mkdtemp(prefix="ptzcam-mock-"))
        self.clips: dict[str, Path] = {}
        self.media_cache: dict[tuple, bytes] = {}
        self.frame_cache: dict[tuple, tuple] = {}
        self.frame_locks: dict[tuple, asyncio.Lock] = {}
        still = cv2.imread(str(args.still)) if args.still else None
        if still is None:
            if args.still:
                log.warning("still %s not found; using a synthetic background", args.still)
            still = np.zeros((1080, 1920, 3), np.uint8)
            still[:] = (60, 45, 35)
            cv2.rectangle(still, (0, 860), (1920, 1080), (70, 70, 70), -1)
        if still.shape[1] != 1920:
            still = cv2.resize(still, (1920, 1080), interpolation=cv2.INTER_AREA)
        self.renderer = Renderer(still, lambda: self.config["camera"]["name"])

    # ---- media
    def make_clip(self):
        """Three test clips made in parallel, each visibly different so switching between them shows: the PTZ
        clip (clip.mp4), the wide camera's (clip_wide.mp4) and the night-vision one (clip_night.mp4)."""
        sources = {"clip.mp4": ("testsrc2=size=1280x720:rate=30", None),
                   "clip_wide.mp4": ("testsrc=size=1280x720:rate=30", None),
                   "clip_night.mp4": ("testsrc2=size=1280x720:rate=30", "hue=s=0,eq=brightness=0.05:contrast=1.2")}

        def make(name, src, vf):
            out = self.tmp / name
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", src,
                   "-f", "lavfi", "-i", "sine=frequency=440:beep_factor=4:sample_rate=48000",
                   *(["-vf", vf] if vf else []),
                   "-t", "10", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
                   "-g", "30", "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-movflags", "+faststart", str(out)]
            subprocess.run(cmd, check=True, timeout=180)
            return name, out

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(make, name, src, vf) for name, (src, vf) in sources.items()]
            for f in futures:
                try:
                    name, out = f.result()
                    self.clips[name] = out
                except (OSError, subprocess.SubprocessError) as exc:
                    log.error("ffmpeg clip generation failed (%s); those clips will 404", exc)
        if self.clips:
            log.info("clips generated in %.1fs: %s", time.time() - t0,
                     ", ".join(f"{n} ({p.stat().st_size // 1024} KB)" for n, p in self.clips.items()))

    def event_media(self, ev: dict, name: str) -> bytes:
        key = (ev["id"], name, ev["version"])
        hit = self.media_cache.get(key)
        if hit:
            return hit
        rows = [self.store.track_by_id[i] for i in ev["rows"]]
        objs = [(r["track_id"], r["category"], r["label"], r["max_score"], r["box"], False,
                 r["direction"] != "rtl") for r in rows]
        ts = ev["trigger_ts"] + 2
        if name == "snapshot.jpg":
            data = self.renderer.render((0.0, 0.0, 1.0), objs, True, ts, 1280, quality=80)
        else:
            top = next(c for c in PRIORITY if any(r["category"] == c for r in rows))
            x1, y1, x2, y2 = next(r["box"] for r in rows if r["category"] == top)
            s = min(1.0, max(0.24, (x2 - x1) * 1.6, (y2 - y1) * 1.9))
            cx = min(1 - s / 2, max(s / 2, (x1 + x2) / 2))
            cy = min(1 - s / 2, max(s / 2, (y1 + y2) / 2))
            data = self.renderer.render((cx - s / 2, cy - s / 2, s), objs, False, ts, 384, osd=False, quality=82)
        if len(self.media_cache) > 600:
            self.media_cache.clear()
        self.media_cache[key] = data
        return data

    # ---- cameras
    def eye_enabled(self) -> bool:
        return bool(self.config["eye"]["enabled"])

    def cam_online(self, cam: str) -> bool:
        if cam == "eye":
            return self.eye_enabled() and self.flags["eye_online"]
        return self.flags["camera_online"]

    def synced(self) -> bool:
        return self.eye_enabled() and self.flags["sync_calibrated"]

    def resolve(self, cam: str) -> str:
        """?camera=main follows the main camera (a failover switches an open stream seamlessly)."""
        return self.main if cam == "main" else cam

    def ptz_mode(self) -> str:
        """"eye" while the wide camera picks targets and aims the PTZ (AutoTracker._check_mode)."""
        ok = (self.synced() and self.cam_online("eye") and self.config["eye"]["follow"]
              and self.config["ptz"]["enabled"] and self.flags["ptz_connected"])
        return "eye" if ok else "ptz"

    def update_main(self, now: float | None = None):
        """MainSelector without its timers (the mock switches at once): the PTZ is main unless it is washed out
        or offline; publishes `main` on a switch."""
        now = now or time.time()
        mode = self.config["failover"]["mode"]
        if not self.eye_enabled():
            want, why = "ptz", "PTZ camera"
        elif not self.flags["eye_online"]:
            want, why = "ptz", "wide camera offline"
        elif not self.flags["camera_online"]:
            want, why = "eye", "PTZ camera offline"
        elif mode in ("ptz", "eye"):
            want, why = mode, "set to the " + ("PTZ" if mode == "ptz" else "wide") + " camera"
        elif self.flags["ptz_blinded"]:
            want, why = "eye", "PTZ picture washed out"
        elif self.main == "eye":
            want, why = "ptz", "PTZ picture clear again"
        else:
            want, why = "ptz", None
        if want == self.main:
            if why:
                self.main_reason = why
            return
        self.main, self.main_reason, self.main_since = want, why or "PTZ camera", now
        log.info("main camera: %s (%s)", self.main, self.main_reason)
        self.hub.publish("main", {"camera": self.main, "reason": self.main_reason, "since": round(self.main_since, 3)})

    def counting(self) -> str:
        eye_ok = self.cam_online("eye")
        if eye_ok and self.config["eye"]["count"] and (self.config["eye"]["zone"] or self.synced()):
            return "eye"
        return "eye" if eye_ok and self.main == "eye" else "ptz"

    def counting_note(self) -> str:
        """Pipeline._counting_note: why the wide camera does not count now."""
        if not self.eye_enabled() or self.counting() == "eye":
            return ""
        if not self.config["eye"]["count"]:
            return "counting on the wide camera is off"
        if not self.cam_online("eye"):
            return "the wide camera is offline"
        if not self.config["eye"]["zone"] and not self.synced():
            return "the wide camera needs the camera sync, or a zone drawn on its picture"
        return ""

    def target_for(self, cam: str):
        """The target as each camera's tracker numbers it (Pipeline._target_for)."""
        p = self.ptz
        if not p.autotrack or p.target_id is None:
            return None
        if self.ptz_mode() == "eye":
            if cam == "eye":
                return p.target_id
            return self.match_id()
        return p.target_id if cam == "ptz" else None

    def match_id(self):
        """The PTZ track of the eye's target, when the PTZ can see it."""
        p = self.ptz
        if self.ptz_mode() != "eye" or p.target_id is None or p.target_id < EYE_ID_BASE:
            return None
        aid = p.target_id - EYE_ID_BASE
        view = p.view()
        for a in self.actors:
            if a.id == aid and to_view(a.box(time.time()), view) is not None:
                return aid
        return None

    # ---- live scene
    def step_actors(self, now: float):
        self.actors = [a for a in self.actors if a.alive(now)]
        want = 3 if int(now / 30) % 2 else 2
        if len(self.actors) < want and now >= self._next_spawn:
            self.actors.append(Actor(self._next_actor, self.rng, now))
            self._next_actor += 1
            self._next_spawn = now + self.rng.uniform(1.5, 4.0)

    def visible_tracks(self, now: float, cam: str = "ptz") -> list[dict]:
        view = EYE_VIEW if cam == "eye" else self.ptz.view()
        base = EYE_ID_BASE if cam == "eye" else 0
        target = self.target_for(cam)
        out = []
        for a in self.actors:
            vb = to_view(a.box(now), view)
            if vb is None:
                continue
            age = now - a.t0
            out.append({"id": base + a.id, "category": a.cat, "label": a.label,
                        "score": round(a.score + 0.03 * math.sin(now * 2 + a.id), 2),
                        "box": [round(v, 4) for v in vb], "moving": True, "counted": age > 1.5,
                        "target": target is not None and base + a.id == target})
        return out

    def scene_objs(self, now: float, cam: str = "ptz") -> list:
        base = EYE_ID_BASE if cam == "eye" else 0
        target = self.target_for(cam)
        return [(a.id, a.cat, a.label, a.score, a.box(now), target is not None and base + a.id == target, a.ltr)
                for a in self.actors]

    def render_cam(self, cam: str, overlay: bool, width: int, quality: int) -> bytes:
        """One frame of a camera ("ptz" | "eye"), or the offline picture."""
        if not self.cam_online(cam):
            return Renderer.offline(width, quality)
        now = time.time()
        if cam == "eye":
            rect = self.ptz.view() if self.synced() else None
            return self.renderer.render(EYE_VIEW, self.scene_objs(now, "eye"), overlay, now, width, True, quality,
                                        name=self.config["eye"]["name"], rect=rect)
        return self.renderer.render(self.ptz.view(), self.scene_objs(now, "ptz"), overlay, now, width, True, quality,
                                    washout=self.flags["ptz_blinded"])

    async def live_jpeg(self, cam: str, overlay: bool, width: int = LIVE_W) -> bytes:
        tick = int(time.time() * LIVE_FPS)
        key = (cam, overlay, width)
        hit = self.frame_cache.get(key)
        if hit and hit[0] == tick:
            return hit[1]
        lock = self.frame_locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self.frame_cache.get(key)
            if hit and hit[0] == tick:
                return hit[1]
            data = await asyncio.to_thread(self.render_cam, cam, overlay, width, 72)
            self.frame_cache[key] = (tick, data)
            return data

    def step_ptz(self, now: float, dt: float):
        p = self.ptz
        if p.pan_v or p.tilt_v:
            if now - p.last_move > 1.0:             # server-side watchdog
                p.pan_v = p.tilt_v = 0.0
            else:
                vs = p.view()[2]
                p.cx += p.pan_v * 0.35 * dt * vs
                p.cy -= p.tilt_v * 0.35 * dt * vs
        if p.zoom_v:
            if now - p.last_zoom > 1.0:
                p.zoom_v = 0.0
            else:
                p.zoom = min(VISCA_ZOOM_MAX, max(0.0, p.zoom + p.zoom_v * 9000 * dt))
        p.steering = False
        if not p.autotrack or p.pan_v or p.tilt_v:
            return
        if self.ptz_mode() == "eye":
            self.step_eye_aim(now, dt)
            return
        cfg = self.config["ptz"]
        view = p.view()
        tracks = {t["id"]: t for t in self.visible_tracks(now)}
        if p.target_id not in tracks:
            cands = [t for t in tracks.values() if t["counted"] and t["category"] in cfg["autotrack_categories"]]
            if cands:
                best = max(cands, key=lambda t: (t["box"][2] - t["box"][0]) * (t["box"][3] - t["box"][1]))
                p.target_id = best["id"]
                p.lost_since = None
            else:
                p.target_id = None
                p.lost_since = p.lost_since or now
                if now - p.lost_since > 1.5 and p.zoom > 0:
                    p.zoom = max(0.0, p.zoom - 6000 * dt)
                    p.steering = True
                if now - p.lost_since > cfg["return_home_seconds"]:
                    p.cx, p.cy = HOME
                return
        t = tracks[p.target_id]
        b = t["box"]
        ex, ey = (b[0] + b[2]) / 2 - 0.5, (b[1] + b[3]) / 2 - 0.5
        if abs(ex) > cfg["deadzone"] / 2 or abs(ey) > cfg["deadzone"] / 2:
            p.cx += ex * 1.6 * dt * view[2]
            p.cy += ey * 1.6 * dt * view[2]
            p.steering = True
        if cfg["autozoom"] and abs(ex) < 0.2 and abs(ey) < 0.2:
            height = b[3] - b[1]
            if height < cfg["target_size"] - 0.08 and p.zoom < cfg["max_zoom_pos"]:
                p.zoom = min(cfg["max_zoom_pos"], p.zoom + 1200 * dt)
                p.steering = True
            elif height > cfg["target_size"] + 0.12:
                p.zoom = max(0.0, p.zoom - 1200 * dt)
                p.steering = True

    def step_eye_aim(self, now: float, dt: float):
        """Eye mode: the wide camera (which sees every actor) picks the target, and the PTZ is pointed straight at
        it through the sync map, even when it is outside the PTZ's own view."""
        p, cfg = self.ptz, self.config["ptz"]
        eye_tracks = {t["id"]: t for t in self.visible_tracks(now, "eye")}
        if p.target_id not in eye_tracks:
            if p.target_id is not None and p.target_id < EYE_ID_BASE:  # a PTZ track was clicked: find it in the eye
                p.target_id += EYE_ID_BASE
            if p.target_id not in eye_tracks:
                cands = [t for t in eye_tracks.values()
                         if t["counted"] and t["category"] in cfg["autotrack_categories"]]
                if not cands:
                    p.target_id = None
                    p.lost_since = p.lost_since or now
                    if now - p.lost_since > cfg["return_home_seconds"]:
                        p.cx, p.cy, p.zoom = HOME[0], HOME[1], 0.0
                    return
                best = max(cands, key=lambda t: (t["box"][2] - t["box"][0]) * (t["box"][3] - t["box"][1]))
                p.target_id = best["id"]
                p.lost_since = None
        b = eye_tracks[p.target_id]["box"]  # the eye sees the whole still: its box is in world coordinates
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        k = min(1.0, 4.0 * dt)  # absolute moves: quick, and they do not overshoot
        if abs(cx - p.cx) > 0.004 or abs(cy - p.cy) > 0.004:
            p.cx += (cx - p.cx) * k
            p.cy += (cy - p.cy) * k
            p.steering = True
        if cfg["autozoom"]:
            vs_want = min(VIEW_BASE, max(0.05, (b[3] - b[1]) / max(0.1, cfg["target_size"])))
            zoom_want = min(cfg["max_zoom_pos"], max(0.0, (VIEW_BASE / vs_want - 1) * VISCA_ZOOM_MAX / 3))
            if abs(zoom_want - p.zoom) > 150:
                p.zoom += (zoom_want - p.zoom) * k
                p.steering = True

    # ---- status
    def cam_status(self, cam: str, now: float) -> dict:
        online = self.cam_online(cam)
        up = now - self.started
        if cam == "eye":
            white = 3.2 + 0.4 * math.sin(now / 13)
            fps = round(14.9 + 0.08 * math.sin(now / 5), 1) if online else 0.0
            rec_on = self.flags["eye_recorder_online"] and online
            name = self.config["eye"]["name"]
            blinded = False
        else:
            blinded = self.flags["ptz_blinded"]
            white = 86.0 + 2 * math.sin(now / 9) if blinded else 6.5 + 1.5 * math.sin(now / 17)
            fps = round(14.85 + 0.1 * math.sin(now / 7), 1) if online else 0.0
            rec_on = self.flags["recorder_online"] and online
            name = self.config["camera"]["name"]
        night = Renderer.daylight(now) < 0.08 and cam == self.main and self.config["lowlight"]["mode"] != "off"
        return {"name": name, "online": online, "fps": fps,
                "last_frame_age": 0.05 if online else round(up % 60 + 5, 1),
                "white": round(white, 1) if online else 0.0, "blinded": blinded and online, "night": night and online,
                "luma": round(30 + 90 * Renderer.daylight(now), 1),
                "recorder": {"online": rec_on, "buffer_seconds": round(min(118.0, up), 1)}}

    def status(self) -> dict:
        now = time.time()
        up = now - self.started
        det_on = self.flags["detector_online"]
        model = Path(self.config["detect"]["model"]).stem
        cfg_ptz = self.config["ptz"]
        cameras = {"ptz": self.cam_status("ptz", now)}
        if self.eye_enabled():
            cameras["eye"] = self.cam_status("eye", now)
        main = cameras[self.main] if self.main in cameras else cameras["ptz"]
        fps = max(c["fps"] for c in cameras.values())
        p = self.ptz
        mode = self.ptz_mode()
        synced = self.synced()
        ll = self.config["lowlight"]
        br = self.config["brightness"]
        # Auto brightness stretches a hazy daytime PTZ picture; nothing to stretch at night or when blinded.
        hazy = self.main == "ptz" and not main["night"] and not main["blinded"] and main["white"] > 5
        gain = 1.35 if hazy else 1.0
        return {
            "time": round(now, 3), "uptime": round(up, 1), "version": "1.0.0-mock",
            "camera": {"name": main["name"], "online": main["online"], "fps": main["fps"],
                       "width": 1280, "height": 720, "last_frame_age": main["last_frame_age"]},
            "cameras": cameras,
            "main": {"camera": self.main, "reason": self.main_reason, "since": round(self.main_since, 3),
                     "mode": self.config["failover"]["mode"]},
            "counting": self.counting(),
            "counting_note": self.counting_note(),
            "sync": {"calibrated": synced, "created": round(self.sync_created, 3) if synced else None,
                     # tools/sync_when_clear.py's progress (flag sync_scheduled)
                     "scheduled": ({"state": "waiting", "since": round(self.started, 3), "clear_for": 240,
                                    "need": 600, "error": None, "updated": round(now, 3)}
                                   if self.flags["sync_scheduled"] and not synced else None),
                     "positions": 28 if synced else 0,
                     "residuals": ({"pan_median": 6.1, "pan_max": 19.4, "tilt_median": 4.2, "tilt_max": 13.0,
                                    "eye_median": 0.0123, "eye_max": 0.0371} if synced else {})},
            "detector": {"online": det_on and main["online"], "model": model, "device": "HAILO10H",
                         "fps": fps if det_on else 0.0,
                         "infer_ms": round(28.1 + 0.6 * math.sin(now / 3), 1) if det_on else 0.0, "error": None},
            "recorder": cameras["ptz"]["recorder"],
            "ptz": {"enabled": cfg_ptz["enabled"], "connected": self.flags["ptz_connected"],
                    "moving": p.moving, "autotrack": p.autotrack, "autozoom": cfg_ptz["autozoom"],
                    "target_id": p.target_id if p.autotrack else None, "zoom": int(p.zoom),
                    "home_preset": cfg_ptz["home_preset"], "autofocus": cfg_ptz["autofocus"], "paused_for": 0.0,
                    "mode": mode, "match_id": self.match_id() if mode == "eye" else None},
            "brightness": {"auto": br["auto"], "detect": br["detect"], "gain": gain, "white": main["white"],
                           "blinded": main["blinded"], "camera": self.main},
            "lowlight": {"mode": ll["mode"], "active": main["night"], "luma": main["luma"],
                         "model": Path(ll["model"]).stem if ll["model"] else None,
                         "enhance_ms": 19.2 if main["night"] else None, "detect": ll["detect"],
                         "save_clips": ll["save_clips"]},
            "system": {"cpu_percent": round(34 + 6 * math.sin(now / 11), 1),
                       "cpu_temp": round(57.5 + 2 * math.sin(now / 23), 1),
                       "mem_percent": 12.3, "disk_free_gb": 100.2, "disk_total_gb": 117.0,
                       "media_gb": round(0.0042 * len(self.store.events), 2),
                       "media_since": min((e["start_ts"] for e in self.store.events.values()), default=None),
                       "load": [1.21, 1.04, 0.86]},
            "today": self.store.counts_for_day(local_day(now)),
        }

    def publish_tracks(self, now: float):
        for cam in ("ptz", "eye"):
            if cam == "eye" and not self.eye_enabled():
                continue
            if self.cam_online(cam):
                self.hub.publish("tracks", {"ts": round(now, 3), "camera": cam, "main": cam == self.main,
                                            "tracks": self.visible_tracks(now, cam)})

    # ---- live events
    def create_live_event(self, hold: float = 5.0, wide_hold: float | None = None) -> dict:
        now = time.time()
        cat = pick(self.rng, [("vehicle", 50), ("person", 28), ("bicycle", 10), ("motorcycle", 5),
                              ("animal", 7)])
        group = self.store.add_group(now - 0.5, cat)
        night = self.cam_status(self.main, now)["night"] and self.config["lowlight"]["save_clips"]
        clips = self.config["eye"]["clips"]
        eye = self.eye_enabled() and clips != "never"
        # With eye.clips "auto" the wide clip is kept only when the PTZ clip does not show the whole scene.
        keep = clips == "always" or self.main == "eye" or self.ptz.autotrack or self.ptz_away()
        ev = self.store.new_event(now - 0.5, "pending", wide="pending" if eye else None, main=self.main,
                                  night=night, night_status="pending")
        for t in group:
            self.store.attach(t, ev)
        self.hub.publish("counts", self.store.counts_for_day(local_day(now)))
        self.hub.publish("event_new", self.store.event_json(ev))
        loop = asyncio.get_running_loop()
        # A camera that is offline when the event opens has nothing in its ring: its clip fails.
        loop.call_later(hold, self.finish_clip, ev["id"], "clip", self.cam_online("ptz"))
        if eye:  # the wide clip is encoded, so it comes a little later
            loop.call_later(hold + 2 if wide_hold is None else wide_hold, self.finish_clip, ev["id"], "clip_wide",
                            self.cam_online("eye"), keep)
        if night:
            loop.call_later(hold + 1, self.finish_clip, ev["id"], "clip_night")
        return ev

    def ptz_away(self) -> bool:
        p = self.ptz
        return abs(p.cx - HOME[0]) > 0.02 or abs(p.cy - HOME[1]) > 0.02 or p.zoom > 100

    def finish_clip(self, eid: int, kind: str = "clip", ok: bool = True, keep: bool = True):
        ev = self.store.events.get(eid)
        key = f"{kind}_status"
        if not ev or ev.get(key) != "pending":
            return
        if not keep:  # a wide clip that adds nothing is dropped (EventManager._wide_clip_done)
            ev[key] = None
        else:
            ev[key] = "ready" if ok and f"{kind}.mp4" in self.clips else "failed"
        ev["version"] += 1
        self.hub.publish("event_update", self.store.event_json(ev))


# --------------------------------------------------------------------------- config validation

def _fail(msg: str):
    raise ValueError(msg)


def _num(p, v, lo, hi, integer=False):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{p} must be a number")
    if integer and float(v) != int(v):
        raise ValueError(f"{p} must be an integer")
    if not lo <= v <= hi:
        raise ValueError(f"{p} must be between {lo} and {hi}")
    return int(v) if integer else v


def _zone(p, v):
    if v and not 3 <= len(v) <= 64:
        raise ValueError(f"{p} needs 0 or 3-64 points")
    for pt in v:
        if (not isinstance(pt, list) or len(pt) != 2
                or any(isinstance(c, bool) or not isinstance(c, (int, float)) or not 0 <= c <= 1 for c in pt)):
            raise ValueError(f"{p} points must be [x, y] with 0 <= x, y <= 1")
    return v


def _cats(p, v):
    if any(c not in CATEGORIES for c in v) or len(set(v)) != len(v):
        raise ValueError(f"{p} must be a list of distinct categories")
    return v


def _choice(*allowed):
    return lambda p, v: v if v in allowed else _fail(f"{p} must be one of {', '.join(map(str, allowed))}")


RULES = {
    "camera.name": lambda p, v: v if 1 <= len(v.strip()) <= 40 else _fail(f"{p} must be 1-40 characters"),
    "camera.rtsp_url": lambda p, v: v if v.startswith("rtsp://") else _fail(f"{p} must start with rtsp://"),
    "camera.visca_port": lambda p, v: _num(p, v, 1, 65535, True),
    "camera.anti_flicker": lambda p, v: _num(p, v, -1, 2, True),
    "eye.width": lambda p, v: _num(p, v, 320, 1920, True),
    "eye.height": lambda p, v: _num(p, v, 180, 1080, True),
    "eye.fps": lambda p, v: _num(p, v, 5, 30, True),
    "eye.anti_flicker": lambda p, v: _num(p, v, -1, 2, True),
    "eye.zone": _zone,
    "eye.clips": _choice("auto", "always", "never"),
    "failover.mode": _choice("auto", "ptz", "eye"),
    "failover.white": lambda p, v: _num(p, v, 0.05, 1.0),
    "failover.switch_after": lambda p, v: _num(p, v, 0, 3600),
    "failover.return_after": lambda p, v: _num(p, v, 0, 86400),
    "lowlight.mode": _choice("auto", "on", "off"),
    "lowlight.model": lambda p, v: v if v == "" or v in LOWLIGHT_MODELS else _fail(f"unknown low-light model {v}"),
    "lowlight.night_luma": lambda p, v: _num(p, v, 1, 200),
    "lowlight.day_luma": lambda p, v: _num(p, v, 2, 250),
    "detect.model": lambda p, v: v if v in MODELS else _fail(f"unknown model {v}"),
    "detect.min_move": lambda p, v: _num(p, v, 0.05, 5),
    "detect.zone": _zone,
    "detect.categories.*.min_score": lambda p, v: _num(p, v, 0.05, 0.99),
    "events.clip_seconds": lambda p, v: _num(p, v, 10, 60, True),
    "events.preroll_seconds": lambda p, v: _num(p, v, 0, 10, True),
    "storage.retention_days": lambda p, v: _num(p, v, 1, 3650, True),
    "storage.max_storage_gb": lambda p, v: _num(p, v, 1, 10000),
    "ptz.autotrack_categories": _cats,
    "ptz.deadzone": lambda p, v: _num(p, v, 0.0, 0.5),
    "ptz.max_pan_speed": lambda p, v: _num(p, v, 1, 24, True),
    "ptz.max_tilt_speed": lambda p, v: _num(p, v, 1, 24, True),
    "ptz.return_home_seconds": lambda p, v: _num(p, v, 0, 3600),
    "ptz.home_preset": lambda p, v: _num(p, v, 0, 254, True),
    "ptz.target_size": lambda p, v: _num(p, v, 0.05, 0.95),
    "ptz.max_zoom_pos": lambda p, v: _num(p, v, 0, VISCA_ZOOM_MAX, True),
}


def check_value(path: str, v, default):
    if isinstance(default, bool):
        if not isinstance(v, bool):
            raise ValueError(f"{path} must be true or false")
    elif isinstance(default, (int, float)):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{path} must be a number")
    elif isinstance(default, str):
        if not isinstance(v, str):
            raise ValueError(f"{path} must be a string")
    elif isinstance(default, list):
        if not isinstance(v, list):
            raise ValueError(f"{path} must be a list")
    parts = path.split(".")
    rule = RULES.get(path)
    if rule is None and len(parts) == 4 and parts[1] == "categories":
        rule = RULES.get(f"{parts[0]}.categories.*.{parts[3]}")
    return rule(path, v) if rule else v


def merge_config(target: dict, patch: dict, schema: dict, path: str = ""):
    for k, v in patch.items():
        p = f"{path}.{k}" if path else k
        if k not in schema:
            raise ValueError(f"unknown config key: {p}")
        if isinstance(schema[k], dict):
            if not isinstance(v, dict):
                raise ValueError(f"{p} must be an object")
            merge_config(target[k], v, schema[k], p)
        else:
            if p == "camera.password" and v == MASK:
                continue
            target[k] = check_value(p, v, schema[k])


def masked(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    out["camera"]["password"] = MASK
    if out["web"].get("auth_password"):
        out["web"]["auth_password"] = MASK
    return out


def get_path(d: dict, path: str):
    for k in path.split("."):
        d = d[k]
    return d


# --------------------------------------------------------------------------- HTTP handlers

def jerr(status: int, msg: str):
    return web.json_response({"error": msg}, status=status)


async def read_json(request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(reason="invalid JSON body")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(reason="JSON body must be an object")
    return body


def M(request) -> Mock:
    return request.app["mock"]


async def index(request):
    return web.FileResponse(WEB_DIR / "index.html")


async def api_status(request):
    return web.json_response(M(request).status())


def _camera(request):
    """?camera=main (default: whichever is main right now), ptz or eye, as in ptzcam/web.py."""
    cam = request.query.get("camera", "main")
    return cam if cam in ("main", "ptz", "eye") else None


async def api_live(request):
    m = M(request)
    overlay = request.query.get("overlay", "1") != "0"
    cam = _camera(request)
    if cam is None:
        return jerr(400, "camera must be main, ptz or eye")
    resp = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame",
                                       "Cache-Control": "no-cache, no-store, must-revalidate",
                                       "Pragma": "no-cache"})
    await resp.prepare(request)
    try:
        while True:
            which = m.resolve(cam)  # re-read each frame: "main" follows a failover at once
            online = m.cam_online(which)
            jpg = await m.live_jpeg(which, overlay)
            await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg)
                             + jpg + b"\r\n")
            # Like the real server: a camera without new frames repeats its "Camera offline" picture every 2 s.
            await asyncio.sleep(1 / LIVE_FPS if online else 2.0)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp


async def api_frame(request):
    m = M(request)
    try:
        width = max(160, min(1920, int(request.query.get("width", "1280")))) // 2 * 2
    except ValueError:
        return jerr(400, "width must be an integer")
    cam = _camera(request)
    if cam is None:
        return jerr(400, "camera must be main, ptz or eye")
    overlay = request.query.get("overlay", "0") != "0"
    data = await asyncio.to_thread(m.render_cam, m.resolve(cam), overlay, width, 85)
    return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def api_stream(request):
    m = M(request)
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    q: asyncio.Queue = asyncio.Queue()
    m.hub.clients.add(q)
    try:
        await resp.write(b": mock stream\n\n" + m.hub.encode("status", m.status()))
        while True:
            await resp.write(await q.get())
    except ConnectionResetError:
        pass
    finally:
        m.hub.clients.discard(q)
    return resp


async def api_events(request):
    m = M(request)
    qs = request.query
    try:
        limit = max(1, min(500, int(qs.get("limit", "50"))))
        before = float(qs["before"]) if qs.get("before") else None
    except ValueError:
        return jerr(400, "limit/before must be numbers")
    day, cat = qs.get("day") or None, qs.get("category") or None
    if cat and cat not in CATEGORIES:
        return jerr(400, f"unknown category {cat}")
    evs = sorted(m.store.events.values(), key=lambda e: e["start_ts"], reverse=True)
    out = []
    for ev in evs:
        if day and ev["day"] != day:
            continue
        if before is not None and ev["start_ts"] >= before:
            continue
        if cat and not any(m.store.track_by_id[i]["category"] == cat for i in ev["rows"]):
            continue
        out.append(ev)
        if len(out) > limit:
            break
    return web.json_response({"events": [m.store.event_json(e) for e in out[:limit]],
                              "has_more": len(out) > limit})


def _event_or_404(request) -> dict:
    try:
        eid = int(request.match_info["eid"])
    except ValueError:
        raise web.HTTPBadRequest(reason="bad event id")
    ev = M(request).store.events.get(eid)
    if not ev:
        raise web.HTTPNotFound(reason=f"event {eid} not found")
    return ev


async def api_event(request):
    m = M(request)
    ev = _event_or_404(request)
    return web.json_response({**m.store.event_json(ev), "objects": m.store.objects(ev)})


async def api_event_delete(request):
    m = M(request)
    ev = _event_or_404(request)
    m.store.delete_event(ev["id"])
    m.hub.publish("event_deleted", {"id": ev["id"]})
    return web.json_response({"ok": True})


async def api_days(request):
    try:
        limit = max(1, min(400, int(request.query.get("limit", "60"))))
    except ValueError:
        return jerr(400, "limit must be an integer")
    return web.json_response({"days": M(request).store.days(limit)})


async def api_stats(request):
    qs = request.query
    rng_name = qs.get("range", "day")
    if rng_name not in ("day", "week", "month"):
        return jerr(400, "range must be day, week or month")
    day = qs.get("day") or local_day(time.time())
    try:
        parse_day(day)
    except ValueError:
        return jerr(400, "day must be YYYY-MM-DD")
    cat = qs.get("category") or None
    if cat and cat not in CATEGORIES:
        return jerr(400, f"unknown category {cat}")
    return web.json_response(M(request).store.stats(rng_name, day, cat))


def _ptz_ready(m: Mock):
    if not m.config["ptz"]["enabled"]:
        raise web.HTTPConflict(reason="PTZ is disabled")
    if not m.flags["ptz_connected"]:
        raise web.HTTPServiceUnavailable(reason="PTZ not connected")


async def api_ptz_move(request):
    m = M(request)
    body = await read_json(request)
    _ptz_ready(m)
    try:
        pan = _num("pan", body.get("pan", 0), -1, 1)
        tilt = _num("tilt", body.get("tilt", 0), -1, 1)
        speed = _num("speed", body.get("speed", 0.5), 0, 1)
    except ValueError as exc:
        return jerr(400, str(exc))
    p = m.ptz
    if p.autotrack:                        # manual control overrides auto-follow, like the real thing
        p.autotrack, p.target_id = False, None
    p.pan_v, p.tilt_v = pan * max(speed, 0.05), tilt * max(speed, 0.05)
    p.last_move = time.time()
    return web.json_response({"ok": True})


async def api_ptz_stop(request):
    m = M(request)
    _ptz_ready(m)
    m.ptz.stop()
    return web.json_response({"ok": True})


async def api_ptz_zoom(request):
    m = M(request)
    body = await read_json(request)
    _ptz_ready(m)
    direction = body.get("dir")
    if direction not in ("in", "out", "stop"):
        return jerr(400, "dir must be in, out or stop")
    try:
        speed = _num("speed", body.get("speed", 0.5), 0, 1)
    except ValueError as exc:
        return jerr(400, str(exc))
    m.ptz.zoom_v = {"in": 1, "out": -1, "stop": 0}[direction] * max(speed, 0.05)
    m.ptz.last_zoom = time.time()
    return web.json_response({"ok": True})


async def api_ptz_home(request):
    m = M(request)
    _ptz_ready(m)
    p = m.ptz
    p.stop()
    p.autotrack, p.target_id = False, None
    p.cx, p.cy, p.zoom = HOME[0], HOME[1], 0.0
    return web.json_response({"ok": True})


async def api_ptz_preset(request):
    m = M(request)
    body = await read_json(request)
    _ptz_ready(m)
    action = body.get("action")
    if action not in ("call", "set"):
        return jerr(400, "action must be call or set")
    try:
        n = _num("preset", body.get("preset"), 1, 254, True)
    except ValueError as exc:
        return jerr(400, str(exc))
    p = m.ptz
    if action == "set":
        p.presets[n] = (p.cx, p.cy, p.zoom)
    else:
        p.stop()
        p.autotrack, p.target_id = False, None
        default = (HOME[0] + 0.06 * math.cos(n), HOME[1] - 0.04 * abs(math.sin(n)), 1200.0 * (n % 4))
        p.cx, p.cy, p.zoom = p.presets.get(n, default)
    return web.json_response({"ok": True})


async def api_ptz_autotrack(request):
    m = M(request)
    body = await read_json(request)
    enabled = body.get("enabled")
    track_id = body.get("track_id")
    if not isinstance(enabled, bool):
        return jerr(400, "enabled must be true or false")
    if track_id is not None and (isinstance(track_id, bool) or not isinstance(track_id, int)):
        return jerr(400, "track_id must be an integer or null")
    _ptz_ready(m)
    p = m.ptz
    p.autotrack = enabled
    p.lost_since = None
    if not enabled:
        p.stop()
        p.target_id = None
    else:
        alive = {a.id for a in m.actors}
        aid = track_id - EYE_ID_BASE if track_id is not None and track_id >= EYE_ID_BASE else track_id
        p.target_id = track_id if aid in alive else None
        if p.target_id is not None and p.target_id < EYE_ID_BASE and m.ptz_mode() == "eye":
            p.target_id += EYE_ID_BASE  # the eye leads: a PTZ pick is found in the eye's picture (here: at once)
    return web.json_response({"ok": True, "autotrack": p.autotrack, "target_id": p.target_id})


async def api_ptz_position(request):
    m = M(request)
    if not m.flags["ptz_connected"]:
        return jerr(503, "PTZ not connected")
    return web.json_response(m.ptz.position())


async def api_config_get(request):
    return web.json_response(masked(M(request).config))


async def api_config_put(request):
    m = M(request)
    patch = await read_json(request)
    new = copy.deepcopy(m.config)
    try:
        merge_config(new, patch, DEFAULT_CONFIG)
        if new["events"]["preroll_seconds"] >= new["events"]["clip_seconds"]:
            raise ValueError("events.preroll_seconds must be shorter than events.clip_seconds")
        if new["lowlight"]["night_luma"] >= new["lowlight"]["day_luma"]:
            raise ValueError("lowlight.night_luma must be below lowlight.day_luma")
        if new["eye"]["enabled"] and not new["eye"]["device"].strip():
            raise ValueError("eye.device must name the USB camera")
    except ValueError as exc:
        return jerr(400, str(exc))
    restarted = any(get_path(new, k) != get_path(m.config, k) for k in RESTART_KEYS)
    m.config = new
    if restarted:
        m.ptz.target_id = None  # old track ids mean nothing to the new trackers
    m.update_main()
    log.info("config updated: %s%s", json.dumps(patch), " (pipeline restarted)" if restarted else "")
    m.hub.publish("status", m.status())
    return web.json_response({"ok": True, "config": masked(new), "restarted": restarted})


async def api_models(request):
    cfg = M(request).config
    return web.json_response({"models": MODELS, "current": cfg["detect"]["model"],
                              "lowlight_models": LOWLIGHT_MODELS, "lowlight_current": cfg["lowlight"]["model"]})


async def media(request):
    m = M(request)
    ev = m.store.events.get(int(request.match_info["eid"])) if request.match_info["eid"].isdigit() else None
    name = request.match_info["name"]
    if not ev or ev["day"] != request.match_info["day"]:
        raise web.HTTPNotFound()
    if name in ("clip.mp4", "clip_wide.mp4", "clip_night.mp4"):
        if ev.get(f"{name[:-4]}_status") != "ready" or name not in m.clips:
            raise web.HTTPNotFound()
        return web.FileResponse(m.clips[name], headers={"Cache-Control": "max-age=3600"})
    if name in ("snapshot.jpg", "thumb.jpg"):
        data = await asyncio.to_thread(m.event_media, ev, name)
        return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "max-age=300"})
    raise web.HTTPNotFound()


async def mock_state(request):
    m = M(request)
    body = await read_json(request)
    for k, v in body.items():
        if k in m.flags and isinstance(v, bool):
            m.flags[k] = v
    m.update_main()
    m.hub.publish("status", m.status())
    return web.json_response({"ok": True, "flags": m.flags, "main": m.main})


async def mock_event(request):
    m = M(request)
    body = await read_json(request) if request.can_read_body else {}
    wide_hold = body.get("wide_hold")
    ev = m.create_live_event(hold=float(body.get("hold", 5)),
                             wide_hold=float(wide_hold) if wide_hold is not None else None)
    return web.json_response(m.store.event_json(ev))


# --------------------------------------------------------------------------- app wiring

@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except web.HTTPException as exc:
        if request.path.startswith("/api/") and exc.status >= 400:
            return jerr(exc.status, exc.reason)
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a mock should explain, not crash
        log.exception("handler error on %s", request.path)
        return jerr(500, f"internal error: {exc}")


async def no_cache_static(request, response):
    if request.path == "/" or request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"


async def ticker(app):
    m: Mock = app["mock"]
    last_status = last_tracks = 0.0
    next_event = time.time() + m.args.event_interval * 0.5
    prev = time.time()
    while True:
        await asyncio.sleep(0.05)
        now = time.time()
        dt, prev = now - prev, now
        m.step_actors(now)
        m.step_ptz(now, dt)
        m.update_main(now)
        if now - last_tracks >= 0.2:
            last_tracks = now
            m.publish_tracks(now)
        if now - last_status >= 2.0:
            last_status = now
            m.hub.publish("status", m.status())
        if m.args.event_interval > 0 and now >= next_event:
            next_event = now + m.rng.uniform(0.75, 1.25) * m.args.event_interval
            if m.cam_online(m.main):
                m.create_live_event()


async def on_startup(app):
    app["ticker"] = asyncio.create_task(ticker(app))


async def on_cleanup(app):
    app["ticker"].cancel()
    shutil.rmtree(app["mock"].tmp, ignore_errors=True)


def build_app(m: Mock) -> web.Application:
    app = web.Application(middlewares=[errors])
    app["mock"] = m
    r = app.router
    r.add_get("/", index)
    r.add_static("/static/", WEB_DIR, show_index=False, follow_symlinks=False)
    r.add_get("/media/{day}/{eid}/{name}", media)
    r.add_get("/api/status", api_status)
    r.add_get("/api/live.mjpg", api_live)
    r.add_get("/api/frame.jpg", api_frame)
    r.add_get("/api/stream", api_stream)
    r.add_get("/api/events", api_events)
    r.add_get("/api/events/{eid}", api_event)
    r.add_delete("/api/events/{eid}", api_event_delete)
    r.add_get("/api/days", api_days)
    r.add_get("/api/stats", api_stats)
    r.add_post("/api/ptz/move", api_ptz_move)
    r.add_post("/api/ptz/stop", api_ptz_stop)
    r.add_post("/api/ptz/zoom", api_ptz_zoom)
    r.add_post("/api/ptz/home", api_ptz_home)
    r.add_post("/api/ptz/preset", api_ptz_preset)
    r.add_post("/api/ptz/autotrack", api_ptz_autotrack)
    r.add_get("/api/ptz/position", api_ptz_position)
    r.add_get("/api/config", api_config_get)
    r.add_put("/api/config", api_config_put)
    r.add_get("/api/models", api_models)
    r.add_post("/_mock/state", mock_state)
    r.add_post("/_mock/event", mock_event)
    app.on_response_prepare.append(no_cache_static)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    ap = argparse.ArgumentParser(description="Mock PTZ Camera Web backend for UI development")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--still", help="camera still (JPEG) used for live frames and media; default: a synthetic one")
    ap.add_argument("--days", type=int, default=6, help="days of events to generate (stats cover 60 days)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--event-interval", type=float, default=20.0, help="seconds between live events; 0 = off")
    ap.add_argument("--no-eye", action="store_true", help="start without the wide camera (eye.enabled false)")
    ap.add_argument("--verbose", action="store_true", help="log every request")
    args = ap.parse_args()
    if args.port == 8080:
        sys.exit("port 8080 belongs to the real backend; use 8090")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    m = Mock(args)
    try:
        m.make_clip()
        log.info("generated %d events over %d days, %d tracks over 60 days; media in %s",
                 len(m.store.events), args.days, len(m.store.tracks), m.tmp)
        web.run_app(build_app(m), host=args.host, port=args.port, shutdown_timeout=1.0,
                    access_log=log if args.verbose else None, print=lambda s: log.info(s.strip()))
    finally:
        shutil.rmtree(m.tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
