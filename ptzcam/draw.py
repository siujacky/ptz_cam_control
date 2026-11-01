"""Drawing helpers shared by the live overlay and event snapshots."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

from .config import CATEGORY_BGR

FONT = cv2.FONT_HERSHEY_SIMPLEX
ZONE_BGR = (0, 210, 255)


def _px(box, w, h):
    return int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)


def draw_box(img, box, category, text="", thickness=2, dim=False, quiet=False):
    """Box plus label. `quiet` labels (dark chip, coloured text) mark objects that are not moving."""
    h, w = img.shape[:2]
    color = CATEGORY_BGR.get(category, (220, 220, 220))
    if dim:
        color = tuple(int(c * 0.55) for c in color)
    x1, y1, x2, y2 = _px(box, w, h)
    cv2.rectangle(img, (x1, y1), (x2, y2), color, thickness, cv2.LINE_AA)
    if not text:
        return
    scale = max(0.45, min(0.9, h / 1080 * 1.2))
    weight = 2 if scale >= 0.75 else 1
    (tw, th), _ = cv2.getTextSize(text, FONT, scale, weight)
    pad = max(3, int(round(4 * scale)))
    top = y1 - th - 2 * pad
    if top < 0:  # no room above the box: put the label just inside its top edge
        top = min(y1 + thickness, h - th - 2 * pad)
    left = min(max(x1, 0), max(0, w - tw - 2 * pad))
    bg, fg = ((22, 22, 22), color) if quiet else (color, (15, 15, 15))
    cv2.rectangle(img, (left, top), (left + tw + 2 * pad, top + th + 2 * pad), bg, -1)
    cv2.putText(img, text, (left + pad, top + th + pad - 1), FONT, scale, fg, weight, cv2.LINE_AA)


def draw_zone(img, zone):
    if len(zone) < 3:
        return
    h, w = img.shape[:2]
    pts = np.array([[int(x * w), int(y * h)] for x, y in zone], dtype=np.int32)
    cv2.polylines(img, [pts], True, ZONE_BGR, 1, cv2.LINE_AA)


def draw_overlay(img, tracks, target_id, zone, ts):
    """Annotate a live frame: zone, every tracked object with its label (still objects drawn quieter),
    motion trails, and the follow target."""
    h, w = img.shape[:2]
    k = h / 540.0  # line widths are tuned for the 960x540 live view and scale with it
    draw_zone(img, zone)
    for t in tracks:
        active = t["moving"] or t["counted"]
        if t["moving"] and len(t["trail"]) > 1:
            pts = np.array([[int(x * w), int(y * h)] for x, y in t["trail"]], dtype=np.int32)
            cv2.polylines(img, [pts], False, CATEGORY_BGR.get(t["category"], (220, 220, 220)),
                          max(1, round(2 * k)), cv2.LINE_AA)
        text = f"{t['label']} {t['score']:.0%}" + ("" if active else " · still")
        draw_box(img, t["box"], t["category"], text,
                 thickness=max(2, round(3 * k)) if active else max(1, round(2 * k)), quiet=not active)
        if t["id"] == target_id:
            x1, y1, x2, y2 = _px(t["box"], w, h)
            cx, cy, r = (x1 + x2) // 2, (y1 + y2) // 2, max(8, min(x2 - x1, y2 - y1) // 4)
            cv2.circle(img, (cx, cy), r, (255, 255, 255), 1, cv2.LINE_AA)
            cv2.line(img, (cx - r - 6, cy), (cx + r + 6, cy), (255, 255, 255), 1, cv2.LINE_AA)
            cv2.line(img, (cx, cy - r - 6), (cx, cy + r + 6), (255, 255, 255), 1, cv2.LINE_AA)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    scale = max(0.4, h / 1080 * 0.8)
    cv2.putText(img, stamp, (10, h - 12), FONT, scale, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, stamp, (10, h - 12), FONT, scale, (255, 255, 255), 1, cv2.LINE_AA)


def offline_frame(width, height, text="Camera offline"):
    img = np.full((height, width, 3), 24, dtype=np.uint8)
    scale = max(0.6, width / 960)
    (tw, th), _ = cv2.getTextSize(text, FONT, scale, 2)
    cv2.putText(img, text, ((width - tw) // 2, (height + th) // 2), FONT, scale, (170, 170, 170), 2, cv2.LINE_AA)
    return img


def thumb_crop(img, box, out_w=384, out_h=216):
    """16:9 crop zoomed on the object, with context, scaled to a timeline thumbnail."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = box[0] * w, box[1] * h, box[2] * w, box[3] * h
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    ch = min(max((y2 - y1) * 2.2, (x2 - x1) * 2.2 * 9 / 16, h * 0.35), h)
    cw = ch * 16 / 9
    if cw > w:
        cw, ch = w, w * 9 / 16
    x0 = min(max(cx - cw / 2, 0), w - cw)
    y0 = min(max(cy - ch / 2, 0), h - ch)
    crop = img[int(y0):int(y0 + ch), int(x0):int(x0 + cw)]
    return cv2.resize(crop, (out_w, out_h), interpolation=cv2.INTER_AREA)


def write_jpeg(path: Path, img, quality=85):
    """Atomic write, so the browser never sees a half-written image."""
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encode failed")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".jpg")
    with os.fdopen(fd, "wb") as f:
        f.write(buf.tobytes())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
