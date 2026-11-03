"""Decodes the camera stream with ffmpeg into BGR frames, keeping only the newest one."""
from __future__ import annotations

import fcntl
import logging
import re
import subprocess
import threading
import time

import numpy as np

from .proc import spawn, terminate

STALL_SECONDS = 15.0


class SourceUnavailable(Exception):
    """Raised by _cmd() when the input is not there (e.g. a USB camera unplugged): nothing is started, and the
    next try follows the usual back-off."""


def is_file_source(url: str) -> bool:
    return not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url)


def input_args(url: str) -> list:
    """ffmpeg input options: RTSP over TCP for the camera, real-time looping for test files."""
    if is_file_source(url):
        return ["-re", "-stream_loop", "-1", "-i", url]
    return ["-rtsp_transport", "tcp", "-timeout", "5000000", "-i", url]


def _grow_pipe(pipe, size=1 << 20):
    """A frame is 2.76 MB: a 64 KiB pipe needs 43 reads for it, and while the reader lags, ffmpeg (which for the
    eye camera also writes the recording ring) waits. 1 MiB is the kernel's default limit for unprivileged pipes."""
    try:
        fcntl.fcntl(pipe.fileno(), 1031, size)  # F_SETPIPE_SZ
    except Exception:  # not a real pipe (tests), or the kernel refuses: the default size works too
        pass


class FrameSource:
    def __init__(self, url: str, width: int, height: int, fps: int, log=None, name: str | None = None):
        """`name` (e.g. "eye") tells two sources apart in the journal: it names the logger child, the threads
        and ffmpeg's stderr lines."""
        self.url, self.width, self.height, self.target_fps = url, width, height, fps
        log = log or logging.getLogger("ptzcam.camera")
        self.log = log.getChild(name) if name else log
        self._tag = name or "decoder"
        self.fps = 0.0
        self._cond = threading.Condition()
        self._frame = None
        self._frame_id = 0
        self._frame_ts = 0.0
        self._proc = None
        self._proc_started = 0.0
        self._stop = threading.Event()
        self._spawn_lock = threading.Lock()  # so stop() cannot miss an ffmpeg that is being started
        self._thread = threading.Thread(target=self._run, name=f"{name or 'frame'}-source", daemon=True)
        self._watchdog = threading.Thread(target=self._watch, name=f"{name or 'frame'}-watchdog", daemon=True)

    def start(self):
        self._thread.start()
        self._watchdog.start()

    def stop(self):
        self._stop.set()
        with self._spawn_lock:  # an ffmpeg being started now is the one stopped; no other starts after this
            proc = self._proc
        terminate(proc)
        with self._cond:
            self._cond.notify_all()
        if self._thread.is_alive():
            self._thread.join(5)

    @property
    def online(self) -> bool:
        return self._frame_ts > 0 and time.time() - self._frame_ts < 5.0

    @property
    def last_frame_age(self):
        return round(time.time() - self._frame_ts, 2) if self._frame_ts else None

    def latest(self):
        with self._cond:
            if self._frame is None:
                return None
            return self._frame_id, self._frame_ts, self._frame

    def wait_frame(self, last_id: int, timeout: float = 1.0):
        """Block until a frame newer than `last_id` arrives. Returns (id, ts, frame) or None."""
        with self._cond:
            self._cond.wait_for(lambda: self._frame_id != last_id or self._stop.is_set(), timeout)
            if self._frame is None or self._frame_id == last_id:
                return None
            return self._frame_id, self._frame_ts, self._frame

    def _cmd(self) -> list:
        vf = f"fps={self.target_fps},scale={self.width}:{self.height}:flags=bilinear"
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-threads", "3",
                "-fflags", "+discardcorrupt", *input_args(self.url),
                "-an", "-sn", "-dn", "-vf", vf, "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]

    def _run(self):
        backoff = 1.0
        size = self.width * self.height * 3
        while not self._stop.is_set():
            started = time.time()
            missing = False
            try:
                self._proc_started = started
                cmd = self._cmd()
                with self._spawn_lock:
                    if self._stop.is_set():  # stop() came while _cmd() ran
                        break
                    self._proc = spawn(cmd, self.log, self._tag, stdout=subprocess.PIPE)
                _grow_pipe(self._proc.stdout)
                self._read_frames(self._proc.stdout, size)
            except SourceUnavailable:
                missing = True  # _cmd() has said why, once
            except Exception:
                self.log.exception("frame source failed")
            finally:
                terminate(self._proc)
            if self._stop.is_set():
                break
            if time.time() - started > 30:
                backoff = 1.0
            if not missing:
                self.log.warning("camera stream ended; reconnecting in %.0f s", backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    def _read_frames(self, pipe, size: int):
        last = None
        while True:  # until EOF, also when stopping: ffmpeg must not block on a full pipe while it closes its files
            buf = bytearray(size)  # fresh buffer per frame, so consumers may keep a reference
            view = memoryview(buf)
            got = 0
            while got < size:
                n = pipe.readinto(view[got:])
                if not n:
                    return
                got += n
            ts = time.time()
            frame = np.frombuffer(buf, dtype=np.uint8).reshape(self.height, self.width, 3)
            with self._cond:
                self._frame, self._frame_ts = frame, ts
                self._frame_id += 1
                self._cond.notify_all()
            if last is not None:
                inst = 1.0 / max(1e-3, ts - last)
                self.fps = inst if self.fps == 0 else 0.9 * self.fps + 0.1 * inst
            last = ts

    def _watch(self):
        """Kill ffmpeg if it is alive but has stopped delivering frames, forcing a reconnect."""
        while not self._stop.wait(2.0):
            proc = self._proc
            if proc is None or proc.poll() is not None:
                continue
            stalled = self._stalled()
            if stalled:
                self.log.warning("%s; restarting decoder", stalled)
                self.fps = 0.0
                proc.kill()

    def _stalled(self):
        """Why the running ffmpeg counts as stuck, or None."""
        if time.time() - max(self._frame_ts, self._proc_started) > STALL_SECONDS:
            return f"no frames for {STALL_SECONDS:.0f} s"
        return None
