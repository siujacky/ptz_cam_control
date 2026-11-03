"""YOLO object detection on the Hailo-10H via HailoRT's InferModel API (NMS runs on-chip)."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from .config import CLASS_TO_CATEGORY


class Detection:
    __slots__ = ("box", "score", "cls", "category")

    def __init__(self, box, score: float, cls: int, category: str):
        self.box = box  # np.array [x1, y1, x2, y2], normalized to the frame
        self.score = score
        self.cls = cls
        self.category = category


def _iou(a, b) -> float:
    iw = min(a[2], b[2]) - max(a[0], b[0])
    ih = min(a[3], b[3]) - max(a[1], b[1])
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    return float(inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter))


_KINDS: dict = {}


def hef_kind(path) -> str | None:
    """'detector' (on-chip NMS over the 80 COCO classes), 'enhancer' (image in, same-shape image out),
    'other', or None if HailoRT is not installed. Reads the file's metadata only; no device needed."""
    try:
        from hailo_platform import HEF
    except ImportError:
        return None
    p = Path(path)
    key = (str(p), p.stat().st_mtime_ns)
    if key not in _KINDS:
        hef = HEF(str(p))
        ins, outs = hef.get_input_vstream_infos(), hef.get_output_vstream_infos()
        kind = "other"
        if len(ins) == 1 and len(outs) == 1:
            i, o = ins[0], outs[0]
            if o.format.order.name.startswith("HAILO_NMS") and o.shape[0] == 80:
                kind = "detector"
            elif tuple(o.shape) == tuple(i.shape) and len(i.shape) == 3 and i.shape[2] == 3:
                kind = "enhancer"
        _KINDS[key] = kind
    return _KINDS[key]


