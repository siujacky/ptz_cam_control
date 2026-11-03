"""Measure the sync between the eye camera (the fixed USB camera) and the PTZ camera, once, in daylight.

    sudo systemctl stop ptzcam
    .venv/bin/python tools/calibrate_sync.py [--dry-run] [--grid 7x4] [--lens]
    sudo systemctl start ptzcam

The PTZ camera moves over a grid of pan/tilt positions that covers the eye's view (about 2 minutes, plus about a
minute for --lens), and the two pictures are matched by their image features. Writes data/sync.json (see
ptzcam/sync.py). --lens also measures the PTZ's zoom curve (as tools/calibrate_zoom.py does), which the eye needs
to choose a zoom; it is run anyway when the curve has never been measured. Needs a clear daytime picture on both
cameras. Re-run it whenever either camera is moved. The PTZ camera goes back Home at the end.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ptzcam.camera import FrameSource  # noqa: E402
from ptzcam.config import DEFAULTS, deep_merge, validate  # noqa: E402
from ptzcam.ptz import ZOOM_ARRIVED, ViscaClient, _from_nibbles, _nibbles, _s16  # noqa: E402
from ptzcam.syncal import calibrate_sync  # noqa: E402
from ptzcam.zoomcal import calibrate as calibrate_lens  # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
from uvc_controls import controls, find_device, get, key  # noqa: E402


class Ptz:
    """Blocking absolute moves over VISCA: each call returns once the camera has stopped."""

    def __init__(self, visca, home_preset):
        self.v, self.home_preset = visca, home_preset

    def position(self):
        r = self.v.send(b"\x09\x06\x12")
        z = self.v.send(b"\x09\x04\x47")
        return _s16(_from_nibbles(r[2:6])), _s16(_from_nibbles(r[6:10])), _from_nibbles(z[2:6])

    def _still(self, timeout=15.0):
        t0, last, steady = time.monotonic(), None, 0
        while time.monotonic() - t0 < timeout:
            time.sleep(0.2)
            p = self.position()
            steady = steady + 1 if p == last else 0
            last = p
            if steady >= 3:  # positions read with lag while moving: wait until several readings agree
                return p
        raise RuntimeError(f"the PTZ camera did not settle (last read {last})")

    def move(self, pan, tilt):
        self.v.send(b"\x01\x06\x02\x14\x12" + _nibbles(pan) + _nibbles(tilt))
        self._still()

    def zoom_to(self, pos):
        self.v.send(b"\x01\x04\x47" + _nibbles(pos))
        p = self._still()
        if abs(p[2] - pos) > ZOOM_ARRIVED:
            raise RuntimeError(f"the zoom did not reach {pos} (read {p[2]})")

    def home(self):
        if self.home_preset:
            self.v.send(b"\x01\x04\x3f\x02" + bytes([self.home_preset]))
        else:
            self.v.send(b"\x01\x06\x04")
        self._still()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--grid", default="7x4", help="positions across x down the eye's view")
    ap.add_argument("--lens", action="store_true", help="also measure the PTZ's zoom curve")
    ap.add_argument("--dry-run", action="store_true", help="measure and print, but write nothing")
    args = ap.parse_args()
    if subprocess.run(["systemctl", "is-active", "--quiet", "ptzcam"]).returncode == 0:
        sys.exit("The ptzcam service is running and owns both cameras: stop it first.")
    gx, gy = (int(n) for n in args.grid.lower().split("x"))

    path = Path(args.config)
    raw = json.loads(path.read_text())
    cfg = deep_merge(DEFAULTS, raw)
    cam, eye_cfg = cfg["camera"], cfg["eye"]
    data_dir = Path(cfg["storage"]["data_dir"])
    data_dir = data_dir if data_dir.is_absolute() else ROOT / data_dir

    source = FrameSource(cam["rtsp_url"], 1920, 1080, 15)
    source.start()
    dev = find_device(eye_cfg["device"])
    fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    try:  # the map only holds for the eye's current crop: record it, the app sets the same one
        ctrls = {key(c["name"]): c["id"] for c in controls(fd)}
        eptz = {k: get(fd, ctrls[k]) for k in ("zoom_absolute", "pan_absolute", "tilt_absolute") if k in ctrls}
    finally:
        os.close(fd)
    eye = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    eye.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    eye.set(cv2.CAP_PROP_FRAME_WIDTH, eye_cfg["width"])
    eye.set(cv2.CAP_PROP_FRAME_HEIGHT, eye_cfg["height"])
    visca = ViscaClient(cam["visca_host"], cam["visca_port"], cam["visca_address"], timeout=1.0)
    ptz = Ptz(visca, cfg["ptz"]["home_preset"])

    def ptz_frame():
        t = time.time() + 0.3  # a frame that left the camera after it stopped
        deadline = time.time() + 10
        while time.time() < deadline:
            item = source.latest()
            if item is not None and item[1] >= t:
                return item[2]
            time.sleep(0.05)
        raise RuntimeError("no picture from the PTZ camera")

    def eye_frame():
        for _ in range(6):  # the driver queues a few frames: skip the old ones
            eye.grab()
        ok, f = eye.read()
        if not ok:
            raise RuntimeError("no picture from the eye camera")
        return f

    curve = cfg["ptz"]["zoom_curve"]
    try:
        ptz_frame()
        eye_frame()
        m = calibrate_sync(ptz, ptz_frame, eye_frame, zoom=0, grid=(gx, gy))
        if args.lens or not curve:
            print("measuring the PTZ's zoom curve at Home")
            ptz.home()
            time.sleep(2.0)
            curve, rate = calibrate_lens(visca, lambda: source.latest()[2])
        ptz.home()
    finally:
        try:
            ptz.home()
        finally:
            visca.close()
            eye.release()
            source.stop()

    m.eye_size = (eye_cfg["width"], eye_cfg["height"])
    m.meta = {"eye_device": eye_cfg["device"], "eye_eptz": eptz, "grid": [gx, gy], "residuals": m.residuals()}
    r = m.meta["residuals"]
    print(f"{len(m.samples)} positions; tilt reached {m.tilt_limits[0]:.0f}..{m.tilt_limits[1]:.0f}")
    if r:
        print(f"pointing error (leave-one-out): pan {r['pan_median']:.1f} median / {r['pan_max']:.1f} max, "
              f"tilt {r['tilt_median']:.1f} / {r['tilt_max']:.1f} units")
    if args.dry_run:
        return
    out = data_dir / "sync.json"
    m.save(out)
    st = data_dir.stat()
    os.chown(out, st.st_uid, st.st_gid)  # the service (not root) reads it
    print(f"written to {out}")
    if curve != cfg["ptz"]["zoom_curve"]:
        raw.setdefault("ptz", {})
        raw["ptz"]["zoom_curve"], raw["ptz"]["zoom_rate"] = curve, rate
        raw["ptz"].pop("zoom_speed", None)
        validate(deep_merge(DEFAULTS, raw), ROOT)
        cst = path.stat()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, indent=2) + "\n")
        os.chown(tmp, cst.st_uid, cst.st_gid)
        os.chmod(tmp, cst.st_mode & 0o777)
        os.replace(tmp, path)
        print(f"zoom curve written to {path}: full zoom x{curve[-1][1]:.1f}")


if __name__ == "__main__":
    main()
