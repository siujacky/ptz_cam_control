"""Entry point: python -m ptzcam [--source video.mp4] [--no-ptz] ..."""
import os

os.environ.setdefault("HAILORT_LOGGER_PATH", "NONE")  # must be set before HailoRT loads

import argparse  # noqa: E402
import logging  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import cv2  # noqa: E402
from aiohttp import web  # noqa: E402

from . import __version__  # noqa: E402
from .config import ConfigStore  # noqa: E402
from .pipeline import Pipeline  # noqa: E402
from .storage import Storage  # noqa: E402
from .sysinfo import SysInfo  # noqa: E402
from .web import EventBus, create_app  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ptzcam", description="Local AI object-tracking camera")
    ap.add_argument("--config", default=str(ROOT / "config.json"), help="config file (created if missing)")
    ap.add_argument("--source", help="video file or stream to use instead of the camera (testing)")
    ap.add_argument("--no-ptz", action="store_true", help="disable PTZ control")
    ap.add_argument("--data-dir", help="override storage.data_dir")
    ap.add_argument("--segment-dir", help="override storage.segment_dir")
    ap.add_argument("--host", help="override web.host")
    ap.add_argument("--port", type=int, help="override web.port")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    # Every image this app handles is small, and several threads work on frames at once. OpenCV's own
    # thread pool then burns up to twice the CPU for the same wall time (measured), which on a fanless
    # Pi is heat; one thread per call is enough.
    cv2.setNumThreads(1)

    logging.basicConfig(level=args.log_level.upper(), stream=sys.stdout,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    overrides: dict = {}
    if args.source:
        src = args.source if "://" in args.source else str(Path(args.source).resolve())
        overrides.setdefault("camera", {})["rtsp_url"] = src
    if args.no_ptz:
        overrides.setdefault("ptz", {})["enabled"] = False
    if args.data_dir:
        overrides.setdefault("storage", {})["data_dir"] = str(Path(args.data_dir).resolve())
    if args.segment_dir:
        overrides.setdefault("storage", {})["segment_dir"] = str(Path(args.segment_dir).resolve())
    if args.host:
        overrides.setdefault("web", {})["host"] = args.host
    if args.port:
        overrides.setdefault("web", {})["port"] = args.port

    cfg = ConfigStore(args.config, overrides, base_dir=ROOT)
    data_dir = cfg.resolve(cfg.data["storage"]["data_dir"])
    media_dir = data_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    web_dir = ROOT / "web"
    web_dir.mkdir(exist_ok=True)

    storage = Storage(data_dir / "ptzcam.db")
    bus = EventBus()
    pipeline = Pipeline(cfg, storage, bus, media_dir)
    ctx = SimpleNamespace(cfg=cfg, storage=storage, pipeline=pipeline, bus=bus,
                          sysinfo=SysInfo(data_dir, media_dir), web_dir=web_dir, media_dir=media_dir,
                          started=time.time())
    w = cfg.data["web"]
    logging.getLogger("ptzcam").info("PTZ Cam %s starting on http://%s:%d (camera %s)",
                                     __version__, w["host"], w["port"], cfg.data["camera"]["rtsp_url"])
    web.run_app(create_app(ctx), host=w["host"], port=w["port"], access_log=None, print=None,
                shutdown_timeout=3)


if __name__ == "__main__":
    main()
