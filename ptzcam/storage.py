"""SQLite storage: events (one per clip) and tracks (one per counted passer-by), plus statistics."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from bisect import bisect_right

from .config import CATEGORIES

DIRECTIONS = ("ltr", "rtl", "approach", "leave", "unknown")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT NOT NULL,
  start_ts REAL NOT NULL, trigger_ts REAL NOT NULL, end_ts REAL NOT NULL, last_seen_ts REAL,
  top_category TEXT NOT NULL,
  categories TEXT NOT NULL DEFAULT '{}', labels TEXT NOT NULL DEFAULT '{}',
  max_score REAL NOT NULL DEFAULT 0,
  snapshot TEXT, thumb TEXT, clip TEXT,
  clip_status TEXT NOT NULL DEFAULT 'pending',
  snap_version INTEGER NOT NULL DEFAULT 0,
  night INTEGER NOT NULL DEFAULT 0,
  clip_night TEXT, clip_night_status TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_ts);
CREATE INDEX IF NOT EXISTS idx_events_day ON events(day);
CREATE TABLE IF NOT EXISTS tracks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER,
  track_id INTEGER,
  day TEXT NOT NULL,
  category TEXT NOT NULL,
  label TEXT NOT NULL,
  first_ts REAL, counted_ts REAL NOT NULL, last_ts REAL,
  max_score REAL,
  direction TEXT NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_tracks_counted ON tracks(counted_ts);
CREATE INDEX IF NOT EXISTS idx_tracks_event ON tracks(event_id);
CREATE INDEX IF NOT EXISTS idx_tracks_day ON tracks(day, category);
"""

_EVENT_FIELDS = {"start_ts", "trigger_ts", "end_ts", "last_seen_ts", "top_category", "categories", "labels",
                 "max_score", "snapshot", "thumb", "clip", "clip_status", "night", "clip_night", "clip_night_status",
                 "clip_wide", "clip_wide_status", "main_camera", "clip_lead", "clip_wide_lead", "clip_wide_keep"}
# Columns added after the first release, created on older databases at open.
_EVENT_MIGRATIONS = (("night", "INTEGER NOT NULL DEFAULT 0"), ("clip_night", "TEXT"), ("clip_night_status", "TEXT"),
                     ("clip_wide", "TEXT"), ("clip_wide_status", "TEXT"), ("main_camera", "TEXT"),
                     ("clip_lead", "REAL"), ("clip_wide_lead", "REAL"),
                     # 1 once the PTZ left its Home view (or was not main) during the event: its wide clip is kept
                     ("clip_wide_keep", "INTEGER NOT NULL DEFAULT 0"))
_TRACK_MIGRATIONS = (("camera", "TEXT"),)  # which camera counted it: "ptz" or "eye"
_TRACK_FIELDS = {"last_ts", "max_score", "direction", "label"}


def local_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _ymd(day: str):
    y, m, d = (int(v) for v in day.split("-"))
    return y, m, d


def day_start(day: str) -> float:
    y, m, d = _ymd(day)
    return time.mktime((y, m, d, 0, 0, 0, 0, 0, -1))


def add_days(day: str, n: int) -> str:
    y, m, d = _ymd(day)
    return time.strftime("%Y-%m-%d", time.localtime(time.mktime((y, m, d + n, 12, 0, 0, 0, 0, -1))))


def _zero():
    return {c: 0 for c in CATEGORIES}


def event_json(row: dict) -> dict:
    def media(rel, version=0):
        if not rel:
            return None
        return f"/media/{rel}" + (f"?v={version}" if version else "")

    return {
        "id": row["id"],
        "day": row["day"],
        "start_ts": row["start_ts"], "trigger_ts": row["trigger_ts"], "end_ts": row["end_ts"],
        "last_seen_ts": row["last_seen_ts"],
        "top_category": row["top_category"],
        "categories": json.loads(row["categories"] or "{}"),
        "labels": json.loads(row["labels"] or "{}"),
        "max_score": round(row["max_score"] or 0.0, 3),
        "snapshot_url": media(row["snapshot"], row["snap_version"]),
        "thumb_url": media(row["thumb"], row["snap_version"]),
        "clip_url": media(row["clip"]) if row["clip_status"] == "ready" else None,
        "clip_status": row["clip_status"],
        "night": bool(row["night"]),
        "clip_night_url": media(row["clip_night"]) if row["clip_night_status"] == "ready" else None,
        "clip_night_status": row["clip_night_status"],
        "clip_wide_url": media(row["clip_wide"]) if row["clip_wide_status"] == "ready" else None,
        "clip_wide_status": row["clip_wide_status"],
        "main_camera": row["main_camera"] or "ptz",
        # Seconds each camera clip starts before the event window (clips start on whole 2 s segments; the night
        # clip starts on it): switching versions keeps the same moment with these.
        "clip_lead": row["clip_lead"] or 0.0, "clip_wide_lead": row["clip_wide_lead"] or 0.0,
        "duration": round(row["end_ts"] - row["start_ts"], 1),
    }