class HailoEnhancer:
    """Zero-DCE-family low-light enhancement (Hailo Model Zoo HEF) on the detector's Hailo device.

    The model takes an RGB uint8 image (600x400 for the zoo models) and returns the enhanced image: all
    eight curve iterations run on-chip and the output is quantized with scale 1/255, so its uint8 values
    are the picture. The HailoRT scheduler time-shares the chip with the detector.
    """

    def __init__(self, vdevice, hef_path):
        from hailo_platform import FormatType

        self.name = Path(hef_path).stem
        self.ms = 0.0
        self._model = vdevice.create_infer_model(str(hef_path))
        self._model.set_batch_size(1)
        self._model.input().set_format_type(FormatType.UINT8)
        self._model.output().set_format_type(FormatType.UINT8)
        self.h, self.w = self._model.input().shape[:2]
        self._ctx = self._model.configure()
        self._configured = self._ctx.__enter__()
        self._lanes = threading.local()  # buffers and bindings per calling thread (one per camera)
        try:
            self._lane()
        except Exception:
            self._ctx.__exit__(None, None, None)
            raise

    def _lane(self):
        lane = getattr(self._lanes, "v", None)
        if lane is None:
            inp = np.empty((self.h, self.w, 3), np.uint8)
            out = np.empty(self._model.output().shape, np.uint8)
            lane = self._lanes.v = (inp, out, self._configured.create_bindings(
                output_buffers={self._model.output().name: out}))
        return lane

    def enhance(self, frame: np.ndarray) -> np.ndarray:
        """Whole BGR frame in (any size; squeezed to the model's input), enhanced RGB at model size out.
        The returned array is reused by this thread's next call."""
        inp, out, bindings = self._lane()
        h, w = frame.shape[:2]
        if w >= 2 * self.w * 0.9 and h >= 2 * self.h * 0.8:
            # An exact 2x INTER_AREA averages pixels (less night grain) at a tenth of the cost of a direct
            # non-integer INTER_AREA (9 ms here); the small remaining resize is linear.
            frame = cv2.resize(frame, (w // 2, h // 2), interpolation=cv2.INTER_AREA)
        small = cv2.resize(frame, (self.w, self.h), interpolation=cv2.INTER_LINEAR)
        cv2.cvtColor(small, cv2.COLOR_BGR2RGB, dst=inp)
        bindings.input().set_buffer(inp)
        t0 = time.perf_counter()
        self._configured.run([bindings], 5000)
        ms = (time.perf_counter() - t0) * 1000.0
        self.ms = ms if self.ms == 0 else 0.9 * self.ms + 0.1 * ms
        return out

    def close(self):
        self._ctx.__exit__(None, None, None)


def merge_same_category(dets: list, iou: float = 0.7) -> list:
    """On-chip NMS is per class, so one vehicle can come back as both 'car' and 'truck'.
    Keep only the most confident box among same-category boxes that overlap heavily."""
    kept = []
    for d in sorted(dets, key=lambda d: -d.score):
        if all(k.category != d.category or _iou(k.box, d.box) < iou for k in kept):
            kept.append(d)
    return kept


class HailoDetector:
    def __init__(self, hef_path, min_score: float = 0.2, log=None, enhancer_hef=None):
        from hailo_platform import FormatType, HailoSchedulingAlgorithm, VDevice

        self.log = log or logging.getLogger("ptzcam.detector")
        self.min_score = min_score
        self.model_name = Path(hef_path).stem
        self.infer_ms = 0.0
        self.enhancer = None

        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        self._vdevice = VDevice(params)
        try:
            self._model = self._vdevice.create_infer_model(str(hef_path))
            self._model.set_batch_size(1)
            self._model.input().set_format_type(FormatType.UINT8)
            self._model.output().set_format_type(FormatType.FLOAT32)
            self.input_h, self.input_w = self._model.input().shape[:2]
            self._ctx = self._model.configure()
            self._configured = self._ctx.__enter__()
            # Each calling thread (one per camera) gets its own canvas and bindings: two threads then run the
            # model at once, and the scheduler keeps the chip busy (measured 36.6 fps from one thread, 70.4 from two).
            self._lanes = threading.local()
            self._lane()
        except Exception:
            self._vdevice.release()
            raise
        self.device = self._identify()
        self.log.info("detector ready: %s on %s (input %dx%d)", self.model_name, self.device, self.input_w, self.input_h)
        if enhancer_hef:
            try:  # night vision is optional: detection must work without it
                self.enhancer = HailoEnhancer(self._vdevice, enhancer_hef)
                self.log.info("night vision ready: %s (%dx%d)", self.enhancer.name, self.enhancer.w, self.enhancer.h)
            except Exception as e:
                self.log.error("night vision model %s unavailable: %s", enhancer_hef, e)

    def _identify(self) -> str:
        try:
            devices = self._vdevice.get_physical_devices()  # keep a reference: a temporary is released at once
            arch = devices[0].control.identify().device_architecture
            return getattr(arch, "name", str(arch))
        except Exception:
            return "HAILO"

    def _lane(self):
        lane = getattr(self._lanes, "v", None)
        if lane is None:
            out = np.empty(self._model.output().shape, dtype=np.float32)
            bindings = self._configured.create_bindings(output_buffers={self._model.output().name: out})
            canvas = np.full((self.input_h, self.input_w, 3), 114, dtype=np.uint8)
            lane = self._lanes.v = {"bindings": bindings, "canvas": canvas, "layout": None}
        return lane

    def detect(self, frame: np.ndarray, enhanced: np.ndarray | None = None) -> list:
        """Detections in `frame` (BGR). With `enhanced` (the enhancer's RGB output for this frame), the
        model looks at that instead; it covers the whole frame, so the geometry is the same. Safe to call from
        one thread per camera at the same time."""
        lane = self._lane()
        canvas = lane["canvas"]
        h, w = frame.shape[:2]
        iw, ih = self.input_w, self.input_h
        s = min(iw / w, ih / h)
        nw, nh = int(round(w * s)), int(round(h * s))
        px, py = (iw - nw) // 2, (ih - nh) // 2
        if lane["layout"] != (nw, nh):
            canvas[:] = 114  # letterbox padding only needs repainting when the layout changes
            lane["layout"] = (nw, nh)
        if enhanced is not None:
            canvas[py:py + nh, px:px + nw] = cv2.resize(enhanced, (nw, nh), interpolation=cv2.INTER_LINEAR)
        else:
            resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
            canvas[py:py + nh, px:px + nw] = resized[:, :, ::-1]  # BGR -> RGB
        bindings = lane["bindings"]
        bindings.input().set_buffer(canvas)

        t0 = time.perf_counter()
        self._configured.run([bindings], 5000)
        ms = (time.perf_counter() - t0) * 1000.0
        self.infer_ms = ms if self.infer_ms == 0 else 0.9 * self.infer_ms + 0.1 * ms

        dets = []
        for cls, arr in enumerate(bindings.output().get_buffer()):
            category = CLASS_TO_CATEGORY.get(cls)
            if category is None or len(arr) == 0:
                continue
            for d in arr:
                score = float(d[4])
                if score < self.min_score:
                    continue
                # On-chip NMS emits [ymin, xmin, ymax, xmax] normalized to the letterboxed input.
                box = np.array([
                    (d[1] * iw - px) / s / w, (d[0] * ih - py) / s / h,
                    (d[3] * iw - px) / s / w, (d[2] * ih - py) / s / h,
                ], dtype=np.float64).clip(0.0, 1.0)
                if box[2] - box[0] > 0.002 and box[3] - box[1] > 0.002:
                    dets.append(Detection(box, score, cls, category))
        return merge_same_category(dets)

    def close(self):
        try:
            if self.enhancer is not None:
                self.enhancer.close()
            self._ctx.__exit__(None, None, None)
        finally:
            self._vdevice.release()
