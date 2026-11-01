"""Offline tracking/counting evaluation on a video file, using the production detector, tracker and
counting rules frame by frame (not real time).

    .venv/bin/python tools/eval_tracking.py VIDEO --out DIR [--fps 15] [--model models/yolov11m.hef]

Prints a per-category summary and every counted object, and writes `sheet.jpg`: a grid of annotated
frames with track ids, to spot id switches and double counts by eye.
"""
import argparse
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

os.environ.setdefault("HAILORT_LOGGER_PATH", "NONE")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from ptzcam.config import DEFAULTS, deep_merge  # noqa: E402
from ptzcam.detector import HailoDetector  # noqa: E402
from ptzcam.draw import draw_box  # noqa: E402
from ptzcam.events import EventManager  # noqa: E402
from ptzcam.storage import Storage  # noqa: E402
from ptzcam.tracker import Tracker  # noqa: E402


class _Cfg:
    def __init__(self, data):
        self.data = data


class _Null:
    def publish(self, *a):
        pass

    def request_clip(self, *a):
        pass


def frames(path, w, h, fps):
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-an", "-vf", f"fps={fps},scale={w}:{h}",
           "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = w * h * 3
    while True:
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    p.wait()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out", required=True)
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--model", default="models/yolov11m.hef")
    ap.add_argument("--sheet-every", type=int, default=10, help="frames between contact-sheet tiles")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    w, h = 1280, 720

    det = HailoDetector(ROOT / args.model)
    tracker = Tracker(w / h, DEFAULTS["detect"]["min_hits"], DEFAULTS["detect"]["min_move"])
    tmp = tempfile.TemporaryDirectory()
    storage = Storage(Path(tmp.name) / "eval.db")
    cfg = _Cfg(deep_merge(DEFAULTS, {}))
    events = EventManager(cfg, storage, _Null(), Path(tmp.name) / "media", _Null())

    tiles, all_tracks, n = [], {}, 0
    t0 = 1_800_000_000.0
    for n, frame in enumerate(frames(args.video, w, h, args.fps)):
        ts = t0 + n / args.fps
        tracks, removed = tracker.update(det.detect(frame), ts)
        events.process(ts, frame, tracks, removed, True)
        for t in tracks:
            all_tracks[t.id] = t
        if n % args.sheet_every == 0:
            img = frame.copy()
            for t in tracks:
                if t.confirmed and t.last_ts == ts:
                    tag = f"#{t.id}{'*' if t.counted else ''}"
                    draw_box(img, t.box, t.category, tag, thickness=2 if t.moving else 1, dim=not t.moving)
            cv2.putText(img, f"frame {n}", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 3)
            tiles.append(cv2.resize(img, (640, 360)))
    det.close()

    confirmed = [t for t in all_tracks.values() if t.confirmed]
    counted = [t for t in confirmed if t.counted]
    print(f"frames: {n + 1} @ {args.fps} fps")
    print("confirmed tracks:", dict(Counter(t.category for t in confirmed)))
    print("counted         :", dict(Counter(t.category for t in counted)))
    print("stationary (not moving) confirmed:", dict(Counter(t.category for t in confirmed if not t.moving)))
    for t in sorted(counted, key=lambda t: t.first_ts):
        f0, f1 = round((t.first_ts - t0) * args.fps), round((t.last_ts - t0) * args.fps)
        print(f"  #{t.id:<4} {t.label:<10} frames {f0:>4}-{f1:<4} hits {t.hits:<4} dir {t.direction():<8} "
              f"max {t.max_score:.2f}")

    cols = 4
    while len(tiles) % cols:
        tiles.append(np.zeros_like(tiles[0]))
    rows = [np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]
    cv2.imwrite(str(out / "sheet.jpg"), np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 80])
    print("sheet:", out / "sheet.jpg")


if __name__ == "__main__":
    main()
