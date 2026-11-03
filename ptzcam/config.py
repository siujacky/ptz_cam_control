"""Configuration: defaults, validation, persistence and category metadata."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path

CATEGORIES = ("person", "vehicle", "bicycle", "motorcycle", "animal")
# Highest first; decides an event's top_category.
CATEGORY_PRIORITY = ("person", "animal", "motorcycle", "bicycle", "vehicle")
CATEGORY_COLORS = {
    "person": "#4F9DFF",
    "vehicle": "#FFB020",
    "bicycle": "#2ED47A",
    "motorcycle": "#FF5C7A",
    "animal": "#B57BFF",
}
CATEGORY_BGR = {k: (int(v[5:7], 16), int(v[3:5], 16), int(v[1:3], 16)) for k, v in CATEGORY_COLORS.items()}

COCO_LABELS = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
    "teddy bear", "hair drier", "toothbrush",
)
CLASS_TO_CATEGORY = {
    0: "person", 1: "bicycle", 2: "vehicle", 3: "motorcycle", 5: "vehicle", 7: "vehicle",
    **{c: "animal" for c in range(14, 24)},
}

DEFAULTS = {
    "camera": {
        "name": "Front", "rtsp_url": "rtsp://192.168.1.87:554/1", "http_url": "http://192.168.1.87",
        "username": "admin", "password": "admin",
        "visca_host": "192.168.1.87", "visca_port": 5678, "visca_address": 1,
        # The PTZ camera's own USB link, if connected: its anti-flicker (power_line_frequency) is put back to this
        # whenever the camera reconnects, because a camera restart resets it. -1 = leave it, 0 off, 1 50 Hz, 2 60 Hz.
        "usb_device": "", "anti_flicker": -1,
    },
    "eye": {  # a second, fixed USB camera beside the PTZ camera (see README, "Two cameras")
        "enabled": False, "name": "Wide", "device": "Avaya HC020",
        "width": 1920, "height": 1080, "fps": 15, "anti_flicker": -1,
        "zone": [],      # counting zone on the eye's picture; empty = the PTZ Home view's area once synced
        "count": True,   # count on the eye: a fixed view never pauses while the PTZ follows
        "follow": True,  # the eye picks targets and aims the PTZ (needs data/sync.json, tools/calibrate_sync.py)
        "clips": "auto",  # keep a wide clip per event: auto (only when the PTZ clip does not show the whole scene),
                          # always, or never
    },
    "failover": {  # which camera the live view, photos and clips come from
        "mode": "auto",        # auto: the PTZ, unless it is washed out or offline; or always "ptz" / "eye"
        "white": 0.5,          # the PTZ counts as washed out with this share of its picture burnt white...
        "switch_after": 20.0,  # ...for this long (seconds), or as soon as it is blinded or offline
        "return_after": 120.0,  # and is main again once clear for this long
    },
    "detect": {
        "model": "models/yolov11m.hef", "fps": 15, "width": 1280, "height": 720,
        "min_move": 0.5, "min_hits": 4, "zone": [],
        "categories": {
            "person": {"enabled": True, "min_score": 0.5},
            "vehicle": {"enabled": True, "min_score": 0.5},
            "bicycle": {"enabled": True, "min_score": 0.45},
            "motorcycle": {"enabled": True, "min_score": 0.45},
            "animal": {"enabled": True, "min_score": 0.45},
        },
    },
    "events": {"clip_seconds": 30, "preroll_seconds": 5},
    "storage": {
        "data_dir": "data", "segment_dir": "/dev/shm/ptzcam", "segment_seconds": 2,
        "buffer_seconds": 120, "retention_days": 30, "max_storage_gb": 50,
    },
    "ptz": {
        "enabled": True, "autotrack": False, "autotrack_categories": list(CATEGORIES),
        "deadzone": 0.12, "max_pan_speed": 12, "max_tilt_speed": 10, "return_home_seconds": 15,
        "home_preset": 0, "settle_seconds": 2.0, "autozoom": True, "target_size": 0.35,
        "max_zoom_pos": 8192, "autofocus": True,
        # The lens, measured by tools/calibrate_zoom.py: [[zoom position, magnification], ...] and the
        # zoom-direct speed in positions per second. Empty / 0 = not measured (an assumed model is used).
        "zoom_curve": [], "zoom_rate": 0.0,
        "pt_rate": 0.0,  # absolute pan/tilt speed at full speed, units per second; 0 = an estimate
    },
    "lowlight": {  # night vision: Zero-DCE on the Hailo when the scene is dark (see lowlight.py)
        "mode": "auto", "model": "models/lowlight/zero_dce.hef", "detect": False, "save_clips": True,
        "night_luma": 45, "day_luma": 70,
    },
    "brightness": {  # daytime auto-brightness for washed-out pictures (sun on the window, see lowlight.AutoLevels)
        "auto": True, "detect": True,
    },
    "web": {
        "host": "0.0.0.0", "port": 8080, "live_width": 960, "live_fps": 12,
        "auth_user": "", "auth_password": "",
        "allowed_hosts": [],  # extra host names allowed besides IPs, bare and .local/.lan names
    },
}

# Changing any of these rebuilds the capture/detection pipeline.
RESTART_KEYS = {
    ("camera", "rtsp_url"), ("detect", "model"), ("detect", "fps"), ("detect", "width"),
    ("detect", "height"), ("storage", "segment_dir"), ("storage", "segment_seconds"),
    ("storage", "buffer_seconds"), ("lowlight", "model"), ("eye", "enabled"), ("eye", "device"), ("eye", "width"),
    ("eye", "height"), ("eye", "fps"),
}
LOWLIGHT_MODES = ("auto", "on", "off")
FAILOVER_MODES = ("auto", "ptz", "eye")
WIDE_CLIPS = ("auto", "always", "never")

_NUM = {
    ("camera", "visca_port"): (int, 1, 65535),
    ("camera", "visca_address"): (int, 1, 7),
    ("camera", "anti_flicker"): (int, -1, 2),
    ("eye", "width"): (int, 320, 1920),  # 4K cannot be decoded in software on a Pi 5
    ("eye", "height"): (int, 180, 1080),
    ("eye", "fps"): (int, 5, 30),
    ("eye", "anti_flicker"): (int, -1, 2),
    ("failover", "white"): (float, 0.05, 1.0),
    ("failover", "switch_after"): (float, 0.0, 3600.0),
    ("failover", "return_after"): (float, 0.0, 86400.0),
    ("ptz", "pt_rate"): (float, 0.0, 1e5),
    ("detect", "fps"): (int, 1, 30),
    ("detect", "width"): (int, 320, 1920),
    ("detect", "height"): (int, 180, 1080),
    ("detect", "min_move"): (float, 0.05, 5.0),
    ("detect", "min_hits"): (int, 1, 30),
    ("events", "clip_seconds"): (int, 5, 120),
    ("events", "preroll_seconds"): (int, 0, 30),
    ("storage", "segment_seconds"): (int, 1, 10),
    ("storage", "buffer_seconds"): (int, 30, 1800),
    ("storage", "retention_days"): (int, 1, 3650),
    ("storage", "max_storage_gb"): (float, 0.5, 10000.0),
    ("ptz", "deadzone"): (float, 0.0, 0.45),
    ("ptz", "max_pan_speed"): (int, 1, 24),
    ("ptz", "max_tilt_speed"): (int, 1, 20),
    ("ptz", "return_home_seconds"): (float, 0.0, 3600.0),
    ("ptz", "home_preset"): (int, 0, 254),
    ("ptz", "settle_seconds"): (float, 0.0, 30.0),
    ("ptz", "target_size"): (float, 0.05, 0.9),
    ("ptz", "max_zoom_pos"): (int, 0, 65535),
    ("ptz", "zoom_rate"): (float, 0.0, 1e6),
    ("web", "port"): (int, 1, 65535),
    ("web", "live_width"): (int, 160, 1920),
    ("web", "live_fps"): (int, 1, 30),
    ("lowlight", "night_luma"): (float, 1.0, 200.0),
    ("lowlight", "day_luma"): (float, 2.0, 250.0),
}
_BOOL = {("ptz", "enabled"), ("ptz", "autotrack"), ("ptz", "autozoom"), ("ptz", "autofocus"),
         ("lowlight", "detect"), ("lowlight", "save_clips"), ("brightness", "auto"), ("brightness", "detect"),
         ("eye", "enabled"), ("eye", "count"), ("eye", "follow")}
_STR = {
    ("camera", "name"), ("camera", "rtsp_url"), ("camera", "http_url"), ("camera", "username"),
    ("camera", "password"), ("camera", "visca_host"), ("camera", "usb_device"), ("detect", "model"),
    ("storage", "data_dir"), ("storage", "segment_dir"), ("web", "host"), ("web", "auth_user"),
    ("web", "auth_password"), ("lowlight", "mode"), ("lowlight", "model"), ("eye", "name"), ("eye", "device"),
    ("eye", "clips"), ("failover", "mode"),
}


class ConfigError(ValueError):
    pass


def deep_merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def check_known(patch, ref=DEFAULTS, path=()):
    """Reject keys that do not exist in the defaults (catches typos)."""
    if not isinstance(patch, dict):
        raise ConfigError(f"{'.'.join(path) or 'config'} must be an object")
    for k, v in patch.items():
        if k not in ref:
            raise ConfigError("unknown setting: " + ".".join(path + (k,)))
        if isinstance(ref[k], dict):
            check_known(v, ref[k], path + (k,))


def _strip_unknown(cfg, ref=DEFAULTS):
    """Drop keys from an older config file that no longer exist."""
    return {k: (_strip_unknown(v, ref[k]) if isinstance(ref[k], dict) and isinstance(v, dict) else v)
            for k, v in cfg.items() if k in ref}


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _polygon(zone, name: str) -> list:
    if not isinstance(zone, list) or len(zone) > 64 or 0 < len(zone) < 3:
        raise ConfigError(f"{name} must be empty or a polygon of 3 to 64 points")
    points = []
    for p in zone:
        if not (isinstance(p, (list, tuple)) and len(p) == 2 and all(_is_num(v) and 0 <= v <= 1 for v in p)):
            raise ConfigError(f"{name} points must be [x, y] pairs between 0 and 1")
        points.append([float(p[0]), float(p[1])])
    return points


def validate(cfg: dict, base_dir: Path, check_paths: bool = False) -> dict:
    cfg = copy.deepcopy(cfg)
    for (sec, key), (typ, lo, hi) in _NUM.items():
        v = cfg[sec][key]
        if not _is_num(v):
            raise ConfigError(f"{sec}.{key} must be a number")
        v = int(round(v)) if typ is int else float(v)
        if not lo <= v <= hi:
            raise ConfigError(f"{sec}.{key} must be between {lo} and {hi}")
        cfg[sec][key] = v
    if cfg["events"]["preroll_seconds"] >= cfg["events"]["clip_seconds"]:
        raise ConfigError("events.preroll_seconds must be shorter than events.clip_seconds")
    for sec, key in _BOOL:
        if not isinstance(cfg[sec][key], bool):
            raise ConfigError(f"{sec}.{key} must be true or false")
    for sec, key in _STR:
        if not isinstance(cfg[sec][key], str):
            raise ConfigError(f"{sec}.{key} must be text")
    if not cfg["camera"]["rtsp_url"].startswith(("rtsp://", "rtsps://")):
        raise ConfigError("camera.rtsp_url must start with rtsp://")

    models_dir = (base_dir / "models").resolve()
    model = (base_dir / cfg["detect"]["model"]).resolve()
    if model.suffix != ".hef" or model.parent != models_dir or not model.is_file():
        raise ConfigError("detect.model must be a .hef file inside models/")
    ll = cfg["lowlight"]
    if ll["mode"] not in LOWLIGHT_MODES:
        raise ConfigError("lowlight.mode must be auto, on or off")
    if ll["night_luma"] >= ll["day_luma"]:
        raise ConfigError("lowlight.night_luma must be below lowlight.day_luma")
    enhancer = (base_dir / ll["model"]).resolve() if ll["model"] else None
    if enhancer is not None and (enhancer.suffix != ".hef" or models_dir not in enhancer.parents
                                 or not enhancer.is_file()):
        raise ConfigError("lowlight.model must be empty or a .hef file inside models/")

    for name in CATEGORIES:
        c = cfg["detect"]["categories"][name]
        if not isinstance(c.get("enabled"), bool):
            raise ConfigError(f"detect.categories.{name}.enabled must be true or false")
        if not _is_num(c.get("min_score")) or not 0.05 <= c["min_score"] <= 0.99:
            raise ConfigError(f"detect.categories.{name}.min_score must be between 0.05 and 0.99")
        c["min_score"] = float(c["min_score"])

    for sec in ("detect", "eye"):
        cfg[sec]["zone"] = _polygon(cfg[sec]["zone"], f"{sec}.zone")
    if cfg["eye"]["clips"] not in WIDE_CLIPS:
        raise ConfigError("eye.clips must be auto, always or never")
    if cfg["failover"]["mode"] not in FAILOVER_MODES:
        raise ConfigError("failover.mode must be auto, ptz or eye")
    if cfg["eye"]["enabled"] and not cfg["eye"]["device"].strip():
        raise ConfigError("eye.device must name the USB camera")

    hosts = cfg["web"]["allowed_hosts"]
    if not isinstance(hosts, list) or any(not isinstance(h, str) or not h.strip() for h in hosts):
        raise ConfigError("web.allowed_hosts must be a list of host names")
    cfg["web"]["allowed_hosts"] = [h.strip().lower() for h in hosts]

    if check_paths:  # only for changes: a bad path must not stop an already-configured service starting
        seg = Path(cfg["storage"]["segment_dir"])
        probe = seg if seg.exists() else seg.parent
        if not seg.is_absolute() or not os.access(probe, os.W_OK):
            raise ConfigError("storage.segment_dir must be an absolute, writable path")
        # A model of the wrong kind loads fine and then fails on every frame, so check what each one is.
        from .detector import hef_kind
        for key, path, want in (("detect.model", model, "detector"), ("lowlight.model", enhancer, "enhancer")):
            try:
                kind = hef_kind(path) if path is not None else want
            except Exception as e:
                raise ConfigError(f"{key}: cannot read {path.name} ({e})") from e
            if kind not in (want, None):
                raise ConfigError(f"{key}: {path.name} is not a {'detection' if want == 'detector' else 'low-light'} model")

    curve = cfg["ptz"]["zoom_curve"]
    ok = isinstance(curve, list) and (not curve or len(curve) >= 2) and all(
        isinstance(p, (list, tuple)) and len(p) == 2 and _is_num(p[0]) and _is_num(p[1]) for p in curve)
    if ok and curve:
        ok = curve[0][1] >= 1.0 and all(0 <= p[0] <= 0x4000 for p in curve) and all(
            b[0] > a[0] and b[1] > a[1] for a, b in zip(curve, curve[1:]))
    if not ok:
        raise ConfigError("ptz.zoom_curve must be empty or [position, magnification] pairs, both increasing")
    cfg["ptz"]["zoom_curve"] = [[int(p[0]), float(p[1])] for p in curve]

    cats = cfg["ptz"]["autotrack_categories"]
    if not isinstance(cats, list) or any(c not in CATEGORIES for c in cats):
        raise ConfigError("ptz.autotrack_categories must list known categories")
    cfg["ptz"]["autotrack_categories"] = list(dict.fromkeys(cats))
    return cfg


def diff_paths(old: dict, new: dict, path=()) -> set:
    changed = set()
    for k in set(old) | set(new):
        a, b = old.get(k), new.get(k)
        if isinstance(a, dict) and isinstance(b, dict):
            changed |= diff_paths(a, b, path + (k,))
        elif a != b:
            changed.add(path + (k,))
    return changed


def needs_restart(changed: set) -> bool:
    return any(p[:2] in RESTART_KEYS for p in changed)


class ConfigStore:
    """Holds the live config. `data` is replaced atomically, so readers never need a lock.

    `overrides` (from the command line) apply at runtime but are never written to disk.
    """

    def __init__(self, path, overrides: dict | None = None, base_dir=None):
        self.path = Path(path).resolve()
        self.base_dir = Path(base_dir).resolve() if base_dir else self.path.parent
        self._overrides = overrides or {}
        self._lock = threading.Lock()
        file_cfg = {}
        if self.path.exists():
            file_cfg = _strip_unknown(json.loads(self.path.read_text()))
        self._file = validate(deep_merge(DEFAULTS, file_cfg), self.base_dir)
        self.data = deep_merge(self._file, self._overrides)
        if not self.path.exists():
            self._save(self._file)

    def update(self, patch: dict) -> set:
        """Deep-merge `patch`, validate, persist. Returns the set of changed key paths."""
        with self._lock:
            check_known(patch)
            new_file = validate(deep_merge(self._file, patch), self.base_dir, check_paths=True)
            changed = diff_paths(self._file, new_file)
            if changed:
                self._save(new_file)  # persist first: a failed write must leave memory unchanged
                self._file = new_file
                self.data = deep_merge(new_file, self._overrides)
            return changed

    def snapshot(self) -> dict:
        return copy.deepcopy(self._file)

    def restore(self, file_cfg: dict):
        """Put back a snapshot (used when new settings could not be applied)."""
        with self._lock:
            self._save(file_cfg)
            self._file = copy.deepcopy(file_cfg)
            self.data = deep_merge(self._file, self._overrides)

    def resolve(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.base_dir / p

    def _save(self, file_cfg: dict):
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".config.", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(file_cfg, f, indent=2)
                f.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
