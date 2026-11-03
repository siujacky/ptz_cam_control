"""PTZ control over VISCA-over-TCP (HTTP CGI fallback): a motion watchdog, autofocus after moves,
and auto-follow with zoom.

Callers never block on the network: they set the desired motion and a worker thread sends VISCA
commands, enforces watchdog deadlines (a move that is not refreshed stops), polls positions and
refocuses once the camera has settled.
"""
from __future__ import annotations

import base64
import bisect
import logging
import math
import socket
import threading
import time
import urllib.request
from collections import deque

import numpy as np

PAN_MAX, TILT_MAX = 0x18, 0x14
STOP = (1, 1, 3, 3)  # pan speed, tilt speed, pan dir, tilt dir (3 = stop)
PRESET_TRAVEL_S = 3.0  # assume the camera is moving this long after a preset/home recall
HOME_SLOT = 200  # camera preset slot that stores the user's own Home view
AF_SETTLE_S = 0.6  # refocus this long after the camera stops...
AF_MIN_GAP_S = 3.0  # ...but never more often than this (auto-follow starts and stops a lot)
AF_COMMANDS = (b"\x01\x04\x18\x01", b"\x01\x04\x38\x02")  # one-push AF trigger; else continuous AF
HOME_CHECK_TIMEOUT_S = 10.0
ZOOM_RANGE = 0x4000      # VISCA zoom positions run 0 (wide) to this (full optical zoom)
ZOOM_RATIO = 12.0        # assumed magnification at full zoom, growing exponentially, until the lens is measured
ZOOM_RATE = 8000.0       # assumed zoom-direct speed (positions per second) until measured
ZOOM_ARRIVED = 64        # a zoom position this close to the goal has arrived
ZOOM_GOAL_TIMEOUT_S = 4.0
PT_RATE = 250.0          # pan/tilt units per second at full absolute-move speed (300 units took 1.7 s at 2/3 speed)
PT_ARRIVED = 4           # a pan/tilt position this close to the goal has arrived
PT_GOAL_SLACK_S = 2.0    # give up waiting for arrival this long after the predicted travel time
PT_RETRY_S = 0.3         # an absolute move the camera could not take yet (busy) is retried after this

FOLLOW_WEIGHT = {"person": 3.0, "animal": 2.5, "motorcycle": 2.0, "bicycle": 2.0, "vehicle": 1.0}
# A rider and their bike can be detected as either; losing one and finding the other close by is the same target.
RIDER = {"person", "bicycle", "motorcycle"}
# Slow and few: worth coasting after and re-finding as a new detection. While the camera pans, every parked car
# that comes into view is a new detection too, so a lost vehicle is only re-found among things seen moving.
SLOW = {"person", "bicycle", "animal"}
# Track ids of the eye camera's tracker start here, so one id never means two objects (API, UI, auto-follow). The PTZ
# tracker uses a few thousand ids a day, so a smaller base (it was 1,000,000) is reached within months of uptime.
EYE_ID_BASE = 1 << 40


class ViscaError(Exception):
    pass


def _nibbles(value: int) -> bytes:
    return bytes([(value >> 12) & 0xF, (value >> 8) & 0xF, (value >> 4) & 0xF, value & 0xF])


def _from_nibbles(b: bytes) -> int:
    v = 0
    for x in b:
        v = (v << 4) | (x & 0x0F)
    return v


