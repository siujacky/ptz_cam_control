"""Night vision in software: deciding when the scene is dark.

The camera has no IR mode, and its brightness setting is only a black-level offset (it adds no exposure),
so night frames are enhanced after capture: Zero-DCE / Zero-DCE++ (Hailo Model Zoo, run on the Hailo-10H
next to the detector, see detector.HailoEnhancer) estimate per-pixel tone curves that lift dark areas
without blowing out lights. Their 600x400 output is simply upscaled for viewing and recording: at night the
frame's own fine detail is mostly sensor noise, and adding it back made the picture grainier, not sharper.
Measured on this camera at night, the enhanced picture also made the detector worse (window-screen mesh and
noise amplified into texture), so detection keeps using the original frames unless `lowlight.detect` is on.
"""
from __future__ import annotations

import cv2
import numpy as np

LUMA_TAU_S = 8.0  # smoothing time constant: a passing headlight must not end night mode
VIEW_SIZE = (712, 400)  # the night picture: the model's 400 rows at 16:9 (upscaling further adds no detail)
WB_STRENGTH = 0.85     # how far the grey-world balance goes (1 = fully neutral grey)
SATURATION = 0.4       # colour kept: enough for lights and cars, little of the amplified colour noise
LEVELS = (0.5, 99.7)   # luma percentiles stretched to black and white...
MAX_STRETCH = 3.0      # ...by at most this factor, so a featureless frame is not blown up into noise
ADAPT = 0.05           # per-frame adaptation of balance and levels (~1.3 s at 15 fps): no flicker
DENOISE_KNEE = 12      # luma change (0-255) at quarter resolution, where grain averages out, treated as motion
DENOISE_KEEP = 0.85    # share of history kept for still pixels (halves frame-to-frame noise on night footage)
_LUMA = np.array([0.299, 0.587, 0.114], np.float32)


