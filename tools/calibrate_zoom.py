"""Measure the lens once: magnification at each VISCA zoom position, written to config.json as
`ptz.zoom_curve` (and the zoom-direct speed as `ptz.zoom_rate`), for auto-zoom's one-step zoom.

    sudo systemctl stop ptzcam
    .venv/bin/python tools/calibrate_zoom.py [--dry-run]
    sudo systemctl start ptzcam

The camera goes to its Home view, zooms from wide to full zoom in 16 steps (about a minute), and comes back
Home. It needs a picture with detail: not at night, and not while the sun has washed it out.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ptzcam.camera import FrameSource  # noqa: E402
from ptzcam.config import DEFAULTS, deep_merge, validate  # noqa: E402
from ptzcam.ptz import ViscaClient  # noqa: E402
from ptzcam.zoomcal import calibrate  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--dry-run", action="store_true", help="measure and print, but do not write the config")
    args = ap.parse_args()
    if subprocess.run(["systemctl", "is-active", "--quiet", "ptzcam"]).returncode == 0:
        sys.exit("The ptzcam service is running and owns the camera's PTZ connection: stop it first.")

    path = Path(args.config)
    raw = json.loads(path.read_text())
    cfg = deep_merge(DEFAULTS, raw)
    cam = cfg["camera"]
    source = FrameSource(cam["rtsp_url"], cfg["detect"]["width"], cfg["detect"]["height"], cfg["detect"]["fps"])
    source.start()
    visca = ViscaClient(cam["visca_host"], cam["visca_port"], cam["visca_address"], timeout=1.0)
    home = cfg["ptz"]["home_preset"]

    def latest_frame():
        deadline = time.time() + 10
        while time.time() < deadline:
            item = source.latest()
            if item is not None and source.online:
                return item[2]
            time.sleep(0.1)
        raise RuntimeError("no picture from the camera")

    try:
        latest_frame()
        if home:
            visca.send(b"\x01\x04\x3f\x02" + bytes([home]))  # recall the Home view (pan, tilt and zoom)
            time.sleep(3.0)
        curve, rate = calibrate(visca, latest_frame)
    finally:
        try:
            if home:
                visca.send(b"\x01\x04\x3f\x02" + bytes([home]))
            else:
                visca.send(b"\x01\x04\x47\x00\x00\x00\x00")
        finally:
            visca.close()
            source.stop()

    print(f"zoom-direct speed: {rate} positions/s; full zoom: x{curve[-1][1]:.1f}")
    if args.dry_run:
        return
    raw.setdefault("ptz", {})
    raw["ptz"]["zoom_curve"], raw["ptz"]["zoom_rate"] = curve, rate
    raw["ptz"].pop("zoom_speed", None)  # replaced by the measured zoom
    validate(deep_merge(DEFAULTS, raw), ROOT)
    st = path.stat()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2) + "\n")
    os.chown(tmp, st.st_uid, st.st_gid)  # the service (not root) must still be able to save settings
    os.chmod(tmp, st.st_mode & 0o777)
    os.replace(tmp, path)
    print(f"written to {path}")


if __name__ == "__main__":
    main()
