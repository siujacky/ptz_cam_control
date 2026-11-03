"""Which camera is main: the live view, the event photos and the default clip come from it (config "failover").

The PTZ camera is main. It hands over to the eye (the fixed USB camera) when its picture is washed out (the sun on
the window: blinded for a few seconds, or mostly white for `switch_after` seconds) or when it goes offline, and
gets the job back once its picture has been clear for `return_after` seconds. Passing clouds do not flap it: one
clear moment restarts the washed-out timer, and a switch is held for at least MIN_DWELL_S unless a camera dies.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

BLINDED_FOR_S = 3.0     # a blinded (flat white) picture this long is washed out at once
MIN_DWELL_S = 30.0      # stay on a camera at least this long, except to leave one that went offline
CLEAR_FACTOR = 0.5      # "clear again" means white below half the washed-out threshold
EYE_MARGIN = 0.15       # the eye must be at least this much less white to be worth switching to


@dataclass
class Health:
    online: bool
    white: float = 0.0  # share of the picture burnt white, smoothed (0..1)
    blinded: bool = False


class MainSelector:
    def __init__(self):
        self.main, self.reason, self.since = "ptz", "PTZ camera", 0.0
        self._white_since = self._blind_since = self._clear_since = None
        self.history = deque(maxlen=50)  # (ts, camera, reason) for the status and the log

    def tick(self, now: float, cfg: dict, ptz: Health, eye: Health | None) -> bool:
        """Re-decide (call about once a second). Returns True when the main camera changed."""
        self._track(now, cfg, ptz)
        want, why = self._decide(now, cfg, ptz, eye)
        if want == self.main:
            if why:
                self.reason = why
            return False
        offline = "offline" in why
        if not offline and now - self.since < MIN_DWELL_S:
            return False
        self.main, self.reason, self.since = want, why, now
        self.history.append((now, want, why))
        return True

    def _track(self, now, cfg, ptz):
        white = ptz.online and ptz.white >= cfg["white"]
        self._white_since = (self._white_since or now) if white else None
        self._blind_since = (self._blind_since or now) if ptz.online and ptz.blinded else None
        clear = ptz.online and not ptz.blinded and ptz.white < cfg["white"] * CLEAR_FACTOR
        self._clear_since = (self._clear_since or now) if clear else None

    def washed_out(self, now: float, cfg: dict) -> bool:
        return (self._blind_since is not None and now - self._blind_since >= BLINDED_FOR_S) or \
            (self._white_since is not None and now - self._white_since >= cfg["switch_after"])

    def _decide(self, now, cfg, ptz: Health, eye: Health | None):
        if eye is None:
            return "ptz", "PTZ camera"
        if not eye.online:
            return "ptz", "wide camera offline"
        if not ptz.online:
            return "eye", "PTZ camera offline"
        mode = cfg["mode"]
        if mode in ("ptz", "eye"):
            return mode, "set to the " + ("PTZ" if mode == "ptz" else "wide") + " camera"
        eye_better = not eye.blinded and eye.white + EYE_MARGIN < ptz.white
        if self.main == "ptz":
            if self.washed_out(now, cfg) and eye_better:
                return "eye", "PTZ picture washed out"
            return "ptz", None
        if self._clear_since is not None and now - self._clear_since >= cfg["return_after"]:
            return "ptz", "PTZ picture clear again"
        if eye.blinded and not ptz.blinded:  # both washed out: the eye was no better after all
            return "ptz", "wide camera washed out too"
        return "eye", None
