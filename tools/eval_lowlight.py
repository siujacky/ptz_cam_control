"""Detector-input comparison: raw frames vs classical enhancement vs Zero-DCE / Zero-DCE++ on the Hailo-10H
(night), or vs daytime auto-levels (`levels`, washed-out pictures), all through the production detector,
tracker and counting rules (config.json thresholds).

    .venv/bin/python tools/eval_lowlight.py INPUT [INPUT...] --out DIR [--fps 15] [--start 2] [--duration 10]
                                            [--variants raw,classic,zero_dce,zero_dce_pp]

INPUT is a video file, or a directory of MPEG-TS segments (seg_<n>.ts, joined in order). Writes:
  grid_*.jpg        inputs side by side [raw | classic / zero_dce | zero_dce_pp] with detections
  only_<v>.jpg      crops of confident detections a variant found that raw did not (judge true/false by eye)
  lost_<v>.jpg      crops of confident raw detections the variant missed
  summary.json
Only one process can own the Hailo device: stop the ptzcam service first (or use --fake to dry-run).
"""
import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("HAILORT_LOGGER_PATH", "NONE")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from ptzcam.config import DEFAULTS, deep_merge  # noqa: E402
from ptzcam.detector import Detection, HailoDetector, HailoEnhancer, _iou  # noqa: E402
from ptzcam.draw import draw_box  # noqa: E402
from ptzcam.events import EventManager  # noqa: E402
from ptzcam.lowlight import AutoLevels  # noqa: E402
from ptzcam.storage import Storage  # noqa: E402
from ptzcam.tracker import TRACK_THRESH, Tracker  # noqa: E402

VARIANTS = ["raw", "classic", "zero_dce", "zero_dce_pp"]  # also "levels"; pick with --variants
W, H = 1280, 720
DW, DH = 640, 360          # what the detector actually sees of a 16:9 frame (letterboxed into 640x640)
MATCH_IOU = 0.3


def clone_enhancer(e):
    c = copy.copy(e)
    c._in = np.empty_like(e._in)
    c._out = np.empty_like(e._out)
    c._bindings = e._configured.create_bindings(output_buffers={e._model.output().name: c._out})
    return c


def clone_detector(det):
    """Same configured model, own buffers: HailoRT runs concurrently only with separate bindings."""
    c = copy.copy(det)
    c._canvas = np.full_like(det._canvas, 114)
    c._layout = None
    c._out = np.empty_like(det._out)
    c._bindings = det._configured.create_bindings(output_buffers={det._model.output().name: c._out})
    return c


_CLAHE = threading.local()


def classic(bgr, target=0.4, clip=2.0):
    """Adaptive gamma (mean luma -> target) then CLAHE, on luma only."""
    if not hasattr(_CLAHE, "c"):
        _CLAHE.c = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8))
    ycc = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb)
    y = ycc[:, :, 0]
    m = max(float(cv2.mean(y)[0]) / 255.0, 1e-3)
    g = float(np.clip(np.log(target) / np.log(m), 0.3, 1.0))
    lut = np.clip(((np.arange(256) / 255.0) ** g) * 255.0 + 0.5, 0, 255).astype(np.uint8)
    ycc[:, :, 0] = _CLAHE.c.apply(cv2.LUT(y, lut))
    return cv2.cvtColor(ycc, cv2.COLOR_YCrCb2BGR)


# ---------- fakes for --fake dry runs (no Hailo device) ----------
class FakeDetector:
    def __init__(self):
        self.rng = np.random.default_rng(1)

    def detect(self, img):
        y = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        bright = float(y.mean()) / 255
        dets = []
        if self.rng.random() < 0.5 + bright:
            x = self.rng.uniform(0.1, 0.7)
            dets.append(Detection(np.array([x, 0.3, x + 0.2, 0.5]), float(min(0.95, 0.3 + bright)), 2, "vehicle"))
        if self.rng.random() < bright:
            dets.append(Detection(np.array([0.5, 0.5, 0.55, 0.7]), 0.4, 0, "person"))
        return dets


class FakeEnhancer:
    def __init__(self, name, g):
        self.name, self.g = name, g

    def enhance(self, frame_bgr):
        small = cv2.resize(frame_bgr, (600, 400))
        lut = np.clip(((np.arange(256) / 255.0) ** self.g) * 255.0, 0, 255).astype(np.uint8)
        return cv2.cvtColor(cv2.LUT(small, lut), cv2.COLOR_BGR2RGB)

    def close(self):
        pass