class Storage:
    def __init__(self, path):
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)
            have = {r[1] for r in self._db.execute("PRAGMA table_info(events)")}
            for col, decl in _EVENT_MIGRATIONS:
                if col not in have:
                    self._db.execute(f"ALTER TABLE events ADD COLUMN {col} {decl}")
            have = {r[1] for r in self._db.execute("PRAGMA table_info(tracks)")}
            for col, decl in _TRACK_MIGRATIONS:
                if col not in have:
                    self._db.execute(f"ALTER TABLE tracks ADD COLUMN {col} {decl}")

    def close(self):
        with self._lock:
            self._db.close()

    def _all(self, sql, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _one(self, sql, args=()):
        with self._lock:
            r = self._db.execute(sql, args).fetchone()
            return dict(r) if r else None

    def _exec(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args)

    # ---- events ----
    def insert_event(self, start_ts, trigger_ts, end_ts, top_category, categories, labels, max_score) -> int:
        cur = self._exec(
            "INSERT INTO events(day, start_ts, trigger_ts, end_ts, last_seen_ts, top_category, categories, labels,"
            " max_score) VALUES (?,?,?,?,?,?,?,?,?)",
            (local_day(trigger_ts), start_ts, trigger_ts, end_ts, trigger_ts, top_category,
             json.dumps(categories), json.dumps(labels), max_score))
        return cur.lastrowid

    def update_event(self, eid: int, **fields):
        bad = set(fields) - _EVENT_FIELDS
        if bad:
            raise ValueError(f"unknown event fields: {bad}")
        if not fields:
            return
        sets, args = [], []
        for k, v in fields.items():
            sets.append(f"{k}=?")
            args.append(json.dumps(v) if k in ("categories", "labels") else v)
        self._exec(f"UPDATE events SET {', '.join(sets)} WHERE id=?", (*args, eid))

    def bump_snapshot(self, eid: int):
        self._exec("UPDATE events SET snap_version = snap_version + 1 WHERE id=?", (eid,))

    def get_event(self, eid: int):
        return self._one("SELECT * FROM events WHERE id=?", (eid,))

    def event_objects(self, eid: int) -> list[dict]:
        return self._all(
            "SELECT track_id, category, label, first_ts, counted_ts, last_ts, max_score, direction"
            " FROM tracks WHERE event_id=? ORDER BY counted_ts", (eid,))

    def list_events(self, day=None, category=None, before=None, limit=50):
        where, args = [], []
        if day:
            where.append("day=?")
            args.append(day)
        if category:
            where.append("json_extract(categories, '$.' || ?) IS NOT NULL")
            args.append(category)
        if before is not None:
            where.append("start_ts < ?")
            args.append(before)
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "")
        rows = self._all(sql + " ORDER BY start_ts DESC LIMIT ?", (*args, limit + 1))
        return rows[:limit], len(rows) > limit

    def delete_event(self, eid: int):
        """Delete an event but keep its counted objects (stats) by detaching them."""
        with self._lock:
            row = self.get_event(eid)
            if row is None:
                return None
            self._db.execute("BEGIN")
            try:
                self._db.execute("UPDATE tracks SET event_id=NULL WHERE event_id=?", (eid,))
                self._db.execute("DELETE FROM events WHERE id=?", (eid,))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            return row

    def pending_clips(self) -> list[dict]:
        return self._all("SELECT * FROM events WHERE clip_status='pending' ORDER BY start_ts")

    def pending_wide_clips(self) -> list[dict]:
        return self._all("SELECT * FROM events WHERE clip_wide_status='pending' ORDER BY start_ts")

    def fail_pending_night_clips(self):
        """Night clips are built from frames held in RAM, so a restart loses any that were pending."""
        self._exec("UPDATE events SET clip_night_status='failed' WHERE clip_night_status='pending'")

    def events_before(self, ts: float, limit: int = 500) -> list[dict]:
        return self._all("SELECT * FROM events WHERE end_ts < ? ORDER BY start_ts LIMIT ?", (ts, limit))

    def oldest_events(self, limit: int = 20) -> list[dict]:
        return self._all("SELECT * FROM events ORDER BY start_ts LIMIT ?", (limit,))

    def oldest_start(self) -> float | None:
        """When the oldest stored event began: how far back the recordings go."""
        row = self._one("SELECT MIN(start_ts) AS t FROM events")
        return row["t"] if row else None

    # ---- tracks ----
    def insert_track(self, event_id, track_id, category, label, first_ts, counted_ts, last_ts, max_score,
                     direction, camera=None) -> int:
        cur = self._exec(
            "INSERT INTO tracks(event_id, track_id, day, category, label, first_ts, counted_ts, last_ts, max_score,"
            " direction, camera) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, track_id, local_day(counted_ts), category, label, first_ts, counted_ts, last_ts,
             max_score, direction, camera))
        return cur.lastrowid

    def update_track(self, tid: int, **fields):
        bad = set(fields) - _TRACK_FIELDS
        if bad:
            raise ValueError(f"unknown track fields: {bad}")
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self._exec(f"UPDATE tracks SET {sets} WHERE id=?", (*fields.values(), tid))

    def purge_tracks_before(self, ts: float):
        self._exec("DELETE FROM tracks WHERE counted_ts < ?", (ts,))

    # ---- counts & stats ----
    def counts_between(self, t0: float, t1: float) -> dict:
        out = _zero()
        for r in self._all("SELECT category, COUNT(*) AS n FROM tracks WHERE counted_ts >= ? AND counted_ts < ?"
                           " GROUP BY category", (t0, t1)):
            if r["category"] in out:
                out[r["category"]] = r["n"]
        return out

    def counts_today(self) -> dict:
        today = local_day(time.time())
        return self.counts_between(day_start(today), day_start(add_days(today, 1)))

    def days(self, limit: int = 60) -> list[dict]:
        events = {r["day"]: r["n"] for r in self._all("SELECT day, COUNT(*) AS n FROM events GROUP BY day")}
        counts: dict[str, dict] = {}
        for r in self._all("SELECT day, category, COUNT(*) AS n FROM tracks GROUP BY day, category"):
            if r["category"] in CATEGORIES:
                counts.setdefault(r["day"], _zero())[r["category"]] = r["n"]
        days = sorted(set(events) | set(counts), reverse=True)[:limit]
        return [{"day": d, "events": events.get(d, 0), "counts": counts.get(d, _zero())} for d in days]

    def stats(self, range_: str = "day", day: str | None = None, category: str | None = None) -> dict:
        day = day or local_day(time.time())
        ndays = {"day": 1, "week": 7, "month": 30}[range_]
        first = add_days(day, -(ndays - 1))
        start, end = day_start(first), day_start(add_days(day, 1))
        if range_ == "day":
            y, m, d = _ymd(day)
            bounds = [time.mktime((y, m, d, h, 0, 0, 0, 0, -1)) for h in range(24)] + [end]
            labels = [f"{h:02d}:00" for h in range(24)]
            bucket = "hour"
        else:
            days = [add_days(first, i) for i in range(ndays)]
            bounds = [day_start(x) for x in days] + [end]
            labels = [time.strftime("%b ", time.localtime(b)) + str(time.localtime(b).tm_mday) for b in bounds[:-1]]
            bucket = "day"

        series = [{"ts": bounds[i], "label": labels[i], "counts": _zero()} for i in range(len(labels))]
        totals = _zero()
        directions = {c: {d: 0 for d in DIRECTIONS} for c in CATEGORIES}
        heatmap = [[0] * 24 for _ in range(7)]
        by_hour = [0] * 24
        rows = self._all("SELECT counted_ts, category, direction FROM tracks WHERE counted_ts >= ? AND counted_ts < ?",
                         (start, end))
        for r in rows:
            c = r["category"]
            if c not in totals:
                continue
            i = bisect_right(bounds, r["counted_ts"]) - 1
            if 0 <= i < len(series):
                series[i]["counts"][c] += 1
            totals[c] += 1
            directions[c][r["direction"] if r["direction"] in DIRECTIONS else "unknown"] += 1
            if category is None or c == category:
                lt = time.localtime(r["counted_ts"])
                heatmap[lt.tm_wday][lt.tm_hour] += 1
                by_hour[lt.tm_hour] += 1
        peak = max(range(24), key=lambda h: by_hour[h])
        busiest = {"label": f"{peak:02d}:00", "count": by_hour[peak]} if by_hour[peak] else {"label": None, "count": 0}
        return {
            "range": range_, "day": day, "start_ts": start, "end_ts": end, "bucket": bucket,
            "categories": list(CATEGORIES), "totals": totals,
            "previous_totals": self.counts_between(day_start(add_days(first, -ndays)), start),
            "series": series, "directions": directions, "heatmap": heatmap, "busiest": busiest,
        }
