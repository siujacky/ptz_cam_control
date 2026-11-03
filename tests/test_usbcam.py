import io
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ptzcam.camera import FrameSource, SourceUnavailable
from ptzcam.recorder import segment_output_args
from ptzcam.usbcam import UsbCapture


class ExitedProc:
    """An ffmpeg that exited without sending a frame."""
    returncode = 1
    pid = 0

    def __init__(self):
        self.stdout = io.BytesIO()

    def poll(self):
        return self.returncode


class StreamingProc:
    """An ffmpeg that sends frames until SIGTERM, then `tail` more, closes the pipe and exits a little later
    (as ffmpeg still closes the ring's segment after pipe:1)."""
    pid = 0

    def __init__(self, tail=0):
        self.returncode, self.signals, self.tail = None, [], tail
        self.stdout = self
        self._term, self._exited = threading.Event(), threading.Event()

    def readinto(self, view):
        time.sleep(0.005)
        if self._term.is_set():
            if self.tail <= 0:
                threading.Timer(0.05, self._exit, (255,)).start()  # 255: ffmpeg's code after SIGTERM
                return 0
            self.tail -= 1
        view[:] = bytes(len(view))
        return len(view)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.signals.append("TERM")
        self._term.set()

    def kill(self):
        self.signals.append("KILL")
        self.tail = 0
        self._term.set()
        self._exit(-9)

    def _exit(self, code):
        if self.returncode is None:
            self.returncode = code
        self._exited.set()

    def wait(self, timeout=None):
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return self.returncode


def capture(ring, **kw):
    return UsbCapture("Avaya HC020", 1920, 1080, 15, 1280, 720, ring, **kw)


def wait_until(cond, timeout=5.0):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.02)
    return cond()


class FrameSourceNameTests(unittest.TestCase):
    def test_name_tells_two_sources_apart(self):
        ptz = FrameSource("rtsp://camera", 1280, 720, 15)
        self.assertEqual((ptz.log.name, ptz._thread.name, ptz._watchdog.name, ptz._tag),
                         ("ptzcam.camera", "frame-source", "frame-watchdog", "decoder"))
        with tempfile.TemporaryDirectory() as d:
            eye = capture(Path(d))
        self.assertEqual((eye.log.name, eye._thread.name, eye._watchdog.name, eye._tag, eye.ring.log.name),
                         ("ptzcam.camera.eye", "eye-source", "eye-watchdog", "eye", "ptzcam.recorder.eye"))


class StopTests(unittest.TestCase):
    def test_ffmpeg_closes_its_files_after_one_sigterm(self):
        # stop() and the reader both call terminate(); ffmpeg takes a second SIGTERM as "exit now".
        src, proc = FrameSource("rtsp://camera", 4, 2, 15), StreamingProc(tail=5)
        with mock.patch("ptzcam.camera.spawn", return_value=proc):
            src.start()
            self.assertTrue(wait_until(lambda: src._frame_id > 0))
            src.stop()
        self.assertEqual((proc.signals, proc.returncode, proc.tail), (["TERM"], 255, 0))  # the pipe was drained
        self.assertFalse(src._thread.is_alive())

    def test_stop_while_ffmpeg_starts_stops_that_ffmpeg(self):
        src, procs = FrameSource("rtsp://camera", 4, 2, 15), []

        def spawn(cmd, log, name, stdout=None):
            src._stop.wait(5)  # stop() comes while this ffmpeg is starting
            time.sleep(0.05)
            procs.append(StreamingProc())
            return procs[-1]

        with mock.patch("ptzcam.camera.spawn", side_effect=spawn):
            src.start()
            time.sleep(0.1)
            src.stop()
        self.assertEqual([p.signals for p in procs], [["TERM"]])
        self.assertFalse(src._thread.is_alive())

    def test_stop_while_preparing_the_command_starts_nothing(self):
        src = FrameSource("rtsp://camera", 4, 2, 15)
        src._cmd = lambda: src._stop.wait(5) and ["ffmpeg"]  # e.g. the eye looking for its camera
        with mock.patch("ptzcam.camera.spawn") as spawn:
            src.start()
            time.sleep(0.1)
            src.stop()
        spawn.assert_not_called()
        self.assertFalse(src._thread.is_alive())


class CommandTests(unittest.TestCase):
    def test_one_ffmpeg_writes_the_ring_and_decodes(self):
        with tempfile.TemporaryDirectory() as d:
            ring = Path(d) / "run" / "eye"
            cap = capture(ring)
            with mock.patch("ptzcam.usbcam.uvc.find_device", return_value="/dev/video2") as find, \
                    mock.patch("ptzcam.usbcam.uvc.write_control") as write:
                before = int(time.time())
                cmd = cap._cmd()
            find.assert_called_once_with("Avaya HC020")
            write.assert_not_called()  # anti_flicker None: left as it is
            self.assertTrue(ring.is_dir())
            start_n = int(cmd[cmd.index("-segment_start_number") + 1])
            self.assertIn(start_n, (before, before + 1))
            self.assertEqual(cmd, [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-fflags", "+genpts+discardcorrupt",
                "-flags", "low_delay", "-thread_queue_size", "64",
                "-f", "v4l2", "-input_format", "h264", "-video_size", "1920x1080", "-framerate", "15",
                "-i", "/dev/video2",
                "-map", "0:v:0", "-c:v", "copy", *segment_output_args(ring, 2, start_n),
                "-map", "0:v:0", "-vf", "scale=1280:720:flags=bilinear", "-fps_mode", "passthrough", "-enc_time_base", "-1", "-pix_fmt", "bgr24", "-f", "rawvideo",
                "pipe:1"])
            self.assertEqual((cap.width, cap.height), (1280, 720))  # the size of the frames delivered

    def test_watchdog_also_restarts_when_the_ring_stops_growing(self):
        with tempfile.TemporaryDirectory() as d:
            cap = capture(Path(d))
            now = time.time()
            cap._proc_started, cap._frame_ts = now - 60, now  # running for a minute, frames still arriving
            seg = Path(d) / "seg_100.ts"
            seg.write_bytes(b"x")
            self.assertIsNone(cap._stalled())
            for age, expected in ((10, None), (20, "no new segment for 16 s")):
                os.utime(seg, (now - age, now - age))
                self.assertEqual(cap._stalled(), expected)
            cap._frame_ts = now - 20
            self.assertEqual(cap._stalled(), "no frames for 15 s")