# ---------- input ----------
def decode(path, fps, start, duration):
    path = Path(path)
    tmp = None
    if path.is_dir():
        segs = sorted(path.glob("seg_*.ts"), key=lambda p: int(p.stem.split("_")[1]))
        tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        tmp.write("".join(f"file '{s}'\n" for s in segs))
        tmp.close()
        src = ["-f", "concat", "-safe", "0", "-i", tmp.name]
    else:
        src = ["-i", str(path)]
    seek = ["-ss", str(start)] if start else []
    dur = ["-t", str(duration)] if duration else []
    cmd = ["ffmpeg", "-v", "error", *seek, *src, *dur, "-an", "-vf", f"fps={fps},scale={W}:{H}",
           "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = W * H * 3
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(H, W, 3)
    finally:
        p.kill()
        p.wait()
        if tmp:
            os.unlink(tmp.name)


class _Cfg:
    def __init__(self, data):
        self.data = data


class _Null:
    def publish(self, *a):
        pass

    def request_clip(self, *a):
        pass


def crop_tile(img, box, label, color=(255, 255, 255), size=160):
    h, w = img.shape[:2]
    x1, y1, x2, y2 = box[0] * w, box[1] * h, box[2] * w, box[3] * h
    cx, cy, s = (x1 + x2) / 2, (y1 + y2) / 2, max(x2 - x1, y2 - y1) * 0.7 + 8
    a, b = int(max(0, cx - s)), int(max(0, cy - s))
    c, d = int(min(w, cx + s)), int(min(h, cy + s))
    tile = cv2.resize(img[b:d, a:c], (size, size)) if d > b and c > a else np.zeros((size, size, 3), np.uint8)
    cv2.putText(tile, label, (3, size - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 0, 0), 3)
    cv2.putText(tile, label, (3, size - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.42, color, 1)
    return tile


def sheet(tiles, path, cols=8):
    if not tiles:
        return
    tiles = tiles[:cols * 12]
    while len(tiles) % cols:
        tiles.append(np.zeros_like(tiles[0]))
    cv2.imwrite(str(path), np.vstack([np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]),
                [cv2.IMWRITE_JPEG_QUALITY, 85])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--fps", type=float, default=15)
    ap.add_argument("--start", type=float, default=0)
    ap.add_argument("--duration", type=float, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--grid-every", type=float, default=3.0, help="seconds between comparison grids")
    ap.add_argument("--max-grids", type=int, default=40)
    ap.add_argument("--fake", action="store_true", help="dry run without the Hailo device")
    ap.add_argument("--variants", default=",".join(VARIANTS), help="comma-separated, raw first (also: levels)")
    args = ap.parse_args()
    VARIANTS[:] = args.variants.split(",")
    if VARIANTS[0] != "raw" or not set(VARIANTS) <= {"raw", "classic", "zero_dce", "zero_dce_pp", "levels"}:
        ap.error("--variants must start with raw and use raw, classic, zero_dce, zero_dce_pp or levels")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    file_cfg = json.loads((ROOT / "config.json").read_text())
    cfg = deep_merge(DEFAULTS, file_cfg)
    cfg["detect"]["zone"] = []
    enabled = {c for c, v in cfg["detect"]["categories"].items() if v["enabled"]}
    thresh = {c: min(TRACK_THRESH, v["min_score"]) for c, v in cfg["detect"]["categories"].items()}

    if args.fake:
        det = FakeDetector()
        enh = {"zero_dce": FakeEnhancer("zero_dce", 0.35), "zero_dce_pp": FakeEnhancer("zero_dce_pp", 0.45)}
    else:
        det = HailoDetector(ROOT / cfg["detect"]["model"])
        enh = {v: HailoEnhancer(det._vdevice, ROOT / "models" / "lowlight" / f"{v}.hef")
               for v in ("zero_dce", "zero_dce_pp") if v in VARIANTS}

    tls = threading.local()

    def objs():
        if not hasattr(tls, "det"):
            tls.det = det if args.fake else clone_detector(det)
            tls.enh = {k: (e if args.fake else clone_enhancer(e)) for k, e in enh.items()}
        return tls

    def run(frame, variant):
        o = objs()
        t0 = time.perf_counter()
        rgb = None
        if variant == "raw":
            img = cv2.resize(frame, (DW, DH), interpolation=cv2.INTER_LINEAR)
            src = frame
        elif variant == "classic":
            img = src = classic(cv2.resize(frame, (DW, DH), interpolation=cv2.INTER_LINEAR))
        elif variant == "levels":  # per frame, without the live view's smoothing (frames run out of order here)
            fixed = AutoLevels().process(frame)
            src = fixed if fixed is not None else frame
            img = cv2.resize(src, (DW, DH), interpolation=cv2.INTER_LINEAR)
        else:
            rgb = o.enh[variant].enhance(frame)
            img = cv2.cvtColor(cv2.resize(rgb, (DW, DH), interpolation=cv2.INTER_LINEAR), cv2.COLOR_RGB2BGR)
            src = frame
        t1 = time.perf_counter()
        dets = [d for d in (o.det.detect(src, enhanced=rgb) if rgb is not None and not args.fake else o.det.detect(img if rgb is not None else src))
                if d.category in enabled]
        return img, dets, t1 - t0, time.perf_counter() - t1

    pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
    tmpdir = tempfile.TemporaryDirectory()
    S = {v: {"dets35": Counter(), "dets50": Counter(), "score_sum": 0.0, "score_n": 0, "new": Counter(),
             "boosted": Counter(), "lost": Counter(), "matched": 0, "delta_sum": 0.0, "confirmed": Counter(),
             "counted": Counter(), "counted_list": [], "t_pre": 0.0, "t_det": 0.0} for v in VARIANTS}
    only_tiles = defaultdict(list)
    lost_tiles = defaultdict(list)
    last_tile_ts = defaultdict(lambda: -1e9)
    n_frames, n_grids, luma = 0, 0, []
    wall0 = time.time()

    for fi, inp in enumerate(args.inputs):
        t_base = 1_800_000_000.0 + fi * 100_000
        trackers, events, all_tracks = {}, {}, {}
        for v in VARIANTS:
            trackers[v] = Tracker(W / H, cfg["detect"]["min_hits"], cfg["detect"]["min_move"])
            st = Storage(Path(tmpdir.name) / f"{fi}_{v}.db")
            events[v] = EventManager(_Cfg(cfg), st, _Null(), Path(tmpdir.name) / f"media_{fi}_{v}", _Null())
            all_tracks[v] = {}
        last_grid = -1e9
        gen = decode(inp, args.fps, args.start, args.duration)
        idx = 0
        while True:
            chunk = []
            for frame in gen:
                chunk.append(frame)
                if len(chunk) >= 16:
                    break
            if not chunk:
                break
            futs = [[pool.submit(run, f, v) for v in VARIANTS] for f in chunk]
            for frame, fr in zip(chunk, futs):
                ts = t_base + idx / args.fps
                res = {v: f.result() for v, f in zip(VARIANTS, fr)}
                luma.append(float(cv2.cvtColor(res["raw"][0], cv2.COLOR_BGR2GRAY).mean()))
                raw_dets = res["raw"][1]
                for v in VARIANTS:
                    img, dets, tp, td = res[v]
                    s = S[v]
                    s["t_pre"] += tp
                    s["t_det"] += td
                    for d in dets:
                        if d.score >= TRACK_THRESH:
                            s["dets35"][d.category] += 1
                            s["score_sum"] += d.score
                            s["score_n"] += 1
                        if d.score >= 0.5:
                            s["dets50"][d.category] += 1
                    tracks, removed = trackers[v].update(dets, ts, thresh)
                    events[v].process(ts, frame, tracks, removed, True)
                    for t in tracks:
                        all_tracks[v][t.id] = t
                    if v == "raw":
                        continue
                    # detection-level agreement with raw, same frame
                    for d in dets:
                        if d.score < TRACK_THRESH:
                            continue
                        best, bs = 0.0, 0.0
                        for r in raw_dets:
                            if r.category == d.category:
                                i = _iou(d.box, r.box)
                                if i > best:
                                    best, bs = i, r.score
                        if best >= MATCH_IOU and bs >= TRACK_THRESH:
                            s["matched"] += 1
                            s["delta_sum"] += d.score - bs
                        elif best >= MATCH_IOU:
                            s["boosted"][d.category] += 1
                        else:
                            s["new"][d.category] += 1
                            key = (v, "only")
                            if ts - last_tile_ts[key] >= 0.5:
                                last_tile_ts[key] = ts
                                lab = f"{fi}:{idx} {d.category[:4]} {d.score:.2f}"
                                only_tiles[v].append(np.hstack([crop_tile(img, d.box, lab, (0, 255, 0)),
                                                                crop_tile(res["raw"][0], d.box, "raw")]))
                    for r in raw_dets:
                        if r.score < TRACK_THRESH:
                            continue
                        if not any(d.category == r.category and d.score >= 0.2 and _iou(d.box, r.box) >= MATCH_IOU
                                   for d in dets):
                            s["lost"][r.category] += 1
                            key = (v, "lost")
                            if ts - last_tile_ts[key] >= 0.5:
                                last_tile_ts[key] = ts
                                lab = f"{fi}:{idx} {r.category[:4]} {r.score:.2f}"
                                lost_tiles[v].append(np.hstack([crop_tile(res["raw"][0], r.box, lab, (0, 0, 255)),
                                                                crop_tile(img, r.box, v[:9])]))
                # comparison grid: periodic, or whenever the variants disagree on confident detections
                n35 = {v: sum(d.score >= TRACK_THRESH for d in res[v][1]) for v in VARIANTS}
                disagree = len(set(n35.values())) > 1
                if n_grids < args.max_grids and ts - last_grid >= args.grid_every and (disagree or max(n35.values())):
                    last_grid = ts
                    n_grids += 1
                    tiles = []
                    for v in VARIANTS:
                        im = res[v][0].copy()
                        for d in res[v][1]:
                            if d.score >= 0.25:
                                draw_box(im, d.box, d.category, f"{d.label if hasattr(d, 'label') else d.category} {d.score:.2f}",
                                         thickness=2 if d.score >= TRACK_THRESH else 1, dim=d.score < TRACK_THRESH)
                        cv2.putText(im, v, (8, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4)
                        cv2.putText(im, v, (8, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
                        tiles.append(im)
                    while len(tiles) % 2:
                        tiles.append(np.zeros_like(tiles[0]))
                    grid = np.vstack([np.hstack(tiles[i:i + 2]) for i in range(0, len(tiles), 2)])
                    cv2.imwrite(str(out / f"grid_{fi:02d}_{idx:05d}.jpg"), grid, [cv2.IMWRITE_JPEG_QUALITY, 82])
                idx += 1
                n_frames += 1
        # flush trackers so tracks still alive at the end are finalised
        for v in VARIANTS:
            for t in all_tracks[v].values():
                if t.confirmed:
                    S[v]["confirmed"][t.category] += 1
                if t.counted:
                    S[v]["counted"][t.category] += 1
                    S[v]["counted_list"].append({"input": fi, "id": t.id, "label": t.label,
                                                 "f0": round((t.first_ts - t_base) * args.fps),
                                                 "f1": round((t.last_ts - t_base) * args.fps),
                                                 "max": round(t.max_score, 2), "dir": t.direction()})
        print(f"[{fi + 1}/{len(args.inputs)}] {inp}: {idx} frames, {time.time() - wall0:.0f} s elapsed", flush=True)

    pool.shutdown()
    if not args.fake:
        for e in enh.values():
            e.close()
        det.close()

    summary = {"frames": n_frames, "fps": args.fps, "inputs": args.inputs, "mean_luma": round(float(np.mean(luma)), 1) if luma else None,
               "wall_s": round(time.time() - wall0, 1), "variants": {}}
    print(f"\nframes {n_frames} @ {args.fps} fps, mean luma {summary['mean_luma']}, wall {summary['wall_s']} s")
    print(f"{'variant':<12} {'det>=.35':>8} {'det>=.5':>8} {'mean sc':>7} {'new':>5} {'boost':>5} {'lost':>5} {'dscore':>6} "
          f"{'confirmed':>16} {'counted':>16} {'pre ms':>6} {'det ms':>6}")
    for v in VARIANTS:
        s = S[v]
        ms = s["score_sum"] / max(1, s["score_n"])
        dsc = s["delta_sum"] / max(1, s["matched"])
        summary["variants"][v] = {
            "dets35": dict(s["dets35"]), "dets50": dict(s["dets50"]), "mean_score": round(ms, 3),
            "new": dict(s["new"]), "boosted": dict(s["boosted"]), "lost": dict(s["lost"]), "matched": s["matched"],
            "mean_score_delta_matched": round(dsc, 3), "confirmed": dict(s["confirmed"]), "counted": dict(s["counted"]),
            "counted_list": s["counted_list"], "pre_ms": round(1000 * s["t_pre"] / max(1, n_frames), 2),
            "det_ms": round(1000 * s["t_det"] / max(1, n_frames), 2)}
        fmt = lambda c: ",".join(f"{k[:3]}{n}" for k, n in sorted(c.items())) or "-"  # noqa: E731
        print(f"{v:<12} {sum(s['dets35'].values()):>8} {sum(s['dets50'].values()):>8} {ms:>7.3f} "
              f"{sum(s['new'].values()):>5} {sum(s['boosted'].values()):>5} {sum(s['lost'].values()):>5} {dsc:>+6.3f} "
              f"{fmt(s['confirmed']):>16} {fmt(s['counted']):>16} "
              f"{summary['variants'][v]['pre_ms']:>6} {summary['variants'][v]['det_ms']:>6}")
    for v in VARIANTS[1:]:
        sheet(only_tiles[v], out / f"only_{v}.jpg")
        sheet(lost_tiles[v], out / f"lost_{v}.jpg")
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print("out:", out)


if __name__ == "__main__":
    main()
