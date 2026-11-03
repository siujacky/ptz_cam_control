"""Try the eye's capture (ptzcam/usbcam.py) on the real USB camera: one ffmpeg that records the ring and decodes.

    .venv/bin/python tools/usbcam_smoke.py [--device "Avaya HC020"] [--seconds 15] [--anti-flicker N]

Runs UsbCapture for --seconds into a ring in RAM and reports the frames received per second, the segments and
their durations, a 6 s clip cut with ring.request_clip (checked with ffprobe), a clip still pending at stop, and
the CPU of the ffmpeg process. Nothing is kept: the ring and the clips are deleted at the end. Only one process
can stream the camera, so while another one has it this waits and retries; the app must not be using it.
--anti-flicker also sets that on connect, as the app does, and puts the old value back afterwards.
"""
import argparse
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ptzcam import uvc  # noqa: E402
from ptzcam.usbcam import UsbCapture  # noqa: E402

TICK = os.sysconf("SC_CLK_TCK")


def holders(dev: str) -> list:
    """Other processes that have the video node open (only those this user may look at)."""
    real, out = os.path.realpath(dev), []
    for p in Path("/proc").glob("[0-9]*"):
        if int(p.name) == os.getpid():
            continue
        try:
            if any(os.readlink(f) == real for f in (p / "fd").iterdir()):
                out.append(f"{p.name} {(p / 'comm').read_text().strip()}")
        except OSError:
            continue
    return out


def cpu_seconds(pid) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / TICK  # utime + stime


