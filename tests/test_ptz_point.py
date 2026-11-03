"""PTZController absolute pan/tilt (pan_tilt_to): coalesced goals, arrival, cancelling, and cameras that refuse it."""
import threading
import time
import unittest

from ptzcam import ptz
from ptzcam.ptz import PTZController, ViscaError, _nibbles

from .helpers import FakeConfig

ABS = b"\x01\x06\x02"


class FakeVisca:
    """Answers position inquiries from a simulated pose; absolute moves jump the pose to the goal after `lag`."""

    def __init__(self, refuse_abs=False, busy_first=0):
        self.sent, self.pan, self.tilt, self.zoom = [], 0, -431, 1428
        self.refuse_abs, self.busy = refuse_abs, busy_first
        self.lock = threading.Lock()

    def send(self, body):
        with self.lock:
            self.sent.append(body)
            if body == b"\x09\x06\x12":
                return b"\x90\x50" + _nibbles(self.pan) + _nibbles(self.tilt) + b"\xff"
            if body == b"\x09\x04\x47":
                return b"\x90\x50" + _nibbles(self.zoom) + b"\xff"
            if body.startswith(ABS):
                if self.refuse_abs:
                    raise ViscaError("camera replied error 90 60 02 ff")
                if self.busy:
                    self.busy -= 1
                    raise ViscaError("camera replied error 90 61 41 ff")
                p, t = body[5:9], body[9:13]
                self.pan = ptz._s16(ptz._from_nibbles(p))
                self.tilt = ptz._s16(ptz._from_nibbles(t))
            return b"\x90\x51\xff"

    def close(self):
        pass

    def abs_sent(self):
        with self.lock:
            return [b for b in self.sent if b.startswith(ABS)]


def wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class PointTest(unittest.TestCase):
    def make(self, **kw):
        cfg = FakeConfig(ptz={"home_preset": 0, "autofocus": False})
        visca = FakeVisca(**kw)
        c = PTZController(cfg)
        c._visca = visca
        c._make_client = lambda: visca
        c._home_checked = True  # no startup Home recall in these tests
        c.start()
        self.addCleanup(c.close)
        self.assertTrue(wait(lambda: c.pan_pos is not None))
        return c, visca

    def test_sends_one_absolute_move_with_signed_positions(self):
        c, v = self.make()
        self.assertTrue(c.pan_tilt_to(-300, -281, speed=0.5))
        self.assertTrue(wait(lambda: v.abs_sent()))
        body = v.abs_sent()[0]
        self.assertEqual(body, ABS + bytes([12, 10]) + _nibbles(-300) + _nibbles(-281))
        self.assertFalse(c.at_home)
        self.assertTrue(c.is_moving())
        self.assertTrue(wait(lambda: c.pt_arrived))

    def test_newer_goals_replace_older_ones_instead_of_queueing(self):
        c, v = self.make()
        with c._cond:  # hold the worker so three goals arrive before it can send any
            for p in (100, 200, 300):
                c._want_abs = None
                c.pan_tilt_to(p, -431)
        self.assertTrue(wait(lambda: v.abs_sent()))
        time.sleep(0.3)
        sent = v.abs_sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][5:9], _nibbles(300))

    def test_pose_estimate_moves_towards_the_goal_before_the_readout_catches_up(self):
        c, v = self.make()
        c.pan_tilt_to(250, -431)
        mid = c.pose_estimate()
        self.assertGreaterEqual(mid[0], 0)
        self.assertLessEqual(mid[0], 250)
        self.assertTrue(wait(lambda: c.pt_arrived))
        self.assertEqual(c.pose_estimate(), (250.0, -431.0))

    def test_stop_cancels_a_move_in_flight_and_stops_the_camera(self):
        c, v = self.make()
        c.pan_tilt_to(400, -431)
        self.assertTrue(wait(lambda: v.abs_sent()))
        with v.lock:
            v.pan = 100  # still travelling
        c.stop()
        self.assertTrue(wait(lambda: any(b == b"\x01\x06\x01" + bytes(ptz.STOP) for b in v.sent)))
        self.assertIsNone(c.pt_goal)

    def test_a_camera_that_refuses_absolute_moves_turns_them_off(self):
        c, v = self.make(refuse_abs=True)
        c.pan_tilt_to(100, -431)
        self.assertTrue(wait(lambda: not c.can_point))
        self.assertFalse(c.pan_tilt_to(120, -431))
        time.sleep(0.3)
        self.assertEqual(len(v.abs_sent()), 1)

    def test_a_busy_camera_gets_the_goal_again_shortly(self):
        c, v = self.make(busy_first=1)
        c.pan_tilt_to(150, -400)
        self.assertTrue(wait(lambda: v.pan == 150))
        self.assertEqual(len(v.abs_sent()), 2)
        self.assertTrue(c.can_point)


if __name__ == "__main__":
    unittest.main()