class ConnectTests(unittest.TestCase):
    def test_anti_flicker_set_before_every_connect(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []

            def write_control(dev, name, value):
                calls.append(("write", dev, name, value))
                return 1, value

            def spawn(cmd, log, name, stdout=None):
                calls.append(("spawn", name))
                return ExitedProc()  # so the source reconnects after its back-off (1 s)

            cap = capture(Path(d), anti_flicker=0)
            with mock.patch("ptzcam.usbcam.uvc.find_device", return_value="/dev/video2"), \
                    mock.patch("ptzcam.usbcam.uvc.write_control", side_effect=write_control), \
                    mock.patch("ptzcam.camera.spawn", side_effect=spawn), \
                    self.assertLogs("ptzcam.camera.eye", level="INFO") as logs:
                cap.start()
                self.assertTrue(wait_until(lambda: len(calls) >= 4))
                cap.stop()
            self.assertEqual(calls, [("write", "/dev/video2", "power_line_frequency", 0), ("spawn", "eye")]
                             * (len(calls) // 2))
            self.assertIn("anti-flicker 1 -> 0", "\n".join(logs.output))
            for off in (None, -1):
                with mock.patch("ptzcam.usbcam.uvc.find_device", return_value="/dev/video2"), \
                        mock.patch("ptzcam.usbcam.uvc.write_control") as write:
                    capture(Path(d), anti_flicker=off)._cmd()
                write.assert_not_called()

    def test_the_sync_maps_crop_is_put_back_on_every_connect(self):
        with tempfile.TemporaryDirectory() as d:
            cap = capture(Path(d), anti_flicker=0)
            cap.crop = {"zoom_absolute": 120, "pan_absolute": 3600}  # set by the pipeline from sync.json
            with mock.patch("ptzcam.usbcam.uvc.find_device", return_value="/dev/video2"), \
                    mock.patch("ptzcam.usbcam.uvc.write_control", return_value=(100, 120)) as write:
                cap._cmd()
                cap._cmd()  # a reconnect (a re-plug resets UVC controls)
            names = [c.args[1] for c in write.call_args_list]
            self.assertEqual(names, ["power_line_frequency", "zoom_absolute", "pan_absolute"] * 2)

    def test_refused_anti_flicker_still_connects(self):
        with tempfile.TemporaryDirectory() as d:
            cap = capture(Path(d), anti_flicker=2)
            with mock.patch("ptzcam.usbcam.uvc.find_device", return_value="/dev/video2"), \
                    mock.patch("ptzcam.usbcam.uvc.write_control", side_effect=OSError(22, "Invalid argument")), \
                    self.assertLogs("ptzcam.camera.eye", level="WARNING") as logs:
                self.assertIn("/dev/video2", cap._cmd())
            self.assertIn("could not set anti-flicker to 2", logs.output[0])

    def test_missing_camera_fails_the_spawn_and_is_logged_once(self):
        with tempfile.TemporaryDirectory() as d:
            cap = capture(Path(d), anti_flicker=0)
            find = mock.Mock(side_effect=LookupError("no USB camera named 'Avaya HC020'; connected: Unite 200"))
            with mock.patch("ptzcam.usbcam.uvc.find_device", find), \
                    mock.patch("ptzcam.usbcam.uvc.write_control", return_value=(0, 0)) as write, \
                    mock.patch("ptzcam.camera.spawn") as spawn, \
                    self.assertLogs("ptzcam.camera.eye", level="INFO") as logs:
                cap.start()
                self.assertTrue(wait_until(lambda: find.call_count >= 1))
                time.sleep(0.2)
                self.assertTrue(cap._thread.is_alive())  # waiting out its back-off, not crashed
                self.assertFalse(cap.online or cap.ring.online)
                cap.stop()
                for _ in range(2):
                    with self.assertRaises(SourceUnavailable):
                        cap._cmd()
                spawn.assert_not_called()
                write.assert_not_called()
                find.side_effect, find.return_value = None, "/dev/video2"  # plugged in again
                self.assertIn("/dev/video2", cap._cmd())
                write.assert_called_once()
            warnings = [r.getMessage() for r in logs.records if r.levelname == "WARNING"]
            self.assertEqual(warnings, ["no USB camera named 'Avaya HC020'; connected: Unite 200; waiting for it"])
            self.assertIn("USB camera 'Avaya HC020' is /dev/video2", logs.output[-1])


if __name__ == "__main__":
    unittest.main()