def probe(path, *args) -> str:
    r = subprocess.run(["ffprobe", "-v", "error", *args, "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    out = r.stdout.strip() or r.stderr.strip()
    return out.splitlines()[0] if out else ""  # MPEG-TS lists a stream twice (program and streams)


def check_clip(path: Path) -> str:
    if not path.exists():
        return "missing"
    frames = probe(path, "-count_frames", "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames")
    size = probe(path, "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height,avg_frame_rate")
    dur = float(probe(path, "-show_entries", "format=duration"))
    return f"{frames} frames, {dur:.2f} s ({size}), {path.stat().st_size / 1000:.0f} kB"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--device", default="Avaya HC020", help="camera name or /dev path, as the app's eye.device")
    ap.add_argument("--size", default="1920x1080", help="what the camera sends (H.264)")
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--out", default="1280x720", help="size of the frames delivered")
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--anti-flicker", type=int, help="set power_line_frequency on connect: 0 off, 1 50 Hz, 2 60 Hz")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    w, h = (int(v) for v in args.size.split("x"))
    ow, oh = (int(v) for v in args.out.split("x"))
    try:
        dev = uvc.find_device(args.device)
    except LookupError as e:
        sys.exit(str(e))
    print(f"camera {args.device!r} is {os.path.realpath(dev)}")
    old_flicker = uvc.read_control(dev, "power_line_frequency")
    print(f"power_line_frequency = {old_flicker}")
    for _ in range(20):
        busy = holders(dev)
        if not busy:
            break
        print(f"busy ({', '.join(busy)}); retrying in 3 s")
        time.sleep(3)

    with tempfile.TemporaryDirectory(prefix="usbcam_smoke_", dir="/dev/shm") as tmp:
        tmp = Path(tmp)
        cap = UsbCapture(args.device, w, h, args.fps, ow, oh, tmp / "ring", anti_flicker=args.anti_flicker)
        done, windows = {}, {}
        finished = threading.Event()

        def clip_done(event_id, ok):
            done[event_id] = (ok, time.time())
            finished.set()

        t_start = time.time()
        cap.start()
        try:
            got = cap.wait_frame(0, timeout=30.0)
            while got is None and time.time() - t_start < 60:  # e.g. busy at first: the capture retries
                got = cap.wait_frame(0, timeout=5.0)
            if got is None:
                sys.exit("no frames from the camera")
            first = time.time() - t_start
            proc = cap._proc
            print(f"first frame after {first:.2f} s: {got[2].shape[1]}x{got[2].shape[0]}")
            print("ffmpeg " + " ".join(proc.args[1:]))
            fid, t0 = got[0], time.time()
            c0, s0 = cpu_seconds(proc.pid), cpu_seconds("self")
            stamps, first_id = [got[1]], got[0]
            while time.time() - t0 < args.seconds:
                item = cap.wait_frame(fid, timeout=1.0)
                if item is not None:
                    fid = item[0]
                    stamps.append(item[1])
            wall = time.time() - t0
            c1, s1 = cpu_seconds(proc.pid), cpu_seconds("self")
            frames = fid - first_id
            settled = [ts for ts in stamps if ts >= t0 + 2.0]  # the first frames come in a burst
            gap = max(b - a for a, b in zip(stamps, stamps[1:]))
            print(f"frames: {frames} in {wall:.1f} s = {frames / wall:.2f} fps; after the first 2 s "
                  f"{(len(settled) - 1) / (settled[-1] - settled[0]):.2f} fps, largest gap {1000 * gap:.0f} ms; "
                  f"source online {cap.online}, ring online {cap.ring.online}")
            print(f"CPU: ffmpeg {100 * (c1 - c0) / wall:.1f} % of a core, this Python process "
                  f"{100 * (s1 - s0) / wall:.1f} % (reading frames)")

            t1 = time.time() - 1.0
            cap.ring.request_clip(1, t1 - 6.0, t1, tmp / "clip.mp4", clip_done)
            windows[1] = (t1 - 6.0, t1)
            if not finished.wait(30):
                print("clip: not built within 30 s")
            else:
                print(f"clip: ok={done[1][0]}, ready {done[1][1] - t1:.1f} s after its window closed")
            now = time.time()
            cap.ring.request_clip(2, now - 4.0, now + 1.0, tmp / "clip_stop.mp4", clip_done)  # still pending at stop
            windows[2] = (now - 4.0, now)
        finally:
            t = time.time()
            cap.stop()
            stopped = time.time() - t
            if args.anti_flicker is not None and args.anti_flicker >= 0 and args.anti_flicker != old_flicker:
                print("power_line_frequency %s -> %s (put back)" % uvc.write_control(dev, "power_line_frequency",
                                                                                      old_flicker))
        print(f"stop: {stopped:.2f} s, ffmpeg exit code {cap._proc.returncode}; "
              f"clip at stop ok={done.get(2, [None])[0]}")

        segs = cap.ring.segments()
        print(f"segments: {len(segs)}")
        recorded = 0
        for i, s in enumerate(segs):
            p = Path(s.path)
            n = probe(p, "-count_packets", "-select_streams", "v:0", "-show_entries", "stream=nb_read_packets")
            recorded += int(n) if n.isdigit() else 0
            key = probe(p, "-select_streams", "v:0", "-show_entries", "packet=flags", "-read_intervals", "%+#1")
            dur = probe(p, "-show_entries", "format=duration")
            gap = f", {s.end - segs[i - 1].end:.3f} s after the previous" if i else ""
            print(f"  {p.name}: {n} frames, {float(dur):.3f} s, starts with a keyframe: {key.startswith('K')}, "
                  f"{p.stat().st_size} B{gap}")
        print(f"frames decoded in all: {cap._frame_id}, frames recorded in the ring: {recorded}")
        if len(segs) > 2:
            mid = segs[len(segs) // 2].path
            r = subprocess.run(["ffmpeg", "-v", "error", "-i", mid, "-f", "null", "-"], capture_output=True, text=True)
            print(f"a middle segment decodes on its own: {'yes' if not r.stderr.strip() else r.stderr.strip()[:200]}")
        last = Path(segs[-1].path) if segs else None
        if last:
            print(f"last segment closed cleanly: {last.stat().st_size % 188 == 0} ({last.stat().st_size} B)")
        for n, name in ((1, "clip.mp4"), (2, "clip_stop.mp4")):
            a, b = windows.get(n, (0, 0))
            held = [s for s in segs if s.end > a and s.start < b]
            span = f"; footage from {a - held[0].start:.2f} s before the window" if held else ""
            print(f"{name}: window {b - a:.1f} s{span}: {check_clip(tmp / name)}")
    print(f"deleted {tmp}: {not tmp.exists()}")


if __name__ == "__main__":
    main()
