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
        self._unfit = False  # washed out or offline, and not clear again since
        self.history = deque(maxlen=50)  # (ts, camera, reason) for the status and the log

    def tick(self, now: float, cfg: dict, ptz: Health, eye: Health | None) -> bool:
        """Re-decide (call about once a second). Returns True when the main camera changed. `reason` always says
        why the main camera is the one it is now; `history` keeps why each switch happened."""
        self._track(now, cfg, ptz)
        want, why = self._decide(now, cfg, ptz, eye)
        if want == self.main:
            self.reason = why
            return False
        offline = "offline" in why
        if not offline and now - self.since < MIN_DWELL_S:  # held for the minimum time on a camera
            self.reason = f"switching to the {'PTZ' if want == 'ptz' else 'wide'} camera shortly"
            return False
        self.main, self.reason, self.since = want, why, now
        self.history.append((now, want, why))
        return True

    @property
    def ptz_clear(self) -> bool:
        """The PTZ picture is clear now (online, not blinded, white well below the washed-out threshold)."""
        return self._clear_since is not None

    @property
    def bad_since(self):
        """When the PTZ picture went white or blinded (still now), or None."""
        times = [t for t in (self._white_since, self._blind_since) if t is not None]
        return min(times) if times else None

    @property
    def ptz_unfit(self) -> bool:
        """The PTZ picture was washed out (or the PTZ offline) and has not been clear since: no photos from it while
        the wide camera is main. A PTZ that was never washed out (e.g. the wide camera set as main) is fit."""
        return self._unfit

    def _track(self, now, cfg, ptz):
        white = ptz.online and ptz.white >= cfg["white"]
        self._white_since = (self._white_since or now) if white else None
        self._blind_since = (self._blind_since or now) if ptz.online and ptz.blinded else None
        clear = ptz.online and not ptz.blinded and ptz.white < cfg["white"] * CLEAR_FACTOR
        self._clear_since = (self._clear_since or now) if clear else None
        if not ptz.online or self.washed_out(now, cfg):
            self._unfit = True
        elif clear:
            self._unfit = False

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
            return "ptz", "PTZ camera"
        if self._clear_since is not None and now - self._clear_since >= cfg["return_after"]:
            return "ptz", "PTZ picture clear again"
        if eye.blinded and not ptz.blinded:  # both washed out: the eye was no better after all
            return "ptz", "wide camera washed out too"
        if self._clear_since is not None:
            return "eye", "PTZ picture clearing up"
        if ptz.blinded or ptz.white >= cfg["white"]:
            return "eye", "PTZ picture washed out"
        return "eye", "PTZ picture not clear yet"