class NightView:
    """Turns the model's output into a picture worth watching, at its own small size where this is cheap.

    Zero-DCE lifts the dark scene, together with the street lights' colour cast and the sensor noise, and
    leaves it flat (black becomes grey). Per frame: a partial grey-world white balance and a contrast
    stretch (one lookup table, slowly adapting), most of the colour removed, a light 3x3 blur against the
    amplified speckle and compression blocks, and a motion-adaptive temporal filter that averages still
    areas over a few frames while anything moving is shown as is.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._gains = None
        self._levels = None
        self._prev = None

    def process(self, rgb: np.ndarray) -> np.ndarray:
        """Model output (RGB uint8) in; BGR uint8 at VIEW_SIZE out."""
        sample = rgb[::4, ::4].reshape(-1, 3).astype(np.float32)
        means = sample.mean(axis=0) + 1.0
        gains = np.clip(means.mean() / means, 0.5, 2.0) ** WB_STRENGTH
        luma = np.clip((sample * gains) @ _LUMA, 0, 255).astype(np.uint8)
        cdf = np.cumsum(np.bincount(luma, minlength=256)) / luma.size * 100.0
        lo, hi = float(np.searchsorted(cdf, LEVELS[0])), float(np.searchsorted(cdf, LEVELS[1]))
        hi = max(hi, lo + 255.0 / MAX_STRETCH)
        if self._gains is None:
            self._gains, self._levels = gains, np.array([lo, hi])
        else:
            self._gains += ADAPT * (gains - self._gains)
            self._levels += ADAPT * (np.array([lo, hi]) - self._levels)
        lo, hi = self._levels
        lut = (np.arange(256, dtype=np.float32)[:, None] * self._gains[None, :] - lo) * (255.0 / (hi - lo))
        img = cv2.LUT(rgb, np.clip(lut, 0, 255).astype(np.uint8).reshape(256, 1, 3))
        grey = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        img = cv2.addWeighted(img, SATURATION, cv2.merge((grey, grey, grey)), 1.0 - SATURATION, 0)
        img = cv2.blur(img, (3, 3))  # softens the amplified speckle and compression blocks (1 ms; bilateral: 10)
        prev = self._prev
        if prev is not None and prev.shape == img.shape:
            # Grain (and the stream's compression blocks) average out at quarter resolution; motion does not.
            h, w = img.shape[:2]
            q = (w // 4, h // 4)
            diff = cv2.absdiff(cv2.resize(cv2.cvtColor(img, cv2.COLOR_RGB2GRAY), q, interpolation=cv2.INTER_AREA),
                               cv2.resize(cv2.cvtColor(prev, cv2.COLOR_RGB2GRAY), q, interpolation=cv2.INTER_AREA))
            moving = cv2.resize((diff > DENOISE_KNEE).astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST)
            moving = cv2.dilate(moving, np.ones((9, 9), np.uint8))
            img = cv2.copyTo(img, moving, cv2.addWeighted(prev, DENOISE_KEEP, img, 1.0 - DENOISE_KEEP, 0))
        self._prev = img
        return cv2.cvtColor(cv2.resize(img, VIEW_SIZE, interpolation=cv2.INTER_LINEAR), cv2.COLOR_RGB2BGR)


DAY_LEVELS = (0.5, 99.5)  # luma percentiles of a washed-out picture mapped to DAY_OUT...
DAY_OUT = (12.0, 243.0)   # ...leaving a little headroom, so clipped sky stays plain white
DAY_MIN_BLACK = 40.0      # correct only when even the darkest parts are this bright (haze, sun glare)
DAY_MAX_GAIN = 4.0        # stretch at most this much...
DAY_MIN_GAIN = 1.15       # ...and leave a picture that nearly fills the range alone
BLINDED_SPAN = 12.0       # a picture squeezed into fewer levels than this holds nothing: the camera is blinded


class AutoLevels:
    """Daytime auto-brightness. The camera offers no exposure control, and when the sun shines on its window
    its own exposure gives up: the picture fades towards white, the scene squeezed into the top few levels
    (on 2026-10-31 it went from 1 % to 65 % white between 07:00 and 08:00, then flat white). Stretching the
    levels that still hold the picture across the range brings it back while any detail is left. One lookup
    table for all three channels (no colour shift), adapting like NightView's so the picture does not pump.
    Dim pictures are left alone: there the stretch would only amplify noise, and night vision takes over.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self._levels = None
        self.gain = 1.0
        self.white = 0.0      # share of pure-white pixels
        self.blinded = False  # flat white: nothing left to recover

    def process(self, frame: np.ndarray, correct: bool = True) -> np.ndarray | None:
        """BGR frame in; the corrected frame, or None when it needs no correction or holds nothing (or
        `correct` is off: then the frame is only measured, for the blinded warning)."""
        luma = cv2.cvtColor(cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        self.white = float(np.count_nonzero(luma >= 250)) / luma.size
        levels = np.percentile(luma, DAY_LEVELS).astype(np.float32)
        if self._levels is None:
            self._levels = levels
        else:
            self._levels += ADAPT * (levels - self._levels)
        lo, hi = (float(v) for v in self._levels)
        self.blinded = hi - lo < BLINDED_SPAN
        gain = min(DAY_MAX_GAIN, (DAY_OUT[1] - DAY_OUT[0]) / max(hi - lo, 1.0))
        if self.blinded or lo < DAY_MIN_BLACK or gain < DAY_MIN_GAIN or not correct:
            self.gain = 1.0
            return None
        self.gain = gain
        lut = np.clip((np.arange(256, dtype=np.float32) - lo) * gain + DAY_OUT[0], 0, 255).astype(np.uint8)
        return cv2.LUT(frame, lut)


class LightMeter:
    """Smoothed scene brightness (0-255) with hysteresis between `night_luma` and `day_luma`."""

    def __init__(self):
        self.luma = None
        self.night = False
        self._ts = None

    def update(self, frame, ts: float, cfg: dict) -> bool:
        """Feed one frame; returns whether night vision should be on (config `lowlight` section)."""
        m = float(frame[::8, ::8].mean())  # strided mean over all channels: ~0.1 ms, plenty for exposure
        if self.luma is None:
            self.luma = m
        elif ts > self._ts:
            self.luma += min(1.0, (ts - self._ts) / LUMA_TAU_S) * (m - self.luma)
        self._ts = ts
        if self.night and self.luma > cfg["day_luma"]:
            self.night = False
        elif not self.night and self.luma < cfg["night_luma"]:
            self.night = True
        mode = cfg["mode"]
        return True if mode == "on" else False if mode == "off" else self.night
