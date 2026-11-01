import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ptzcam.recorder import ClipJob, Recorder


def _touch(path: Path, mtime: float):
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))


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
            self.assertAlmostEqual(dur, 5.0, delta=0.6)
            streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                                      str(out)], capture_output=True, text=True, check=True).stdout.split()
            self.assertEqual(sorted(streams), ["aac", "h264"])
            self.assertFalse(list(out.parent.glob("*.part.mp4")))

    def test_no_footage_fails_when_forced(self):
        with tempfile.TemporaryDirectory() as d:
            r = Recorder("rtsp://camera", d, segment_seconds=2)
            self.assertFalse(r._build(ClipJob(1, 10.0, 40.0, Path(d) / "c.mp4", None), force=True))


if __name__ == "__main__":
    unittest.main()
