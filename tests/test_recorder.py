import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from ptzcam.recorder import ClipJob, Recorder, next_start_number, segment_output_args


def _touch(path: Path, mtime: float):
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))


def _write_ring(seg_dir: Path, size: str, start_n: int, base: float, seconds: int = 8, gop: int = 15) -> list:
    """15 fps H.264 (a keyframe every `gop` frames) cut into the ring as the camera writers do; segment k then
    ends at base + 2 (k + 1)."""
    src = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate=15", "-t", str(seconds), "-c:v", "libx264",
           "-g", str(gop), "-pix_fmt", "yuv420p"]
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *src, *segment_output_args(seg_dir, 2, start_n)],
                   check=True)
    segs = sorted(seg_dir.glob("seg_*.ts"), key=lambda p: int(p.stem[4:]))
    for k, p in enumerate(segs):
        os.utime(p, (base + 2 * (k + 1), base + 2 * (k + 1)))
    return segs


def _probe(path, entries: str, *extra) -> str:
    return subprocess.run(["ffprobe", "-v", "error", *extra, "-select_streams", "v:0", "-show_entries", entries,
                           "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout.strip()


class SegmentIndexTests(unittest.TestCase):
    def test_times_from_mtimes(self):
        with tempfile.TemporaryDirectory() as d:
            r = Recorder("rtsp://camera", d, segment_seconds=2)
            base = 1_800_000_000.0
            for i, n in enumerate([100, 101, 102, 104]):  # 103 missing: a gap (ffmpeg restarted)
                _touch(Path(d) / f"seg_{n}.ts", base + 2 * (i + 1))
            segs = r.segments()  # ffmpeg not running, so every segment counts as complete
            self.assertEqual([s.n for s in segs], [100, 101, 102, 104])
            self.assertAlmostEqual(segs[1].start, segs[0].end)
            self.assertAlmostEqual(segs[2].start, segs[1].end)
            self.assertAlmostEqual(segs[3].start, segs[3].end - 2)
            self.assertAlmostEqual(r.buffer_seconds(), segs[3].end - segs[0].start)


class ClipBuildTests(unittest.TestCase):
    def test_build_cuts_window_from_real_segments(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            # 8 s of H.264 + AAC with a keyframe every second, split into 2 s TS segments.
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
                 "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "8",
                 "-c:v", "libx264", "-g", "30", "-pix_fmt", "yuv420p", "-c:a", "aac",
                 "-f", "segment", "-segment_time", "2", "-segment_format", "mpegts", "-reset_timestamps", "1",
                 "-segment_start_number", "500", str(d / "seg_%d.ts")], check=True)
            base = 1_800_000_000.0
            for k, p in enumerate(sorted(d.glob("seg_*.ts"), key=lambda p: int(p.stem[4:]))):
                os.utime(p, (base + 2 * (k + 1), base + 2 * (k + 1)))
            r = Recorder("rtsp://camera", d, segment_seconds=2)
            out = d / "event" / "clip.mp4"
            job = ClipJob(1, base + 1.0, base + 6.0, out, None)  # window = 1 s..6 s of the footage
            self.assertTrue(r._build(job, force=True))
            dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                        str(out)], capture_output=True, text=True, check=True).stdout)
            self.assertAlmostEqual(dur, 6.0, delta=0.6)  # from 0 s, where the segment holding 1 s starts
            streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                                      str(out)], capture_output=True, text=True, check=True).stdout.split()
            self.assertEqual(sorted(streams), ["aac", "h264"])
            self.assertFalse(list(out.parent.glob("*.part.mp4")))

    def test_window_starting_mid_gop_keeps_all_its_frames(self):
        # The eye camera sends a keyframe every 2 s, so each segment is one GOP.
        with tempfile.TemporaryDirectory() as d:
            ring, out = Path(d) / "eye", Path(d) / "clip_wide.mp4"
            ring.mkdir()
            base = 1_800_000_000.0
            _write_ring(ring, "320x180", 500, base, seconds=14, gop=30)
            r = Recorder(None, ring, segment_seconds=2)
            self.assertTrue(r._build(ClipJob(1, base + 6.5, base + 12.5, out, None), force=True))
            frames = _probe(out, "stream=nb_read_frames", "-count_frames")
            self.assertGreaterEqual(int(frames), 6 * 15)  # all of the window, from the keyframe at 6 s
            self.assertEqual(_probe(out, "stream=start_time"), "0.000000")

    def test_no_footage_fails_when_forced(self):
        with tempfile.TemporaryDirectory() as d:
            r = Recorder("rtsp://camera", d, segment_seconds=2)
            self.assertFalse(r._build(ClipJob(1, 10.0, 40.0, Path(d) / "c.mp4", None), force=True))

    def test_two_rings_cut_into_one_event_folder_at_once(self):
        # The PTZ ring and the eye's ring cut clip.mp4 and clip_wide.mp4 for the same event at the same moment.
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            base = 1_800_000_000.0
            event = d / "media" / "2026-11-02" / "1"
            rings = {"clip.mp4": (Recorder("rtsp://camera", d / "ptz", 2), "320x180"),
                     "clip_wide.mp4": (Recorder(None, d / "eye", 2), "256x144")}
            for r, size in rings.values():
                r.seg_dir.mkdir()
                _write_ring(r.seg_dir, size, 500, base)
            barrier, results = threading.Barrier(len(rings)), {}

            def build(name):
                barrier.wait()
                results[name] = rings[name][0]._build(ClipJob(1, base + 1.0, base + 7.0, event / name, None),
                                                      force=True)

            threads = [threading.Thread(target=build, args=(name,)) for name in rings]
            for t in threads:
                t.start()
            for t in threads:
                t.join(120)
            self.assertEqual(results, {"clip.mp4": True, "clip_wide.mp4": True})
            for name, (_, size) in rings.items():  # each clip holds its own ring's footage
                self.assertEqual(_probe(event / name, "stream=width,height"), size.replace("x", ","))
            self.assertEqual(sorted(p.name for p in event.iterdir()), ["clip.mp4", "clip_wide.mp4"])