def _s16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def _interp(x: float, xs: list, ys: list) -> float:
    """Piecewise-linear interpolation, clamped at both ends (xs increasing)."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect.bisect_right(xs, x)
    return ys[i - 1] + (ys[i] - ys[i - 1]) * (x - xs[i - 1]) / (xs[i] - xs[i - 1])


def mag_at(pos: float, curve: list) -> float:
    """Magnification at a VISCA zoom position, relative to fully wide: from the measured lens curve
    (`ptz.zoom_curve`, interpolated in log-magnification), or the assumed model while it is empty."""
    if not curve:
        return ZOOM_RATIO ** (min(max(pos, 0.0), ZOOM_RANGE) / ZOOM_RANGE)
    return math.exp(_interp(pos, [p for p, _ in curve], [math.log(m) for _, m in curve]))


def pos_for(mag: float, curve: list) -> int:
    """The zoom position giving `mag` (the inverse of mag_at), clamped to what the lens can do."""
    lm = math.log(max(mag, 1e-6))
    if not curve:
        return round(min(max(lm / math.log(ZOOM_RATIO), 0.0), 1.0) * ZOOM_RANGE)
    return round(_interp(lm, [math.log(m) for _, m in curve], [p for p, _ in curve]))


class ViscaClient:
    """One persistent TCP connection; sends a packet and waits for its completion or inquiry reply."""

    def __init__(self, host: str, port: int, address: int = 1, timeout: float = 1.0):
        self.host, self.port, self.address, self.timeout = host, port, address, timeout
        self._sock = None
        self._buf = b""

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock, self._buf = None, b""

    def send(self, body: bytes):
        """Send one packet; return its completion (commands) or data reply (inquiries), or None if the camera
        stayed silent (it does not answer a stop while idle). Replies are matched to this packet: a command's
        completion or error carries the socket number of its own ACK (this camera ACKs and completes within
        ~1 ms), and inquiry answers come on socket 0, so a late reply to an earlier packet is skipped."""
        inquiry = body[:1] == b"\x09"
        for attempt in range(2):
            try:
                if self._sock is None:
                    self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                    self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self._drain()
                self._sock.sendall(bytes([0x80 | self.address]) + body + b"\xff")
                deadline = time.monotonic() + self.timeout
                acked = None  # socket number from this command's ACK
                while True:
                    msg = self._read(deadline)
                    if msg is None:
                        return None
                    if len(msg) < 3:
                        continue
                    kind, sock = msg[1] & 0xF0, msg[1] & 0x0F
                    if kind == 0x60 and (sock == 0 or sock == acked):
                        raise ViscaError(f"camera replied error {msg.hex(' ')}")
                    if inquiry:
                        if kind == 0x50 and sock == 0 and len(msg) > 3:
                            return msg
                    elif kind == 0x40 and acked is None:
                        acked = sock
                    elif kind == 0x50 and sock == acked:
                        return msg
            except (OSError, ConnectionError):
                self.close()
                if attempt:
                    raise

    def _read(self, deadline):
        while b"\xff" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(256)
            except socket.timeout:
                return None
            if not chunk:
                raise ConnectionError("VISCA connection closed")
            self._buf += chunk
        msg, _, self._buf = self._buf.partition(b"\xff")
        return msg + b"\xff"

    def _drain(self):
        """Discard stale replies so they are not mistaken for the next answer."""
        self._buf = b""
        self._sock.setblocking(False)
        try:
            while self._sock.recv(256):
                pass
        except (BlockingIOError, InterruptedError):
            pass
        finally:
            self._sock.setblocking(True)


class PTZController:
    def __init__(self, cfg_store, log=None):
        self.cfg = cfg_store
        self.log = log or logging.getLogger("ptzcam.ptz")
        self._cond = threading.Condition()
        self._want_pt = STOP
        self._sent_pt = None
        self._want_zoom = 0
        self._sent_zoom = None
        self._pt_deadline = 0.0
        self._zoom_deadline = 0.0
        self._oneshots = deque()
        self._visca = self._make_client()
        self._visca_ok = True
        self._visca_retry = 0.0
        self._next_poll = 0.0
        self._home_checked = False
        self._af_due = 0.0  # monotonic time an autofocus is due (0 = none pending)
        self._af_last = 0.0
        self._af_cmd = None  # the AF command this camera accepted
        self._af_supported = True
        self.connected = False
        self.zoom_pos = None
        self.pan_pos = None
        self.tilt_pos = None
        self.zoom_goal = None  # the last absolute zoom sent (zoom_to)
        self._zoom_goal_t = 0.0
        self._want_abs = None  # (pan, tilt, vv, ww): the newest absolute pan/tilt goal, replaced, never queued
        self._sent_abs = None  # the last one the camera took (cleared from _want_abs once sent, so never replayed)
        self._abs_stop = False  # a cancelled absolute move may still be travelling: the worker sends a pan/tilt stop
        self._abs_gen = 0  # moves on at every cancel: a send that a cancel overtook does not count as sent
        self._abs_retry = 0.0
        self._abs_supported = True
        self.pt_goal = None  # the last absolute pan/tilt goal (pan_tilt_to)
        self._pt_goal_t = 0.0
        self._pt_from = None  # pose when that goal was set, for pose_estimate()
        self._pt_travel = 0.0
        self.home_zoom = None  # zoom position read while resting at Home
        self.home_pan = self.home_tilt = None
        self.at_home = False  # unknown until the startup check (_check_home)
        self.last_motion_ts = 0.0
        self.motion_until = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ptz", daemon=True)

    def _make_client(self):
        c = self.cfg.data["camera"]
        return ViscaClient(c["visca_host"], c["visca_port"], c["visca_address"])

    @property
    def enabled(self) -> bool:
        return self.cfg.data["ptz"]["enabled"]

    def start(self):
        self._thread.start()

    def close(self):
        self._stop.set()
        with self._cond:
            self._cond.notify()
        if self._thread.is_alive():
            self._thread.join(3)
        if self.enabled or self._sent_pt not in (None, STOP) or self._sent_zoom or self._abs_stop \
                or not self.pt_arrived:
            try:
                self._send_pt(STOP)
                self._send_zoom(0)
            except Exception:
                pass
        self._visca.close()

    def reconfigure(self):
        """Camera address changed: reconnect with the new settings."""
        with self._cond:
            self._enqueue("reconnect")
            self._cond.notify()

    # ---- public API (non-blocking) ----
    def move(self, pan: float, tilt: float, speed: float = 0.5, hold: float = 1.0):
        """pan/tilt in -1..1 (+pan = right, +tilt = up); speed scales both. Stops after `hold` s unless refreshed."""
        if not self.enabled:
            return
        speed = min(max(speed, 0.0), 1.0)
        pd = 1 if pan < -1e-3 else 2 if pan > 1e-3 else 3
        td = 1 if tilt > 1e-3 else 2 if tilt < -1e-3 else 3
        vv = min(PAN_MAX, max(1, round(abs(pan) * speed * PAN_MAX)))
        ww = min(TILT_MAX, max(1, round(abs(tilt) * speed * TILT_MAX)))
        with self._cond:
            if pd == 3 and td == 3:
                self._want_pt = STOP
            else:
                self._cancel_abs()  # a drive takes over from an absolute move on the camera too
                self._want_pt = (vv, ww, pd, td)
                self._pt_deadline = time.monotonic() + hold
                self.at_home = False
                self.last_motion_ts = time.time()
            self._cond.notify()

    def stop_pantilt(self):
        with self._cond:
            self._cancel_abs(stop_camera=True)
            self._want_pt = STOP
            self._cond.notify()

    def stop(self):
        with self._cond:
            self._cancel_abs(stop_camera=True)
            self._want_pt = STOP
            self._want_zoom = 0
            self._cond.notify()

    def _cancel_abs(self, stop_camera: bool = False):
        """Drop any absolute pan/tilt goal (call with the lock held). `stop_camera`: one may still be travelling on
        the camera, so make the worker send a pan/tilt stop even though no drive was sent. The request is a flag of
        its own, so a goal the worker is sending at this very moment cannot swallow it, and the generation moves
        on, so that send does not count as done. Whether a goal may be travelling is judged by the goal itself:
        a worker error forgets what was sent, not what is under way."""
        if stop_camera and self.pt_goal is not None and not self.pt_arrived:
            self._abs_stop = True
        self._want_abs = self._sent_abs = None
        self.pt_goal = None
        self._abs_gen += 1

    def pan_tilt_to(self, pan: int, tilt: int, speed: float = 1.0) -> bool:
        """Absolute pan/tilt (VISCA 01 06 02): the camera drives there itself and stops. A newer goal replaces one not
        yet sent, so a stream of goals never queues. Returns False when this camera or link cannot do it."""
        if not self.can_point:
            return False
        s = min(max(speed, 0.05), 1.0)
        vv, ww = max(1, round(s * PAN_MAX)), max(1, round(s * TILT_MAX))
        rate = (self.cfg.data["ptz"].get("pt_rate") or PT_RATE) * s
        with self._cond:
            cur = self.pose_estimate()
            travel = max(abs(pan - cur[0]), abs(tilt - cur[1])) / rate + 0.3 if cur else 1.5
            self._want_pt = STOP
            self._want_abs = (int(pan), int(tilt), vv, ww)
            self.pt_goal, self._pt_goal_t = (int(pan), int(tilt)), time.monotonic()
            self._pt_from, self._pt_travel = cur, travel
            self._next_poll = 0.0  # an idle camera polls every 10 s: start watching for arrival now
            self.at_home = False
            self.last_motion_ts = time.time()
            self.motion_until = max(self.motion_until, time.time() + travel)  # counting on this camera pauses
            self._cond.notify()
        return True

    @property
    def can_point(self) -> bool:
        """Absolute pan/tilt is possible now: VISCA is up, the camera accepts it, and the pose is known."""
        return self.enabled and self._visca_ok and self._abs_supported and self.pan_pos is not None

    @property
    def pt_arrived(self) -> bool:
        """The last pan_tilt_to has finished: the pose reads back at the goal, or it is overdue."""
        # Read each shared field once: other threads clear the goal (stop, home) and the positions (VISCA lost).
        goal, t0, travel, pan, tilt = self.pt_goal, self._pt_goal_t, self._pt_travel, self.pan_pos, self.tilt_pos
        return self._arrived(goal, t0, travel, pan, tilt)

    @staticmethod
    def _arrived(goal, t0, travel, pan, tilt) -> bool:
        if goal is None or time.monotonic() - t0 > travel + PT_GOAL_SLACK_S:
            return True
        return pan is not None and tilt is not None and abs(pan - goal[0]) <= PT_ARRIVED \
            and abs(tilt - goal[1]) <= PT_ARRIVED

    def pose_estimate(self):
        """Best guess of (pan, tilt) now. Position readings lag about a second while moving, so during an absolute
        move this interpolates from the pose at the command to the goal over the predicted travel time."""
        goal, t0, travel, start = self.pt_goal, self._pt_goal_t, self._pt_travel, self._pt_from
        pan, tilt = self.pan_pos, self.tilt_pos
        if goal is not None and start is not None and not self._arrived(goal, t0, travel, pan, tilt):
            f = min(1.0, (time.monotonic() - t0) / max(travel, 1e-3))
            return start[0] + (goal[0] - start[0]) * f, start[1] + (goal[1] - start[1]) * f
        if pan is None or tilt is None:
            return None
        return float(pan), float(tilt)

    def zoom(self, direction: str, speed: float = 0.5, hold: float = 1.0):
        if not self.enabled:
            return
        p = min(7, max(0, round(min(max(speed, 0.0), 1.0) * 7)))
        byte = {"in": 0x20 | p, "out": 0x30 | p}.get(direction, 0)
        with self._cond:
            self._want_zoom = byte
            if byte:
                self._zoom_deadline = time.monotonic() + hold
                self.at_home = False
                self.last_motion_ts = time.time()
            self._cond.notify()

    def zoom_to(self, pos: int):
        """Absolute zoom (VISCA zoom direct): the camera drives to `pos` itself and stops exactly there."""
        if not self.enabled:
            return
        pos = int(min(max(pos, 0), ZOOM_RANGE))
        rate = self.cfg.data["ptz"]["zoom_rate"] or ZOOM_RATE
        zp = self.zoom_pos
        travel = abs(pos - (zp if zp is not None else pos)) / rate + 0.3
        with self._cond:
            self._want_zoom = 0
            self._enqueue("zoom_to", pos)
            self.zoom_goal, self._zoom_goal_t = pos, time.monotonic()
            self._next_poll = 0.0  # an idle camera polls every 10 s: start watching for arrival now
            self.at_home = False
            self.last_motion_ts = time.time()
            self.motion_until = max(self.motion_until, time.time() + travel)  # counting pauses meanwhile
            self._cond.notify()

    @property
    def zoom_arrived(self) -> bool:
        """The last zoom_to has finished: the position reads back as the goal (or it timed out)."""
        if self.zoom_goal is None or time.monotonic() - self._zoom_goal_t > ZOOM_GOAL_TIMEOUT_S:
            return True
        zp, goal = self.zoom_pos, self.zoom_goal
        return zp is not None and goal is not None and abs(zp - goal) <= ZOOM_ARRIVED

    def home(self):
        if not self.enabled:
            return
        hp = self.cfg.data["ptz"]["home_preset"]
        with self._cond:
            self._cancel_abs(stop_camera=True)
            self._want_pt, self._want_zoom = STOP, 0
            if hp:
                self._enqueue("preset_call", hp)
            else:
                self._enqueue("home")
                self._enqueue("zoom_to", 0)
            self.at_home = True  # cleared again if the camera rejects the recall
            self.motion_until = time.time() + PRESET_TRAVEL_S
            self._cond.notify()

    def preset(self, action: str, n: int):
        if not self.enabled:
            return
        with self._cond:
            if action == "call":
                self._cancel_abs(stop_camera=True)
                self._want_pt, self._want_zoom = STOP, 0
                self._enqueue("preset_call", n)
                hp = self.cfg.data["ptz"]["home_preset"]
                self.at_home = bool(hp) and n == hp
                self.motion_until = time.time() + PRESET_TRAVEL_S
            else:
                self._enqueue("preset_set", n)
            self._cond.notify()

    def run_sync(self, kind: str, arg=None, timeout: float = 4.0):
        """Run one command on the worker and wait for the camera's answer. Returns (ok, error)."""
        if not self.enabled:
            return False, "PTZ control is disabled"
        done, result = threading.Event(), {}
        with self._cond:
            self._oneshots.append((kind, arg, done, result))
            self._cond.notify()
        if not done.wait(timeout):
            return False, "the camera did not answer"
        return bool(result.get("ok")), result.get("error", "")

    def set_home(self):
        """Store the current pan/tilt/zoom in the Home preset slot."""
        ok, err = self.run_sync("preset_set", HOME_SLOT)
        if ok:
            self.at_home = True
        return ok, err

    def _enqueue(self, kind: str, arg=None):
        self._oneshots.append((kind, arg, None, None))

    def is_moving(self) -> bool:
        return self._want_pt != STOP or self._want_zoom != 0 or time.time() < self.motion_until

    def quiet_since(self) -> float:
        """When the camera last moved (or is expected to stop moving)."""
        return max(self.last_motion_ts, self.motion_until)

    def position(self):
        if self.pan_pos is None and self.zoom_pos is None:
            return None
        return {"pan": self.pan_pos, "tilt": self.tilt_pos, "zoom": self.zoom_pos}

    # ---- worker ----
    def _run(self):
        started = time.monotonic()
        while not self._stop.is_set():
            with self._cond:
                self._cond.wait(0.1)
                now = time.monotonic()
                if self._want_pt != STOP and now > self._pt_deadline:
                    self._want_pt = STOP  # watchdog: the client stopped refreshing the move
                if self._want_zoom and now > self._zoom_deadline:
                    self._want_zoom = 0
                want_pt, want_zoom, want_abs, abs_gen = self._want_pt, self._want_zoom, self._want_abs, self._abs_gen
                abs_stop, self._abs_stop = self._abs_stop, False
                oneshots = list(self._oneshots)
                self._oneshots.clear()
            if not self.enabled:
                self._halt_if_moving(abs_stop)
                for _, _, done, result in oneshots:
                    if done is not None:
                        result["error"] = "PTZ control is disabled"
                        done.set()
                continue
            try:
                # Stops first, so they cannot cancel a Home/preset recall queued behind them.
                if (want_pt == STOP and self._sent_pt != STOP) or abs_stop:
                    self._apply_pt(STOP)
                if want_zoom == 0 and self._sent_zoom != 0:
                    self._apply_zoom(0)
                for item in oneshots:
                    self._run_oneshot(*item)
                if want_abs is not None and want_abs != self._sent_abs and time.monotonic() >= self._abs_retry:
                    self._send_abs(want_abs, abs_gen)
                elif want_pt != self._sent_pt:
                    self._apply_pt(want_pt)
                if want_zoom != self._sent_zoom:
                    self._apply_zoom(want_zoom)
                self._poll()
                if not self._home_checked and (self.pan_pos is not None or now - started > HOME_CHECK_TIMEOUT_S):
                    self._check_home()
                self._autofocus()
                if not self.connected:
                    self.log.info("PTZ connected (%s)", "VISCA" if self._visca_ok else "HTTP")
                self.connected = True
            except Exception as e:
                if self.connected:
                    self.log.warning("PTZ command failed: %s", e)
                self.connected = False
                with self._cond:  # a swing still under way is sent again once the camera answers (after a stop)
                    if self._want_abs is None and self._sent_abs is not None and self.pt_goal is not None \
                            and not self.pt_arrived:
                        self._want_abs = self._sent_abs
                self._sent_pt = self._sent_zoom = self._sent_abs = None  # resend once the camera is reachable
                self._stop.wait(1.0)

    def _apply_pt(self, pt):
        was_moving = self._sent_pt not in (None, STOP)
        self._send_pt(pt)
        self._sent_pt = pt
        if pt == STOP and was_moving:
            self._motion_ended()

    def _apply_zoom(self, byte):
        was_zooming = bool(self._sent_zoom)
        self._send_zoom(byte)
        self._sent_zoom = byte
        if byte == 0 and was_zooming:
            self._motion_ended()

    def _motion_ended(self, settle: float = AF_SETTLE_S):
        self.last_motion_ts = time.time()
        self._af_due = time.monotonic() + settle

    def _halt_if_moving(self, abs_stop: bool = False):
        """PTZ was switched off: make sure the camera is not left driving, zooming or travelling to an absolute
        goal (eye mode swings it there: a long swing takes seconds). Nothing is sent to a camera that was not
        moving, so switching PTZ control off never fights the camera's own remote."""
        with self._cond:
            self._cancel_abs(stop_camera=True)
            abs_stop = abs_stop or self._abs_stop
            self._abs_stop = False
            self._want_pt, self._want_zoom = STOP, 0
        driving = self._sent_pt not in (None, STOP)
        if not (abs_stop or driving or self._sent_zoom):
            return
        try:
            if abs_stop or driving:
                self._send_pt(STOP)
            if self._sent_zoom:
                self._send_zoom(0)
        except Exception as e:
            self.log.warning("could not stop the camera after PTZ was disabled: %s", e)
            with self._cond:
                self._abs_stop = self._abs_stop or abs_stop  # try again, after a pause
            self._stop.wait(1.0)
            return
        self._sent_pt, self._sent_zoom = STOP, 0
        self.last_motion_ts = time.time()

    def _check_home(self):
        """Startup: is the camera on its Home view? A custom Home (preset) is recalled so the detection
        zone and stats line up with the saved view."""
        self._home_checked = True
        if self.cfg.data["ptz"]["home_preset"]:
            self.home()
        elif self.pan_pos is None:
            self.at_home = True  # positions unavailable (HTTP fallback): assume the configured view
        else:
            self.at_home = (self.pan_pos, self.tilt_pos, self.zoom_pos) == (0, 0, 0)

    def _autofocus(self):
        if not self._af_due or not self._af_supported or not self.cfg.data["ptz"]["autofocus"]:
            return
        now = time.monotonic()
        if now < self._af_due or self.is_moving():
            return
        if now - self._af_last < AF_MIN_GAP_S:
            self._af_due = self._af_last + AF_MIN_GAP_S
            return
        self._af_due, self._af_last = 0.0, now
        if not self._visca_ok:
            return  # the HTTP CGI has no autofocus command
        for body in ([self._af_cmd] if self._af_cmd else AF_COMMANDS):
            try:
                self._visca.send(body)
                self._af_cmd = body
                return
            except ViscaError:
                continue
        self._af_supported = False
        self.log.warning("camera rejected the autofocus commands; autofocus after moves is off")

    def _run_oneshot(self, kind, arg, done, result):
        try:
            self._oneshot(kind, arg)
            if done is not None:
                result["ok"] = True
            if kind in ("home", "preset_call"):
                self._af_due = time.monotonic() + PRESET_TRAVEL_S  # refocus once the recall has arrived
            elif kind == "zoom_to":
                self._af_due = time.monotonic() + max(0.3, self.motion_until - time.time())
        except ViscaError as e:
            if kind in ("home", "preset_call"):
                self.at_home = False
                self.log.warning("camera rejected %s %s: %s", kind, arg if arg is not None else "", e)
            if done is not None:
                result["error"] = str(e)
        except Exception as e:
            if done is None:
                raise  # connection trouble: let the worker reconnect
            result["error"] = str(e) or e.__class__.__name__
        finally:
            if done is not None:
                done.set()

    def _oneshot(self, kind, arg):
        if kind == "reconnect":
            self._visca.close()
            self._visca = self._make_client()
            self._visca_ok, self._visca_retry = True, 0.0
        elif kind == "home":
            self._cmd(b"\x01\x06\x04", ("home", 10, 10))
        elif kind == "zoom_to":
            try:
                self._cmd(b"\x01\x04\x47" + _nibbles(arg), None)
            except OSError:
                if arg != 0:
                    raise
                with self._cond:  # no absolute zoom over HTTP: zoom out at full speed long enough to reach wide
                    self._want_zoom = 0x37
                    self._zoom_deadline = time.monotonic() + 8.0
        elif kind == "preset_call":
            self._cmd(b"\x01\x04\x3f\x02" + bytes([arg]), ("poscall", arg))
        elif kind == "preset_set":
            self._cmd(b"\x01\x04\x3f\x01" + bytes([arg]), ("posset", arg))

    def _send_pt(self, pt):
        vv, ww, pd, td = pt
        if (pd, td) == (3, 3):
            http = ("ptzstop", 10, 10)
        elif pd != 3 and (td == 3 or vv / PAN_MAX >= ww / TILT_MAX):  # HTTP CGI moves one axis only
            http = ("left" if pd == 1 else "right", vv, ww)
        else:
            http = ("up" if td == 1 else "down", vv, ww)
        self._cmd(b"\x01\x06\x01" + bytes([vv, ww, pd, td]), http, lenient=True)

    def _send_abs(self, goal, gen: int):
        with self._cond:
            if gen != self._abs_gen or self._want_abs != goal:
                return  # cancelled or replaced since this pass read it: not sent at all
        pan, tilt, vv, ww = goal
        try:
            self._cmd(b"\x01\x06\x02" + bytes([vv, ww]) + _nibbles(pan) + _nibbles(tilt), None)
        except ViscaError as e:
            if "90 60 02" in str(e):  # syntax error: this camera has no absolute pan/tilt
                self._abs_supported = False
                self._drop_abs(goal)
                self.log.warning("camera rejected absolute pan/tilt; following from the PTZ picture only")
            else:  # busy (for example a preset still travelling): try again shortly
                self._abs_retry = time.monotonic() + PT_RETRY_S
            return
        except OSError as e:
            # VISCA is down and absolute moves have no HTTP form: drop the goal. Keeping it would make every pass
            # fail before zoom, polling and focus, and send it minutes late once VISCA is back.
            self._drop_abs(goal)
            self.log.info("absolute pan/tilt dropped: %s", e)
            return
        with self._cond:
            if gen != self._abs_gen:
                return  # cancelled while it was being sent: the stop follows (_abs_stop), and it counts as not sent
            self._sent_abs = goal
            self._sent_pt = STOP  # the camera is driving itself now; a later stop must be sent explicitly
            if self._want_abs == goal:
                self._want_abs = None  # sent: nothing to replay after a reconnect
        self._af_due = time.monotonic() + self._pt_travel + 0.3

    def _drop_abs(self, goal):
        """A goal that can never be sent. If an earlier one may still be travelling, the camera is stopped: it can no
        longer be steered from here (VISCA is down)."""
        with self._cond:
            if self._want_abs == goal:
                self._want_abs = None
                self.pt_goal = None
                if self._sent_abs is not None:
                    self._abs_stop = True

    def _send_zoom(self, byte):
        p = byte & 0x0F
        http = ("zoomin", p) if byte & 0xF0 == 0x20 else ("zoomout", p) if byte & 0xF0 == 0x30 else ("zoomstop", 0)
        self._cmd(b"\x01\x04\x07" + bytes([byte]), http, lenient=True)

    def _cmd(self, visca_body: bytes, http_args, lenient: bool = False):
        """Send via VISCA; if VISCA is unreachable fall back to the camera's HTTP CGI.
        A VISCA error reply raises ViscaError unless `lenient` (continuous drive commands, which the
        camera may refuse while a preset is still travelling)."""
        if self._visca_ok or time.monotonic() >= self._visca_retry:
            try:
                r = self._visca.send(visca_body)
                self._visca_ok = True
                return r
            except ViscaError as e:
                if not lenient:
                    raise
                self.log.debug("VISCA error: %s", e)
                return None
            except OSError:
                self._visca_ok = False
                self._visca_retry = time.monotonic() + 30
                self.zoom_pos = self.pan_pos = self.tilt_pos = None  # no position readout over HTTP
                if http_args is None:
                    raise
        if http_args is None:
            raise OSError("VISCA unavailable")
        self._http(*http_args)
        return None

    def _http(self, *args):
        c = self.cfg.data["camera"]
        url = c["http_url"].rstrip("/") + "/cgi-bin/ptzctrl.cgi?ptzcmd&" + "&".join(str(a) for a in args)
        req = urllib.request.Request(url)
        if c["username"]:
            token = base64.b64encode(f"{c['username']}:{c['password']}".encode()).decode()
            req.add_header("Authorization", "Basic " + token)
        with urllib.request.urlopen(req, timeout=2) as r:
            r.read()

    def _poll(self):
        """Refresh zoom/pan/tilt positions: often while moving, rarely when idle."""
        now = time.monotonic()
        if now < self._next_poll or not self._visca_ok:
            return
        busy = self._sent_pt not in (None, STOP) or bool(self._sent_zoom) or time.time() - self.quiet_since() < 3
        goal_open = not self.zoom_arrived or not self.pt_arrived
        self._next_poll = now + (0.2 if goal_open else 0.5 if busy else 10.0)
        try:
            r = self._visca.send(b"\x09\x04\x47")
            if r and len(r) >= 7:
                self.zoom_pos = _from_nibbles(r[2:6])
            r = self._visca.send(b"\x09\x06\x12")
            if r and len(r) >= 11:
                self.pan_pos = _s16(_from_nibbles(r[2:6]))
                self.tilt_pos = _s16(_from_nibbles(r[6:10]))
            if self.at_home and not self.is_moving() and self.zoom_pos is not None:
                self.home_zoom = self.zoom_pos
                self.home_pan, self.home_tilt = self.pan_pos, self.tilt_pos
        except ViscaError as e:  # an unsupported inquiry is not a lost connection
            self.log.debug("position inquiry failed: %s", e)


