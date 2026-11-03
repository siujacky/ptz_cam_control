"""Rolling RAM buffer of stream-copied segments, and MP4 clip assembly for events.

ffmpeg copies the camera stream (no re-encode) into short MPEG-TS segments named seg_<n>.ts, where n
starts at the epoch second ffmpeg was started. A segment's end time is its mtime (its last write); its
start is the previous segment's end when the numbers are consecutive. Clips are cut from the segments
that overlap the event window, whole from the first one's keyframe, once the window has closed. A passive
Recorder (url None) runs no ffmpeg of its own: it indexes and cuts clips from a ring that another process
writes with segment_output_args().
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .camera import input_args
from .proc import spawn, terminate

SEG_RE = re.compile(r"^seg_(\d+)\.ts$")
GIVE_UP_AFTER = 30.0  # seconds past the window end before building with whatever footage exists


def segment_output_args(seg_dir, segment_seconds: int, start_n: int) -> list:
    """ffmpeg output options that write the ring: MPEG-TS segments of segment_seconds (cut at the next keyframe),
    numbered from start_n. The caller maps the streams before these. The delta lets a keyframe that comes a
    little early still start a segment: the eye camera's frame times jitter at start-up, and its first segment
    would otherwise hold two GOPs (4 s), which the index (start = end - segment_seconds) puts 2 s late."""
    return ["-f", "segment", "-segment_time", str(segment_seconds), "-segment_time_delta", str(segment_seconds / 4),
            "-segment_format", "mpegts", "-reset_timestamps", "1", "-segment_start_number", str(start_n),
            str(Path(seg_dir) / "seg_%d.ts")]


def next_start_number(seg_dir) -> int:
    """First segment number for a new writer: the epoch second, or one past the newest segment if that is
    higher, so the numbers keep rising across restarts."""
    top = 0
    try:
        with os.scandir(seg_dir) as it:
            for e in it:
                m = SEG_RE.match(e.name)
                if m:
                    top = max(top, int(m.group(1)))
    except FileNotFoundError:
        pass
    return max(int(time.time()), top + 1)


@dataclass
class Segment:
    n: int
    path: str
    start: float
    end: float


@dataclass
class ClipJob:
    event_id: int
    t0: float
    t1: float
    out_path: Path
    callback: object  # callback(event_id, ok: bool, lead: float)
    lead: float = 0.0  # seconds the clip starts before t0 (it starts on a whole segment)


class Recorder:
    def __init__(self, url: str | None, seg_dir, segment_seconds: int = 2, buffer_seconds: int = 120, log=None,
                 writer_alive=None):
        """url None makes the ring passive: another process writes seg_dir, and writer_alive() tells whether
        it is running (the newest segment is then still being written)."""
        self.url = url
        self.seg_dir = Path(seg_dir)
        self.segment_seconds = segment_seconds
        self.buffer_seconds_cfg = buffer_seconds
        self.log = log or logging.getLogger("ptzcam.recorder")
        self._proc = None
        self._writer_alive = writer_alive or self._proc_alive
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._jobs: list[ClipJob] = []
        self._building: ClipJob | None = None
        self._threads = [
            threading.Thread(target=self._clip_worker, name="clip-builder", daemon=True),
            threading.Thread(target=self._janitor, name="segment-janitor", daemon=True),
        ]
        if url is not None:  # a passive ring has no ffmpeg of its own
            self._threads.insert(0, threading.Thread(target=self._supervise, name="recorder", daemon=True))

    # ---- lifecycle ----
    def start(self, recover: list[ClipJob] | None = None):
        self.seg_dir.mkdir(parents=True, exist_ok=True)
        for job in recover or []:  # clips left pending by a previous run; the RAM buffer survives restarts
            self._finish(job, self._safe_build(job))
        for p in self.seg_dir.glob("seg_*.ts"):
            p.unlink(missing_ok=True)
        for t in self._threads:
            t.start()

    def stop(self):
        self._stop.set()
        terminate(self._proc)  # ffmpeg closes the current segment cleanly on SIGTERM
        with self._cond:
            jobs, self._jobs = self._jobs, []
            self._cond.notify_all()
        for t in self._threads:
            if t.is_alive():
                t.join(30)
        for job in jobs:  # build what we have rather than lose the clip
            self._finish(job, self._safe_build(job))

    # ---- status ----
    @property
    def online(self) -> bool:
        if not self._writer_alive():
            return False
        newest = self._newest_mtime()
        return newest is not None and time.time() - newest < 3 * self.segment_seconds + 2

    def buffer_seconds(self) -> float:
        segs = self.segments()
        return segs[-1].end - segs[0].start if segs else 0.0

    # ---- segments ----
    def segments(self, include_partial: bool = False) -> list[Segment]:
        entries = []
        try:
            with os.scandir(self.seg_dir) as it:
                for e in it:
                    m = SEG_RE.match(e.name)
                    if m:
                        try:
                            entries.append((int(m.group(1)), e.path, e.stat().st_mtime))
                        except FileNotFoundError:
                            pass
        except FileNotFoundError:
            return []
        entries.sort()
        segs, prev = [], None
        for n, path, mtime in entries:
            start = prev[1] if prev is not None and prev[0] == n - 1 else mtime - self.segment_seconds
            segs.append(Segment(n, path, start, mtime))
            prev = (n, mtime)
        if segs and not include_partial and self._writer_alive():
            segs.pop()  # the newest segment is still being written
        return segs

    def _proc_alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def _newest_mtime(self):
        segs = self.segments(include_partial=True)
        return segs[-1].end if segs else None

    def _cmd(self, start_n: int) -> list:
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-fflags", "+genpts+discardcorrupt",
                *input_args(self.url), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                *segment_output_args(self.seg_dir, self.segment_seconds, start_n)]

    def _supervise(self):
        backoff = 1.0
        while not self._stop.is_set():
            started = time.time()
            try:
                # Re-create the buffer dir every time: logind's RemoveIPC wipes a user's /dev/shm files
                # when that user's last login session ends.
                self.seg_dir.mkdir(parents=True, exist_ok=True)
                self._proc = spawn(self._cmd(next_start_number(self.seg_dir)), self.log, "recorder")
                while self._proc.poll() is None and not self._stop.wait(1.0):
                    ref = max(self._newest_mtime() or 0.0, started)
                    if time.time() - ref > 3 * self.segment_seconds + 10:
                        self.log.warning("recorder stalled; restarting")
                        self._proc.kill()
            except Exception:
                self.log.exception("recorder failed")
            finally:
                terminate(self._proc)
            if self._stop.is_set():
                break
            if time.time() - started > 30:
                backoff = 1.0
            self.log.warning("recorder stopped; restarting in %.0f s", backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    def _janitor(self):
        while not self._stop.wait(5.0):
            try:
                self._prune()
            except Exception:
                self.log.exception("segment prune failed")

    def _prune(self):
        cutoff = time.time() - self.buffer_seconds_cfg
        with self._cond:
            needed = [j.t0 for j in self._jobs] + ([self._building.t0] if self._building else [])
        keep_from = min(needed) - self.segment_seconds if needed else None
        for s in self.segments():
            if s.end >= cutoff or (keep_from is not None and s.end >= keep_from):
                break
            try:
                os.unlink(s.path)
            except FileNotFoundError:
                pass

    # ---- clips ----
    def request_clip(self, event_id: int, t0: float, t1: float, out_path, callback):
        with self._cond:
            self._jobs.append(ClipJob(event_id, t0, t1, Path(out_path), callback))
            self._cond.notify_all()

    def _clip_worker(self):
        while not self._stop.is_set():
            with self._cond:
                if not self._jobs:
                    self._cond.wait(1.0)
                    continue
                job = min(self._jobs, key=lambda j: j.t1)
                wait = job.t1 + self.segment_seconds + 1.0 - time.time()
                if wait > 0:
                    self._cond.wait(min(wait, 1.0))
                    continue
                self._jobs.remove(job)
                self._building = job
            try:
                ok = self._build(job)
            except Exception:
                self.log.exception("clip build failed for event %s", job.event_id)
                ok = False
            with self._cond:
                self._building = None
                if ok is None and not self._stop.is_set():
                    self._jobs.append(job)  # footage not complete yet; retry shortly
                    self._cond.wait(1.0)
                    continue
            if ok is None:  # stopping, and stop() has already taken the queue: build what we have here
                ok = self._safe_build(job)
            self._finish(job, ok)

    def _finish(self, job: ClipJob, ok):
        try:
            job.callback(job.event_id, bool(ok), job.lead if ok else 0.0)
        except Exception:
            self.log.exception("clip callback failed for event %s", job.event_id)

    def _safe_build(self, job: ClipJob) -> bool:
        try:
            return bool(self._build(job, force=True))
        except Exception:
            self.log.exception("clip build failed for event %s", job.event_id)
            return False

    def _build(self, job: ClipJob, force: bool = False):
        """Cut the clip. Returns True/False, or None if footage is still arriving (retry later)."""
        late = force or time.time() > job.t1 + GIVE_UP_AFTER
        segs = [s for s in self.segments(include_partial=force) if s.end > job.t0 and s.start < job.t1]
        if not segs:
            return False if late else None
        if segs[-1].end < job.t1 - 0.5 and not late:
            return None
        end = min(job.t1, segs[-1].end)
        if end - max(job.t0, segs[0].start) < 1.0:
            return False
        # The clip starts with the first segment, at its keyframe, so it may begin up to one segment before t0.
        # Seeking into an MPEG-TS segment instead lands on any frame, and a stream copy then drops everything up
        # to the next keyframe: up to a whole GOP of the window (2 s on the eye camera).
        duration = end - segs[0].start
        job.lead = max(0.0, job.t0 - segs[0].start)  # the viewer lines versions of one event up with it
        job.out_path.parent.mkdir(parents=True, exist_ok=True)
        # one listing per output: two rings cut clip.mp4 and clip_wide.mp4 into one event folder at the same moment
        listing = job.out_path.with_name("." + job.out_path.stem + ".concat.txt")
        part = job.out_path.with_name(job.out_path.stem + ".part.mp4")
        listing.write_text("".join(f"file '{s.path}'\n" for s in segs))
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-f", "concat", "-safe", "0", "-i", str(listing),
               "-t", f"{duration:.3f}", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
               "-movflags", "+faststart", str(part)]
        try:
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=120)
            if r.returncode != 0 or not part.exists() or part.stat().st_size < 10_000:
                self.log.warning("clip for event %s failed: %s", job.event_id,
                                 r.stderr.decode(errors="replace").strip()[-300:])
                part.unlink(missing_ok=True)
                return False
            os.replace(part, job.out_path)
            self.log.info("clip ready for event %s (%.1f s from %d segments)", job.event_id, duration, len(segs))
            return True
        except subprocess.TimeoutExpired:
            part.unlink(missing_ok=True)
            return False
        finally:
            listing.unlink(missing_ok=True)
