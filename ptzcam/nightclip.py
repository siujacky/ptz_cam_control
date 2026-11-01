"""Night-vision clips: the enhanced (Zero-DCE) view of an event, as an MP4 next to the camera's own clip.

While night vision is on, the pipeline pushes every enhanced frame here. A worker thread JPEG-encodes it
into a RAM ring that holds the pre-roll, plus whatever a pending event window still needs. Once a window
has closed, its frames go through x264 in one burst (two threads, lowered priority). The Pi 5 has no
hardware H.264 encoder, so this costs far less than encoding the enhanced stream continuously. veryfast at
CRF 28 makes a 30 s night clip of about 1.6 MB for about 9 s of CPU (ultrafast at CRF 26: 4.2 MB).
"""
from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import cv2

from .recorder import ClipJob

JPEG_QUALITY = 85
QUEUE_MAX = 30           # frames waiting for JPEG encoding; beyond this the newest are dropped
ACTIVE_WINDOW_S = 3.0    # night vision counts as on if a frame arrived this recently
FINISH_GRACE_S = 3.0     # build anyway this long after the window end (frames stopped: night ended)


class NightClipper:
    def __init__(self, cfg_store, log=None):
        self.cfg = cfg_store
        self.log = log or logging.getLogger("ptzcam.nightclip")
        self.dropped = 0
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._ring: deque = deque()  # (ts, jpeg bytes), oldest first
        self._cond = threading.Condition()
        self._jobs: list[ClipJob] = []
        self._building: ClipJob | None = None
        self._last_push = 0.0
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._encode_loop, name="night-jpeg", daemon=True),
                         threading.Thread(target=self._build_loop, name="night-clips", daemon=True)]

    # ---- lifecycle ----
    def start(self):
        for t in self._threads:
            t.start()

    def stop(self):
        """Stop the workers, then build pending windows from the frames already held."""
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        for t in self._threads:
            if t.is_alive():
                t.join(30)
        while True:  # frames still queued belong to the windows about to be built
            try:
                self._add(*self._q.get_nowait())
            except queue.Empty:
                break
        with self._cond:
            jobs, self._jobs = self._jobs, []
        for job in jobs:
            self._finish(job, self._safe_encode(job))

    # ---- input ----
    @property
    def active(self) -> bool:
        return time.time() - self._last_push < ACTIVE_WINDOW_S

    def push(self, ts: float, frame):
        """Pipeline thread; never blocks. The caller must not modify `frame` afterwards."""
        self._last_push = time.time()
        try:
            self._q.put_nowait((ts, frame))
        except queue.Full:
            self.dropped += 1

    def request_clip(self, event_id: int, t0: float, t1: float, out_path, callback):
        with self._cond:
            self._jobs.append(ClipJob(event_id, t0, t1, Path(out_path), callback))
            self._cond.notify_all()

    # ---- workers ----
    def _encode_loop(self):
        while not self._stop.is_set():
            try:
                ts, frame = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            self._add(ts, frame)

    def _add(self, ts, frame):
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if ok:
            keep = float(self.cfg.data["events"]["preroll_seconds"]) + 3.0
            with self._cond:
                self._ring.append((ts, buf.tobytes()))
                needed = [j.t0 for j in self._jobs] + ([self._building.t0] if self._building else [])
                keep_from = min([ts - keep] + needed)
                while self._ring and self._ring[0][0] < keep_from:
                    self._ring.popleft()
                self._cond.notify_all()

    def _ready(self, job: ClipJob) -> bool:
        newest = self._ring[-1][0] if self._ring else 0.0
        return newest >= job.t1 or time.time() > job.t1 + FINISH_GRACE_S

    def _frames(self, job: ClipJob) -> list:
        with self._cond:
            return [f for f in self._ring if job.t0 <= f[0] <= job.t1]

    def _build_loop(self):
        while not self._stop.is_set():
            with self._cond:
                ready = sorted((j for j in self._jobs if self._ready(j)), key=lambda j: j.t1)
                if not ready:
                    self._cond.wait(1.0)
                    continue
                job = ready[0]
                self._jobs.remove(job)
                self._building = job
            ok = self._safe_encode(job)
            with self._cond:
                self._building = None
            self._finish(job, ok)

    def _safe_encode(self, job: ClipJob) -> bool:
        try:
            return self._encode(job, self._frames(job))
        except Exception:
            self.log.exception("night clip failed for event %s", job.event_id)
            return False

    def _finish(self, job: ClipJob, ok: bool):
        try:
            job.callback(job.event_id, bool(ok))
        except Exception:
            self.log.exception("night clip callback failed for event %s", job.event_id)

    def _encode(self, job: ClipJob, frames: list) -> bool:
        fps = float(self.cfg.data["detect"]["fps"])
        if len(frames) < fps:  # under a second of night footage (night vision was off most of the window)
            return False
        job.out_path.parent.mkdir(parents=True, exist_ok=True)
        part = job.out_path.with_name(job.out_path.stem + ".part.mp4")
        # `nice` rather than preexec_fn, which can deadlock a forked child in a threaded process
        cmd = ["nice", "-n", "10", "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", f"{fps:g}", "-i", "pipe:0",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", "-threads", "2",
               "-movflags", "+faststart", str(part)]
        t_start = time.time()
        with open(os.devnull, "wb") as devnull:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=devnull, stderr=subprocess.PIPE)
        err = []
        drain = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
        drain.start()
        try:
            # Constant frame rate on the wall clock: repeat a frame across gaps, skip extras.
            i, t, step, end = 0, frames[0][0], 1.0 / fps, frames[-1][0] + 1e-6
            while t <= end:
                while i + 1 < len(frames) and frames[i + 1][0] <= t + 1e-6:
                    i += 1
                p.stdin.write(frames[i][1])
                t += step
            p.stdin.close()
            rc = p.wait(timeout=120)
        except (BrokenPipeError, subprocess.TimeoutExpired):
            p.kill()
            p.wait()
            rc = -1
        drain.join(5)
        p.stderr.close()
        if rc != 0 or not part.exists() or part.stat().st_size < 2_000:  # an MP4 with at least a frame
            msg = (err[0] if err else b"").decode(errors="replace").strip()[-300:]
            self.log.warning("night clip for event %s failed: %s", job.event_id, msg or f"ffmpeg exit {rc}")
            part.unlink(missing_ok=True)
            return False
        os.replace(part, job.out_path)
        self.log.info("night clip ready for event %s (%.1f s, %d frames, encoded in %.1f s)", job.event_id,
                      frames[-1][0] - frames[0][0], len(frames), time.time() - t_start)
        return True