class AutoTracker:
    """Keeps one target centred with pan/tilt, zooms to `target_size`, and returns home when it is gone.

    People come first: a person replaces any other automatically picked target, and objects that cross the
    view faster than the camera can turn (most passing cars) are not chased. Pan/tilt aim where the target
    will be once the picture catches up (it lags the camera by about half a second) and learn the speed of a
    steadily walking target, so the camera keeps pace instead of trailing it.

    Zoom is measured, not steered: once the target is centred and steady and the lens is at rest, its height
    in the picture and the lens curve give the zoom position for `target_size`, sent as one absolute command
    the camera stops at exactly. Steering the zoom continuously through a picture that lags the lens by half
    a second always overshot. When the target is lost, another followable object takes over; otherwise a
    vehicle (which never comes back) sends the camera Home at once, and a person is looked for a little longer.
    """

    COAST_S = 1.2            # s to keep turning at the target's learned speed after losing it (behind a bar?)
    VEHICLE_GRACE = 0.5      # s a lost vehicle (or other fast object) may be missing before giving up on it
    RELOCK_AFTER = 0.3       # s missing before looking for a re-detection (a detection can flicker for a frame)
    RELOCK_RADIUS = 0.2      # a re-detection this close to where the target was heading is taken to be it
    RESPAWN_MARGIN = 0.3     # s: a track born this long before the loss, or later, may be the target found again
    MAX_FOLLOW_SPEED = 0.25  # frame sizes per second: faster objects leave the view before the camera turns
                             # (measured at Home: people 0.05-0.17, passing cars mostly 0.4-0.86)
    UNPROVEN_S = 4.0         # a target found again must be seen moving within this long once the camera is still
    LEAD_S = 0.6             # aim this far ahead: picture latency plus the camera's reaction time
    KI = 0.5                 # how fast the learned speed adapts (speed fraction per second, per unit of error)
    I_ZONE = 0.2             # start learning once the target first comes this close to centre (no wind-up
                             # while the camera first swings towards it)
    ZOOM_IN_ZONE = 0.15      # zoom in only once the target has stayed this close to centre...
    ZOOM_IN_DWELL = 0.5      # ...for this long (s)...
    ZOOM_IN_MAX_VEL = 0.1    # ...and drifts slower than this (frame sizes per second): the camera keeps up
    EDGE = 0.02              # a box this close to the frame border is about to leave it
    MANUAL_PAUSE = 10.0      # manual PTZ input suspends auto-follow this long
    # Eye mode: the fixed eye camera picks the target and the sync map aims the PTZ at it (see update_eye).
    EYE_LAT_S = 0.15         # the eye's picture lag (USB and decoding)
    AIM_PERIOD_S = 0.4       # re-aim at most this often while pursuing...
    ARRIVE_S = 0.6           # ...at a speed that reaches each aim this long after it is sent (smooth pursuit)...
    AIM_DEADBAND = 0.02      # ...and only when the aim moved by this share of the PTZ's field
    ACQUIRE = 0.5            # an aim this far off (share of the field) is a fresh swing, sent at once at full speed
    MAX_SLEW = 0.7           # follow only what needs at most this share of the camera's fastest pan/tilt
    ZOOM_PERIOD_S = 1.0      # re-think the zoom at most this often
    POINTED = 0.12           # pointed: within this share of the field of the aim...
    POINTED_FOR = 0.4        # ...for this long, before zooming in
    FIT = 0.3                # the target may drift this share of the field between aims, at most
    PTZ_LAG_S = 0.6          # the PTZ picture shows the pose this long ago: refine only when it has been still
    REFINE_RADIUS = 0.3      # a PTZ detection this close (share of its frame) to where the target should be is it
    REFINE_MAX_AGE = 0.3     # ...but only while the eye still sees the target (seconds since its last detection)
    BIAS_GAIN = 0.4          # how much of each measured aiming error is corrected
    BIAS_MAX = 0.5           # at most this share of the Home-zoom field
    EYE_RELOCK_RADIUS = 0.08  # a new eye track this close (eye frame) to where a lost target should be now is it
    EYE_EDGE = 0.04          # a target last seen this close to the eye's frame edge, heading out, has left the view
    EYE_MIN_SPEED = 0.01     # eye frames per second: slower than this, a track's direction is unknown
    PICK_TIMEOUT_S = 1.5     # a click on the PTZ view that cannot be placed in the eye's picture by then is dropped

    def __init__(self, ptz: PTZController, cfg_store, log=None):
        self.ptz = ptz
        self.cfg = cfg_store
        self.log = log or logging.getLogger("ptzcam.autotrack")
        self.enabled = cfg_store.data["ptz"]["autotrack"]
        self.paused_until = 0.0
        self.sync = None          # SyncMap (ptzcam/sync.py), set by the pipeline once calibrated
        self.eye_active = False   # the eye camera is online and its picture usable (set by the pipeline)
        self.ptz_usable = True    # the PTZ picture is not washed out (refinement and linking use it)
        self.mode = "ptz"         # "eye" while the eye picks targets and the sync map aims the PTZ
        self.eye_zone = []        # where, in the eye's picture, a target can be counted (set by the pipeline)
        self.eye_counted_only = False  # the PTZ counts: leave Home only for what it has counted (set by the pipeline)
        self.ptz_match_id = None  # the PTZ track matched to the eye target (highlighted on the PTZ view)
        self._lock = threading.RLock()  # the PTZ thread, the eye thread and the web thread all call in
        self.reset_target()

    def reset_target(self):
        """Forget the current target (also used when the tracker restarts and ids are reassigned)."""
        self.target_id = None
        self.user_locked = False
        self.target_category = None
        self.last_center = None
        self.last_seen = time.time()
        self._target = None      # the target's Track, kept after it is lost so a re-detection can take over
        self._last_vel = (0.0, 0.0)
        self._speed = [0.0, 0.0]  # learned pan/tilt speed (fractions of full speed; +y is down the frame)
        self._learning = [False, False]
        self._h = None           # target's last box height (re-detections must be about the same size)
        self._heights = deque(maxlen=5)  # recent box heights since the lens came to rest
        self._lens_rest = None   # when the lens was last seen at rest (monotonic)
        self._centred_since = None
        self._widened = False    # zoomed back out to look for a lost target
        self._unproven = False   # following a re-detection that has not been seen moving yet
        self._still_since = None
        self._pt_key = None
        self._pt_t = 0.0
        # eye mode
        self._aim_t = 0.0
        self._aim = None         # last (pan, tilt) goal sent
        self._aim_out = (0.0, 0.0)  # how far (pan, tilt units) the last aim lay beyond the calibrated range
        self._bias = [0.0, 0.0]  # PTZ-picture corrections to the sync map's aim, in pan/tilt units
        self._aim_bias = (0.0, 0.0)  # the correction the last aim was sent with
        self._poses = deque(maxlen=90)  # (monotonic t, pan, tilt, zoom, bias): which pose a late PTZ picture shows
        self._eye_h = deque(maxlen=5)  # the target's box height in the eye's picture
        self._zoom_t = 0.0
        self._pointed_since = None
        self._ptz_pick = None    # a PTZ track the user clicked while the eye leads: matched to an eye track
        self._ptz_pick_t = 0.0
        self.ptz_match_id = None

    def set_enabled(self, enabled: bool, track_id=None):
        with self._lock:
            was = self.enabled
            self.enabled = enabled
            self.paused_until = 0.0
            if enabled and track_id is not None and self.mode == "eye" and track_id < EYE_ID_BASE:
                # Clicked on the PTZ view: find it in the eye's picture (next PTZ frame, then eye frame). Until then
                # keep following what is followed now, rather than swinging off to whatever ranks best.
                if not was:
                    self.reset_target()
                self._ptz_pick, self._ptz_pick_t = track_id, time.monotonic()
                return
            poses = self._poses
            self.reset_target()
            if enabled:
                if self.mode == "eye":
                    self._poses = poses  # what the camera did stays true for a new pick (a late PTZ picture)
                self.target_id = track_id
                self.user_locked = track_id is not None
            else:
                self.ptz.stop()

    def resume(self):
        """End a manual-control pause (e.g. the user just saved this view as Home)."""
        with self._lock:
            self.paused_until = 0.0
            self.reset_target()

    def paused_for(self) -> float:
        return max(0.0, self.paused_until - time.time()) if self.enabled else 0.0

    def manual_override(self):
        with self._lock:
            if self.enabled:
                self.paused_until = time.time() + self.MANUAL_PAUSE
                self.reset_target()
                self.last_seen = self.paused_until  # the return-home countdown restarts after the pause

    def _check_mode(self):
        """Eye mode needs the sync map, a usable eye picture and absolute pan/tilt. Ids from one tracker mean
        nothing to the other, so switching modes forgets the target."""
        want = "eye" if (self.sync is not None and self.eye_active and self.ptz.can_point
                         and self.cfg.data["eye"]["enabled"] and self.cfg.data["eye"]["follow"]) else "ptz"
        if want != self.mode:
            self.log.info("auto-follow: %s", "the wide camera picks targets and aims the PTZ" if want == "eye"
                          else "the PTZ follows from its own picture")
            self.mode = want
            self.reset_target()
        return want

    def update(self, tracks, ts: float):
        """Called for every PTZ frame with the PTZ tracker's tracks."""
        with self._lock:
            if self._check_mode() == "eye":
                self._refine(tracks, ts)
                return
            self._update_ptz(tracks, ts)

    def _update_ptz(self, tracks, ts: float):
        if not self.enabled or not self.ptz.enabled or ts < self.paused_until:
            return
        cfg = self.cfg.data["ptz"]
        visible = [t for t in tracks if t.confirmed and t.last_ts == ts]
        target = next((t for t in visible if t.id == self.target_id), None)
        lost_for = ts - self.last_seen
        if target is None and self.target_id is not None and self.last_center is not None \
                and self.RELOCK_AFTER <= lost_for <= cfg["return_home_seconds"]:
            target = self._relock(visible, lost_for)

        # A target the user picked is never swapped for another object; it is given up only at return-home.
        if not self.user_locked:
            pick = None
            grace = self.COAST_S if self.target_category in SLOW else self.VEHICLE_GRACE
            if target is None and (self.target_id is None or lost_for > grace):
                pick = self._pick(visible, cfg)  # the next object, when the target is gone
            elif target is not None and target.category != "person":
                pick = self._pick(visible, cfg, people_only=True)  # people come first
            if pick is not None:
                self.log.info("%s %s #%d", "next:" if target is None and self.target_id is not None else "following",
                              pick.label, pick.id)
                self._set_target(pick)
                target = pick

        if target is not None and self._unproven and self._give_up(target, ts, cfg):
            return
        if target is not None:
            self._follow(target, ts, cfg)
        else:
            self._search(ts, lost_for, cfg)

    def _countable(self, t, zone=None) -> bool:
        """Only something that can become an event is worth leaving Home for: while the camera is away,
        nothing else in the scene can be counted."""
        det = self.cfg.data["detect"]
        c = det["categories"].get(t.category)
        if not c or not c["enabled"] or t.max_score < c["min_score"]:
            return False
        return t.counted or not (det["zone"] if zone is None else zone) or t.in_zone

    def _pick(self, visible, cfg, people_only=False):
        cands = [t for t in visible if t.moving and t.category in cfg["autotrack_categories"]
                 and (t.category == "person" or not people_only) and self._countable(t)
                 and _image_speed(t) <= self.MAX_FOLLOW_SPEED]
        if not cands:
            return None
        return max(cands, key=lambda t: (t.category == "person", t.counted,
                                         FOLLOW_WEIGHT.get(t.category, 1.0) * t.area * t.score))

    def _set_target(self, t):
        # Something right where the target was (a rider and their bike) moves with it: keep the learned speed.
        if self.last_center is None or math.dist(t.center, self.last_center) >= self.RELOCK_RADIUS:
            self._speed, self._learning = [0.0, 0.0], [False, False]
        self.target_id, self._target, self.target_category, self.user_locked = t.id, t, t.category, False
        self._h, self._centred_since, self._widened = None, None, False
        self._heights.clear()
        self._unproven, self._still_since = False, None

    def _relock(self, visible, lost_for):
        """The tracker can lose the target while the camera turns (motion blur, a window bar in front) and find
        it again under a new id. Take a matching track that appeared around the loss (or is already known to
        move), close to where the target was heading or to where the camera was bringing it: the centre."""
        cats = RIDER if self.target_category in RIDER else {self.target_category}
        slow = self.target_category in SLOW
        coast = min(lost_for, self.COAST_S) if slow else 0.0
        ahead = (self.last_center[0] + self._last_vel[0] * coast, self.last_center[1] + self._last_vel[1] * coast)
        best = None
        for t in visible:
            # A new detection of about the same size (the search may zoom out meanwhile).
            new = slow and t.first_ts >= self.last_seen - self.RESPAWN_MARGIN \
                and (self._h is None or 0.4 <= (t.box[3] - t.box[1]) / self._h <= 2.5)
            if t.category not in cats or not (new or t.moving):
                continue
            d = min(_seg_dist(t.center, self.last_center, ahead), _seg_dist(t.center, self.last_center, (0.5, 0.5)))
            if d < self.RELOCK_RADIUS and (best is None or d < best[0]):
                best = (d, t, new)
        if best is None:
            return None
        _, t, new = best
        old = self._target
        if new and old is not None and old is not t and t.category == old.category:
            t.inherit(old)  # the same object: it keeps its count and event, so it is not counted twice
        self.log.info("found %s #%s again as %s #%d", old.label if old is not None else self.target_category,
                      self.target_id, t.label, t.id)
        self.target_id, self._target, self.target_category = t.id, t, t.category
        self._heights.clear()  # a new box: measure its size afresh
        self._widened = False
        self._unproven, self._still_since = not t.moving, None
        return t

    def _give_up(self, t, ts, cfg) -> bool:
        """A re-detection that never moves once the camera is still was probably something else."""
        if t.moving:
            self._unproven = False
            return False
        if self.ptz.is_moving() or ts - self.ptz.quiet_since() < cfg["settle_seconds"]:
            self._still_since = None  # motion is only measured while the camera is still
        elif self._still_since is None:
            self._still_since = ts
        elif ts - self._still_since > self.UNPROVEN_S:
            self.log.info("%s #%d is not moving; returning home", t.label, t.id)
            self.ptz.home()
            self.reset_target()
            return True
        return False

    def _follow(self, t, ts, cfg):
        dt = min(max(ts - self.last_seen, 0.0), 0.25)
        self.last_seen, self.last_center, self._target, self.target_category = ts, t.center, t, t.category
        self._last_vel = (float(t.vel[0]), float(t.vel[1]))
        # Aim where the target will be when the picture catches up with the camera.
        err = (t.center[0] - 0.5 + self.LEAD_S * self._last_vel[0], t.center[1] - 0.5 + self.LEAD_S * self._last_vel[1])
        dz = cfg["deadzone"]
        # The same angular speed moves the picture faster when zoomed in: scale by the magnification.
        zp = self.ptz.zoom_pos
        zf = 1.0 / mag_at(zp, cfg["zoom_curve"]) if zp is not None else 1.0
        top = (cfg["max_pan_speed"] / PAN_MAX, cfg["max_tilt_speed"] / TILT_MAX)
        drive = [0.0, 0.0]
        for k in (0, 1):
            e, lim = err[k], top[k]
            self._learning[k] = self._learning[k] or abs(t.center[k] - 0.5) < self.I_ZONE
            if self._learning[k]:  # learn the speed a steadily moving target needs
                self._speed[k] = min(lim / 2, max(-lim / 2, self._speed[k] + self.KI * e * dt))
            p = 0.0
            if abs(e) > dz:
                p = math.copysign(max(0.15, min(1.0, (abs(e) - dz) / max(0.5 - dz, 1e-3))) * lim * zf, e)
            drive[k] = min(lim, max(-lim, p + self._speed[k]))
        self._drive(drive[0], -drive[1])  # +tilt is up; +y is down the frame
        self._h = max(float(t.box[3] - t.box[1]), 1e-3)
        off = max(abs(err[0]), abs(err[1]))
        if off >= self.ZOOM_IN_ZONE:
            self._centred_since = None
        elif self._centred_since is None:
            self._centred_since = ts
        if cfg["autozoom"]:
            self._autozoom(t, off, ts, cfg)

    def _autozoom(self, t, off, ts, cfg):
        ptz, curve, size = self.ptz, cfg["zoom_curve"], cfg["target_size"]
        zp = ptz.zoom_pos
        if zp is None:
            return  # no position readout (HTTP fallback): pan and tilt only
        b = t.box
        at_edge = min(b[0], b[1], 1.0 - b[2], 1.0 - b[3]) < self.EDGE
        if (at_edge and off > 0.3) or off > 0.45:  # about to leave: half the magnification at once
            goal = pos_for(mag_at(zp, curve) / 2.0, curve)
            if zp - goal >= 256 and (ptz.zoom_goal is None or ptz.zoom_goal > goal + 256 or ptz.zoom_arrived):
                ptz.zoom_to(goal)
                self._lens_rest = None
            return
        now = time.monotonic()
        if not ptz.zoom_arrived:
            self._lens_rest = None
            return  # the lens is still moving
        if self._lens_rest is None:
            self._lens_rest = now
            self._heights.clear()
        if now - self._lens_rest < self.LEAD_S:
            return  # the picture still shows the lens before it stopped
        self._heights.append(self._h)
        if len(self._heights) < 3:
            return
        h = sorted(self._heights)[len(self._heights) // 2]
        if h > size * 1.25:
            goal = pos_for(mag_at(zp, curve) * size / h, curve)
        elif h < size / 1.2 and not at_edge and self._centred_since is not None \
                and ts - self._centred_since >= self.ZOOM_IN_DWELL and math.hypot(*t.vel) < self.ZOOM_IN_MAX_VEL:
            goal = min(pos_for(mag_at(zp, curve) * size / h, curve), cfg["max_zoom_pos"])
        else:
            return
        if abs(goal - zp) >= 256:
            ptz.zoom_to(goal)
            self._lens_rest = None

    def _search(self, ts, lost_for, cfg):
        if self.target_id is None and self.ptz.at_home:
            return  # idle on the Home view
        slow = self.target_category in SLOW
        if self.target_id is not None and slow and lost_for <= self.COAST_S:
            self._drive(self._speed[0], -self._speed[1])  # it may be behind a window bar: keep pace with it
        elif self._pt_key not in (None, (0, 0)):
            self.ptz.stop_pantilt()
            self._pt_key = (0, 0)
        if self.target_id is not None and not slow and lost_for > self.VEHICLE_GRACE:
            self.log.info("lost %s #%s; returning home", self.target_category, self.target_id)
            self.ptz.home()  # a vehicle that has gone does not come back
            self.reset_target()
            return
        if self.target_id is not None and slow and lost_for > self.COAST_S and not self._widened:
            self._widened = True  # look for it at the Home zoom, where the view is widest
            hz, zp = self.ptz.home_zoom, self.ptz.zoom_pos
            if cfg["autozoom"] and hz is not None and zp is not None and zp > hz + 256:
                self.ptz.zoom_to(hz)
        if lost_for > cfg["return_home_seconds"]:
            self.log.info("nothing to follow; returning home")
            self.ptz.home()
            self.reset_target()

    # ---- eye mode: the fixed eye camera picks the target, the sync map aims the PTZ at it ----
    def update_eye(self, tracks, ts: float):
        """Called for every eye frame with the eye tracker's tracks. The eye does not move, so its tracks show
        how objects really move: no picture lag to steer through, no losses while the PTZ turns."""
        with self._lock:
            if self._check_mode() != "eye" or not self.enabled or not self.ptz.enabled or ts < self.paused_until:
                return
            cfg = self.cfg.data["ptz"]
            visible = [t for t in tracks if t.confirmed and t.last_ts == ts]
            if self._ptz_pick is not None and time.monotonic() - self._ptz_pick_t > 2 * self.PICK_TIMEOUT_S:
                self._ptz_pick = None  # no PTZ frame placed it (the PTZ picture stopped?)
            if isinstance(self._ptz_pick, tuple):
                self._take_ptz_pick(visible)
            target = next((t for t in visible if t.id == self.target_id), None)
            lost_for = ts - self.last_seen
            if target is None and self.target_id is not None and self._target is not None \
                    and self.RELOCK_AFTER <= lost_for <= cfg["return_home_seconds"]:
                target = self._relock_eye(visible, lost_for, ts)
            if target is not None and self._unproven:  # a re-detection not yet seen moving (see _relock_eye)
                if target.moving:
                    self._unproven = False
                elif ts - self._still_since > self.UNPROVEN_S:
                    self.log.info("%s #%d is not moving; returning home", target.label, target.id)
                    self.ptz.home()
                    self.reset_target()
                    return
            # A target the user picked is never swapped for another, nor is anything picked while a click on the PTZ
            # view is still being placed in the eye's picture.
            if not self.user_locked and self._ptz_pick is None:
                pick = None
                grace = self.COAST_S if self.target_category in SLOW else self.VEHICLE_GRACE
                if target is None and (self.target_id is None or lost_for > grace):
                    pick = self._pick_eye(visible, cfg)
                elif target is not None and self._unproven:
                    pick = self._pick_eye(visible, cfg)  # anything seen moving beats an unproven re-detection
                elif target is not None and target.category != "person":
                    pick = self._pick_eye(visible, cfg, people_only=True)
                if pick is not None:
                    self.log.info("%s %s #%d (wide camera)", "next:" if target is None and self.target_id is not None
                                  else "following", pick.label, pick.id)
                    self._set_eye_target(pick)
                    target = pick
            if target is not None:
                self.last_seen, self.last_center, self._target, self.target_category = ts, target.center, target, target.category
                self._eye_h.append(float(target.box[3] - target.box[1]))
                self._steer(target.center, target.vel, cfg, zoom=cfg["autozoom"])
            else:
                self._eye_search(ts, lost_for, cfg)

    def _set_eye_target(self, t):
        self.target_id, self._target, self.target_category, self.user_locked = t.id, t, t.category, False
        self._bias, self._aim, self._pointed_since, self.ptz_match_id = [0.0, 0.0], None, None, None
        self._aim_bias = (0.0, 0.0)
        self._eye_h.clear()  # (the pose history is the camera's, not the target's: a late PTZ picture needs it)
        self._widened = False
        self._unproven = False

    def _relock_eye(self, visible, lost_for, ts):
        """The eye's tracker gives up on a track after 2 s unseen (behind a van, a tree) and finds the object again
        under a new id. Take a new track of the same kind that appeared since, about its size, close to where the
        target should be by now, and not moving the other way. Vehicles are not looked for (one that has gone does
        not come back), nor anything that left through the edge of the picture: whoever appears there is as likely
        someone else. A target the user picked stays picked.
        The count and event move over only when it is clearly the same object, moving the same way at about the
        same speed; giving one person's count to another would leave that person uncounted. Otherwise it is followed
        unproven: anything seen moving takes over from it, and if it is never seen moving, the camera goes Home."""
        old = self._target
        if old.category not in SLOW:
            return None
        v0 = np.array(old.vel, float)
        s0 = float(np.hypot(*v0))
        b = old.box
        if s0 >= self.EYE_MIN_SPEED and ((b[0] < self.EYE_EDGE and v0[0] < 0) or (b[2] > 1 - self.EYE_EDGE and v0[0] > 0)
                                         or (b[1] < self.EYE_EDGE and v0[1] < 0)
                                         or (b[3] > 1 - self.EYE_EDGE and v0[1] > 0)):
            return None
        cats = RIDER if old.category in RIDER else {old.category}
        h = max(float(b[3] - b[1]), 1e-3)
        c0 = self.last_center if self.last_center is not None else old.center
        coast = min(lost_for, 3.0)
        ahead = (c0[0] + v0[0] * coast, c0[1] + v0[1] * coast)
        best = None
        for t in visible:
            if t is old or t.category not in cats or t.first_ts < self.last_seen - self.RESPAWN_MARGIN:
                continue
            if not 0.5 <= (t.box[3] - t.box[1]) / h <= 2.0:
                continue
            v = np.array(t.vel, float)
            s = float(np.hypot(*v))
            if s >= self.EYE_MIN_SPEED and s0 >= self.EYE_MIN_SPEED and float(v @ v0) < 0:
                continue  # going the other way: someone else
            d = math.dist(t.center, ahead)
            if d < self.EYE_RELOCK_RADIUS and (best is None or d < best[0]):
                same_way = s >= self.EYE_MIN_SPEED and s0 >= self.EYE_MIN_SPEED and float(v @ v0) > 0 \
                    and 0.33 <= s / s0 <= 3.0
                best = (d, t, same_way)
        if best is None:
            return None
        _, t, same_way = best
        if same_way and t.category == old.category:
            t.inherit(old)  # its count and event: not counted twice
        self.log.info("found %s #%s again as %s #%d (wide camera)%s", old.label, self.target_id, t.label, t.id,
                      "" if same_way else ", not yet seen moving")
        self.target_id, self._target, self.target_category = t.id, t, t.category
        self._eye_h.clear()
        self._unproven, self._still_since = not same_way, ts
        return t

    def _slew(self, t) -> float:
        """Pan/tilt speed (units per second) that keeping `t` centred needs."""
        vel = np.array(t.vel, float)
        s, n = _image_speed(t), float(np.hypot(*vel))
        if n > 1e-6 and s > n:  # the velocity estimate lags just after an object appears
            vel *= s / n
        return float(np.abs(self.sync.jacobian(*t.center) @ vel).max())

    def _pick_eye(self, visible, cfg, people_only=False):
        # The zone is the one the counting camera really uses, in the eye's picture (Pipeline: the eye's own zone,
        # or the PTZ's Home view and zone mapped through the sync map), so the PTZ leaves Home only for something
        # that can become an event.
        zone, top = self.eye_zone, (cfg["pt_rate"] or PT_RATE) * self.MAX_SLEW
        # While the PTZ counts, only what it has counted (the eye sees people half a second before the PTZ does;
        # swinging away first would leave them uncounted, with no event at all).
        cands = [t for t in visible if t.moving and t.category in cfg["autotrack_categories"]
                 and (t.category == "person" or not people_only) and self._countable(t, zone)
                 and (t.counted or not self.eye_counted_only)
                 and self.sync.covers(*t.center) and self._slew(t) <= top]
        if not cands:
            return None
        return max(cands, key=lambda t: (t.category == "person", t.counted,
                                         FOLLOW_WEIGHT.get(t.category, 1.0) * t.area * t.score))

    def _steer(self, centre, vel, cfg, zoom: bool):
        """Aim the PTZ where the target will be (eye coordinates), and zoom once pointed."""
        sync, ptz = self.sync, self.ptz
        now = time.monotonic()
        u, v = float(centre[0]), float(centre[1])
        vel = np.array(vel, float)
        rate = cfg["pt_rate"] or PT_RATE
        pose, zp = ptz.pose_estimate(), ptz.zoom_pos
        jac = sync.jacobian(u, v)
        fld = sync.field(u, v, zp if zp is not None else sync.ptz_zoom, cfg["zoom_curve"])
        field_u = max(min(abs(fld[0] * jac[0, 0]), abs(fld[1] * jac[1, 1])), 1.0) if fld else 50.0
        pan0, tilt0 = sync.ptz_raw(u, v)
        # How far off the camera is, measured to where it may go: a target beyond the calibrated range is as close
        # as it gets once the camera sits at the limit (measured to the target itself, it would be re-aimed at the
        # same goal on every frame, keeping the camera "moving" and counting paused).
        here = sync.clamp(pan0 + self._bias[0], tilt0 + self._bias[1])
        gap = max(abs(here[0] - pose[0]), abs(here[1] - pose[1])) if pose else float("inf")
        swing = gap > self.ACQUIRE * field_u
        # Aim where it will be when the camera gets there: the eye's lag plus the travel time. A walker is pursued
        # at a speed that arrives in ARRIVE_S (smooth), a new target swung to at full speed.
        arrive = min(gap / rate, 1.5) if swing else self.ARRIVE_S
        lead = self.EYE_LAT_S + arrive
        pu, pv = min(max(u + vel[0] * lead, 0.0), 1.0), min(max(v + vel[1] * lead, 0.0), 1.0)
        raw_pan, raw_tilt = sync.ptz_raw(pu, pv)
        raw_pan, raw_tilt = raw_pan + self._bias[0], raw_tilt + self._bias[1]
        goal = sync.clamp(raw_pan, raw_tilt)  # never outside the calibrated area
        self._aim_out = (abs(raw_pan - goal[0]), abs(raw_tilt - goal[1]))  # how far the aim is past a limit
        need = max(abs(goal[0] - pose[0]), abs(goal[1] - pose[1])) if pose else float("inf")
        moved = max(abs(goal[0] - self._aim[0]), abs(goal[1] - self._aim[1])) if self._aim else float("inf")
        if (swing and goal != self._aim) or (now - self._aim_t >= self.AIM_PERIOD_S
                                             and moved > self.AIM_DEADBAND * field_u):
            speed = 1.0 if swing else min(1.0, max(1.0 / PAN_MAX, need / (rate * self.ARRIVE_S)))
            if ptz.pan_tilt_to(goal[0], goal[1], speed):
                self._aim, self._aim_t, self._aim_bias = goal, now, tuple(self._bias)
        if pose is not None and zp is not None:
            self._poses.append((now, pose[0], pose[1], zp, self._aim_bias))
        if need / field_u <= self.POINTED:
            self._pointed_since = self._pointed_since or now
        else:
            self._pointed_since = None
        if zoom:
            self._eye_zoom(u, v, jac, vel, now, cfg)

    def _eye_zoom(self, u, v, jac, vel, now, cfg):
        """One zoom step from the eye's measurement of the target's height, capped so it stays in frame."""
        ptz, sync, curve = self.ptz, self.sync, cfg["zoom_curve"]
        zp = ptz.zoom_pos
        if zp is None or not self._eye_h or now - self._zoom_t < self.ZOOM_PERIOD_S or not ptz.zoom_arrived:
            return
        h = sorted(self._eye_h)[len(self._eye_h) // 2]
        goal = sync.zoom_for(u, v, h, cfg["target_size"], curve, cfg["max_zoom_pos"])
        fp = sync.footprint(u, v)
        if goal is None or fp is None:
            return
        k_cal = mag_at(sync.ptz_zoom, curve)
        fw_cal, fh_cal = abs(fp[0] * jac[0, 0]), abs(fp[1] * jac[1, 1])  # the field in pan/tilt units, calibration zoom
        caps = []
        drift = np.abs(jac @ vel) * (self.AIM_PERIOD_S + self.EYE_LAT_S)  # how far it moves before the next aim lands
        for f_cal, d in ((fw_cal, drift[0]), (fh_cal, drift[1])):
            if d > 1e-6:
                caps.append(k_cal * f_cal * self.FIT / d)
        # Past the calibrated range (below the lowest tilt, above the highest, beyond the pan range) the PTZ cannot
        # centre it: zoom only as far as it still fits in the frame from the limit. Half an object's height is 0.55 h
        # with a margin; its half width about 0.15 h (a person, in eye-frame units).
        out_p, out_t = self._aim_out
        if out_t > 1.0:
            caps.append(k_cal * fh_cal / (2.0 * (out_t + 0.55 * h * abs(jac[1, 1]))))
        if out_p > 1.0:
            caps.append(k_cal * fw_cal / (2.0 * (out_p + 0.15 * h * abs(jac[0, 0]))))
        if caps:
            goal = min(goal, pos_for(max(1.0, min(caps)), curve))
        if goal > zp and (self._pointed_since is None or now - self._pointed_since < self.POINTED_FOR):
            return  # zoom in only once pointed at it
        if abs(goal - zp) >= 256:
            ptz.zoom_to(goal)
            self._zoom_t = now

    def _eye_search(self, ts, lost_for, cfg):
        if self.target_id is None:
            # Idle. Usually that is at Home, but the target is also dropped while the camera is elsewhere: manual
            # control (the countdown restarts after its pause), a switch from PTZ mode mid-follow, a restart.
            if not self.ptz.at_home and self._ptz_pick is None and lost_for > cfg["return_home_seconds"]:
                self.log.info("nothing to follow; returning home")
                self.ptz.home()
                self.reset_target()
            return
        t, slow = self._target, self.target_category in SLOW
        if slow and lost_for <= self.COAST_S and t is not None:
            c = (t.center[0] + t.vel[0] * lost_for, t.center[1] + t.vel[1] * lost_for)
            self._steer(c, t.vel, cfg, zoom=False)  # behind a tree or a bar: keep pace with it
            return
        if not slow and lost_for > self.VEHICLE_GRACE:
            self.log.info("lost %s #%s; returning home", self.target_category, self.target_id)
            self.ptz.home()
            self.reset_target()
            return
        if slow and lost_for > self.COAST_S and not self._widened:
            self._widened = True
            hz, zp = self.ptz.home_zoom, self.ptz.zoom_pos
            if cfg["autozoom"] and hz is not None and zp is not None and zp > hz + 256:
                self.ptz.zoom_to(hz)
        if lost_for > cfg["return_home_seconds"]:
            self.log.info("nothing to follow; returning home")
            self.ptz.home()
            self.reset_target()

    def _ptz_view(self):
        """Where the PTZ looks now in the eye's picture: (centre u, v, field width, height), or None."""
        pose, zp = self.ptz.pose_estimate(), self.ptz.zoom_pos
        if pose is None or zp is None:
            return None
        cu, cv = self.sync.to_eye(*pose)
        fld = self.sync.field(cu, cv, zp, self.cfg.data["ptz"]["zoom_curve"])
        return (cu, cv, fld[0], fld[1]) if fld else None

    def _refine(self, tracks, ts):
        """PTZ frame, eye mode: find the target in the PTZ's own picture. Its offset corrects the sync map's aim
        (parallax, calibration error), and the PTZ track joins the target's event so the close-up becomes the
        photo, without ever being counted again."""
        if isinstance(self._ptz_pick, int):
            self._map_ptz_pick(tracks, ts)
        t = self._target
        self.ptz_match_id = None
        if t is None or not self.ptz_usable or self.target_id is None or not self.ptz.zoom_arrived:
            return
        age = ts - t.last_ts  # both are frame arrival times (wall clock)
        if age > self.REFINE_MAX_AGE:
            return  # the eye has lost it: where it was says nothing about who is there now
        then = self._pose_at(_mono(ts) - self.PTZ_LAG_S)  # the PTZ picture shows the pose this long before it came
        if then is None:
            return
        _, pan_then, tilt_then, zoom_then, bias_then = then
        cu, cv = self.sync.to_eye(pan_then, tilt_then)
        fld = self.sync.field(cu, cv, zoom_then, self.cfg.data["ptz"]["zoom_curve"])
        if fld is None:
            return
        fw, fh = fld
        # Where the target was when the PTZ picture was taken: the PTZ picture is older than the eye's, and the eye's
        # last detection is `age` older than this PTZ frame.
        shift = age - (self.PTZ_LAG_S - self.EYE_LAT_S)
        eu, ev = t.center[0] + t.vel[0] * shift, t.center[1] + t.vel[1] * shift
        x_exp, y_exp = 0.5 + (eu - cu) / fw, 0.5 + (ev - cv) / fh
        h_exp = (t.box[3] - t.box[1]) / fh
        cats = RIDER if t.category in RIDER else {t.category}
        best = None
        for p in tracks:
            if not p.confirmed or p.last_ts != ts or p.category not in cats:
                continue
            if not 0.5 <= (p.box[3] - p.box[1]) / max(h_exp, 1e-3) <= 2.0:
                continue
            d = math.hypot(p.center[0] - x_exp, p.center[1] - y_exp)
            if d < self.REFINE_RADIUS and (best is None or d < best[0]):
                best = (d, p)
        if best is None:
            return
        p = best[1]
        self.ptz_match_id = p.id
        if t.event_id is not None and p.event_id is None:
            p.event_id, p.counted = t.event_id, True
        jac = self.sync.jacobian(cu, cv)
        corr = jac @ np.array([(p.center[0] - x_exp) * fw, (p.center[1] - y_exp) * fh])
        home = self.sync.field(cu, cv, self.ptz.home_zoom or self.sync.ptz_zoom, self.cfg.data["ptz"]["zoom_curve"])
        lim = self.BIAS_MAX * abs((home[0] if home else fw) * jac[0, 0])
        # x_exp came from the pose the camera really had, so the offset seen is the sync map's own error there,
        # whatever correction that aim carried: ease the correction towards it (adding it up instead winds up any
        # small constant error, such as a latency a frame off, without limit).
        for k in (0, 1):
            want = min(lim, max(-lim, float(corr[k])))
            self._bias[k] += self.BIAS_GAIN * (want - self._bias[k])

    def _pose_at(self, t):
        """The pose (and the correction it was aimed with) at monotonic time t, if the camera was not swinging fast
        then (a fast swing blurs the PTZ picture and its timing is uncertain)."""
        older = [p for p in self._poses if p[0] <= t]
        if not older:
            return None
        p = older[-1]
        if t - p[0] > 0.3:
            # An old sample is only still true if the camera has not moved since: no aim (a sample is the pose just
            # before that frame's aim), and nothing else either (manual control, a preset; wall clock there).
            moved = time.time() - self.ptz.quiet_since() < time.monotonic() - p[0]
            if self._aim_t >= p[0] or moved or not self.ptz.pt_arrived:
                return None
        before = [q for q in older if q[0] <= p[0] - 0.2]
        if before:
            q = before[-1]
            fld = self.sync.field(*self.sync.to_eye(p[1], p[2]), p[3], self.cfg.data["ptz"]["zoom_curve"])
            jac = self.sync.jacobian(*self.sync.to_eye(p[1], p[2]))
            span = abs(fld[0] * jac[0, 0]) if fld else 50.0
            if max(abs(p[1] - q[1]), abs(p[2] - q[2])) > 0.25 * span or p[3] != q[3]:
                return None
        return p

    def _map_ptz_pick(self, tracks, ts):
        """The user clicked a PTZ track while the eye leads: find where it is in the eye's picture. The PTZ picture
        shows the pose of PTZ_LAG_S ago, and the sync map's aim is off by the learned correction there."""
        p = next((x for x in tracks if x.id == self._ptz_pick), None)
        if p is None:
            self._ptz_pick = None
            return
        then = self._pose_at(_mono(ts) - self.PTZ_LAG_S)
        if then is not None:
            pan, tilt, zp = then[1], then[2], then[3]
        else:  # no usable history (idle at Home, or it just zoomed): the current pose, if the picture shows it
            pose, zp = self.ptz.pose_estimate(), self.ptz.zoom_pos
            settled = time.monotonic() - max(self._aim_t, self._zoom_t) >= self.PTZ_LAG_S
            if pose is None or zp is None or not settled or not self.ptz.pt_arrived or self.ptz.is_moving():
                if time.monotonic() - self._ptz_pick_t > self.PICK_TIMEOUT_S:
                    self._ptz_pick = None  # never still long enough to tell where it was
                return  # swinging: try again on a later frame
            pan, tilt = pose
        cu, cv = self.sync.to_eye(pan - self._bias[0], tilt - self._bias[1])
        fld = self.sync.field(cu, cv, zp, self.cfg.data["ptz"]["zoom_curve"])
        if fld is None:
            self._ptz_pick = None
            return
        self._ptz_pick = (cu + (p.center[0] - 0.5) * fld[0], cv + (p.center[1] - 0.5) * fld[1], p.category, ts)

    def _take_ptz_pick(self, visible):
        """Match the placed click to an eye track, both at the moment the PTZ picture was taken: the eye's picture
        is about half a second newer, and in that time a walker moves about the gap to the next person."""
        u, v, cat, ptz_ts = self._ptz_pick
        self._ptz_pick = None
        cats = RIDER if cat in RIDER else {cat}
        when = ptz_ts - self.PTZ_LAG_S

        def there(t):
            dt = when - (t.last_ts - self.EYE_LAT_S)
            return math.dist((t.center[0] + float(t.vel[0]) * dt, t.center[1] + float(t.vel[1]) * dt), (u, v))
        near = [t for t in visible if t.category in cats and there(t) < 0.08]
        if near:
            t = min(near, key=there)
            self._set_eye_target(t)
            self.user_locked = True
            self.log.info("following %s #%d (picked on the PTZ view)", t.label, t.id)

    def _drive(self, pan: float, tilt: float):
        key = (round(pan * PAN_MAX), round(tilt * TILT_MAX))
        now = time.monotonic()
        if (key != self._pt_key and now - self._pt_t >= 0.2) or now - self._pt_t >= 0.5:
            if key == (0, 0):
                self.ptz.stop_pantilt()
            else:  # an axis below the slowest step stays still
                self.ptz.move(pan if key[0] else 0.0, tilt if key[1] else 0.0, speed=1.0, hold=0.8)
            self._pt_key, self._pt_t = key, now



def _mono(ts: float) -> float:
    """A frame's arrival time (wall clock) on the monotonic clock the pose history uses."""
    return time.monotonic() - (time.time() - ts)


def _image_speed(t) -> float:
    """How fast a track crosses the picture: its velocity, or its average since it appeared if higher (the
    velocity estimate lags during the first half second, which is when objects get picked)."""
    speed = math.hypot(*t.vel)
    trail, span = getattr(t, "trail", None), t.last_ts - t.first_ts
    if trail and 2 <= len(trail) < trail.maxlen and span > 0.1:  # a full trail no longer starts at first_ts
        speed = max(speed, math.dist(trail[-1], trail[0]) / span)
    return speed


def _seg_dist(p, a, b) -> float:
    """Distance from point p to the segment a-b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = dx * dx + dy * dy
    u = 0.0 if n == 0 else min(1.0, max(0.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / n))
    return math.hypot(p[0] - a[0] - u * dx, p[1] - a[1] - u * dy)