class SegmentWriterTests(unittest.TestCase):
    def test_both_writers_use_the_same_segment_options(self):
        with tempfile.TemporaryDirectory() as d:
            cmd = Recorder("rtsp://camera", d, segment_seconds=2)._cmd(1234)
            args = segment_output_args(d, 2, 1234)
            self.assertEqual(cmd[-len(args):], args)
            self.assertEqual(args[-1], str(Path(d) / "seg_%d.ts"))

    def test_next_start_number_keeps_rising(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            now = int(time.time())
            self.assertIn(next_start_number(d / "missing"), (now, now + 1))
            _touch(d / "seg_100.ts", now)  # an old run: the epoch second wins
            self.assertIn(next_start_number(d), (now, now + 1))
            _touch(d / f"seg_{now + 500}.ts", now)  # numbers ran ahead of the clock (many quick restarts)
            _touch(d / f"seg_{now + 900}.part", now)
            self.assertEqual(next_start_number(d), now + 501)


class PassiveRingTests(unittest.TestCase):
    def test_online_and_partial_segment_follow_the_writer(self):
        with tempfile.TemporaryDirectory() as d:
            alive = [False]
            r = Recorder(None, d, segment_seconds=2, writer_alive=lambda: alive[0])
            now = time.time()
            for i, n in enumerate([100, 101, 102]):
                _touch(Path(d) / f"seg_{n}.ts", now - 4 + 2 * i)
            self.assertFalse(r.online)
            self.assertEqual([s.n for s in r.segments()], [100, 101, 102])
            alive[0] = True
            self.assertTrue(r.online)
            self.assertEqual([s.n for s in r.segments()], [100, 101])  # the newest is still being written
            self.assertEqual([s.n for s in r.segments(include_partial=True)], [100, 101, 102])
            for i, n in enumerate([100, 101, 102]):  # alive, but the ring stopped growing
                _touch(Path(d) / f"seg_{n}.ts", now - 24 + 2 * i)
            self.assertFalse(r.online)

    def test_indexes_and_cuts_segments_another_process_writes(self):
        with tempfile.TemporaryDirectory() as d:
            ring, out = Path(d) / "eye", Path(d) / "event" / "clip_wide.mp4"
            alive = [True]
            r = Recorder(None, ring, segment_seconds=2, writer_alive=lambda: alive[0])
            done, results = threading.Event(), []

            def finished(event_id, ok, lead=0.0):
                results.append((event_id, ok))
                done.set()

            with mock.patch("ptzcam.recorder.spawn") as spawn:
                r.start()  # first the ring, then the writer, as UsbCapture does
                self.assertFalse(r.online)
                base = time.time() - 9
                segs = _write_ring(ring, "320x180", next_start_number(ring), base)  # ending 7, 5, 3 and 1 s ago
                self.assertEqual(len(segs), 4)
                self.assertTrue(r.online)
                self.assertEqual(len(r.segments()), 3)
                r.request_clip(7, base + 1.0, base + 6.0, out, finished)  # due now, from the complete segments
                self.assertTrue(done.wait(60))
                alive[0] = False
                self.assertFalse(r.online)
                self.assertEqual(len(r.segments()), 4)
                r.stop()
            spawn.assert_not_called()
            self.assertEqual(results, [(7, True)])
            self.assertAlmostEqual(float(_probe(out, "format=duration")), 6.0, delta=0.6)  # from 0 s: see above
            self.assertEqual(_probe(out, "stream=codec_name,width,height"), "h264,320,180")
            self.assertEqual(sorted(p.name for p in out.parent.iterdir()), ["clip_wide.mp4"])


class StopTests(unittest.TestCase):
    def test_clip_still_waiting_for_footage_at_stop_is_built(self):
        with tempfile.TemporaryDirectory() as d:
            ring, out = Path(d) / "eye", Path(d) / "event" / "clip_wide.mp4"
            r = Recorder(None, ring, segment_seconds=2)
            r.start()
            now = time.time()
            _write_ring(ring, "320x180", 500, now - 12)  # footage up to 4 s ago; the window ends 3 s ago
            results, stopper = [], []
            build = r._build

            def build_then_stop(job, force=False):
                if not force and not stopper:  # stop() comes while the worker finds the footage incomplete
                    stopper.append(threading.Thread(target=r.stop))
                    stopper[0].start()
                    r._stop.wait(5)
                return build(job, force)

            with mock.patch.object(r, "_build", side_effect=build_then_stop):
                r.request_clip(3, now - 11, now - 3, out, lambda eid, ok, lead=0.0: results.append((eid, ok)))
                self.assertTrue(wait_until(lambda: stopper))
                stopper[0].join(60)
            self.assertEqual(results, [(3, True)])
            self.assertTrue(out.exists())


def wait_until(cond, timeout=10.0):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.02)
    return cond()


if __name__ == "__main__":
    unittest.main()
