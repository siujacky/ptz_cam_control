"""The eye: a fixed USB (UVC) camera that sends H.264, read by one ffmpeg that also records it.

Only one process may stream a V4L2 device, so the same ffmpeg writes the stream-copied segment ring for event
clips (see recorder.py) and decodes BGR frames for detection, as FrameSource does for the PTZ camera's stream.
"""
from __future__ import annotations

import logging
import os
import time

from . import uvc
from .camera import FrameSource, SourceUnavailable
from .recorder import Recorder, next_start_number, segment_output_args


class UsbCapture(FrameSource):
    def __init__(self, device: str, width: int, height: int, fps: int, out_width: int, out_height: int, ring_dir,
                 segment_seconds: int = 2, buffer_seconds: int = 120, anti_flicker=None, log=None, name: str = "eye"):
        """device: a camera name such as "Avaya HC020", or a path such as /dev/v4l/by-id/... The camera sends
        width x height H.264 at fps; frames are delivered at out_width x out_height. anti_flicker (None or -1 =
        leave it, 0 off, 1 50 Hz, 2 60 Hz) is set on every (re)connect, because a re-plug resets it."""
        super().__init__(device, out_width, out_height, fps, log=log, name=name)
        self.device, self.capture_size, self.anti_flicker = device, (width, height), anti_flicker
        self.node = None  # the device node in use
        self._missing = False
        self.ring = Recorder(None, ring_dir, segment_seconds, buffer_seconds,
                             log=logging.getLogger("ptzcam.recorder" + (f".{name}" if name else "")),
                             writer_alive=self._writing)

    def start(self, recover=None):
        self.ring.start(recover)  # builds the clips a previous run left pending, then empties the ring
        super().start()

    def stop(self):
        try:
            super().stop()  # ffmpeg closes the current segment on SIGTERM
        finally:
            self.ring.stop()  # and the clips still pending are built from what is buffered

    def _writing(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def _cmd(self) -> list:
        dev = self._find()
        self._set_anti_flicker(dev)
        ring = self.ring.seg_dir
        ring.mkdir(parents=True, exist_ok=True)  # re-created every time, as Recorder._supervise does
        w, h = self.capture_size
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-fflags", "+genpts+discardcorrupt",
                "-flags", "low_delay", "-thread_queue_size", "64",
                "-f", "v4l2", "-input_format", "h264", "-video_size", f"{w}x{h}", "-framerate", str(self.target_fps),
                "-i", dev,
                "-map", "0:v:0", "-c:v", "copy",
                *segment_output_args(ring, self.ring.segment_seconds, next_start_number(ring)),
                "-map", "0:v:0", "-vf", f"scale={self.width}:{self.height}:flags=bilinear",
                "-fps_mode", "passthrough",  # after a stall, no burst of repeated frames with fresh times
                "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]

    def _find(self) -> str:
        try:
            dev = uvc.find_device(self.device)
        except LookupError as e:
            if not self._missing:
                self.log.warning("%s; waiting for it", e)
                self._missing = True
            raise SourceUnavailable(str(e)) from None
        if self._missing or dev != self.node:
            self.log.info("USB camera %r is %s", self.device, os.path.realpath(dev))
        self._missing, self.node = False, dev
        return dev

    def _set_anti_flicker(self, dev: str):
        if self.anti_flicker is None or self.anti_flicker < 0:
            return
        try:
            old, new = uvc.write_control(dev, "power_line_frequency", self.anti_flicker)
        except (OSError, LookupError) as e:
            self.log.warning("could not set anti-flicker to %s: %s", self.anti_flicker, e)
            return
        if old != new:
            self.log.info("anti-flicker %s -> %s", old, new)

    def _stalled(self):
        """As FrameSource's, and also when frames still arrive but the ring has stopped growing."""
        stalled = super()._stalled()
        if stalled:
            return stalled
        limit = 3 * self.ring.segment_seconds + 10
        segs = self.ring.segments(include_partial=True)
        if time.time() - max(segs[-1].end if segs else 0.0, self._proc_started) > limit:
            return f"no new segment for {limit} s"
        return None
