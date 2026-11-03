"""PTZController absolute pan/tilt (pan_tilt_to): coalesced goals, arrival, cancelling, and cameras that refuse it."""
import threading
import time
import unittest

from ptzcam import ptz
from ptzcam.ptz import PTZController, ViscaError, _nibbles

from .helpers import FakeConfig

ABS = b"\x01\x06\x02"
PT_STOP = b"\x01\x06\x01" + bytes(ptz.STOP)


class FakeVisca:
    """Answers position inquiries from a simulated pose; absolute moves jump the pose to the goal after `lag`."""

    def __init__(self, refuse_abs=False, busy_first=0, jump=True):
        self.sent, self.pan, self.tilt, self.zoom = [], 0, -431, 1428
        self.refuse_abs, self.busy, self.jump = refuse_abs, busy_first, jump  # jump False: still travelling
        self.down = False  # the link is down: every packet fails as a lost socket does
        self.fail_polls = 0  # this many position inquiries fail as a lost socket does (a glitch)
        self.fail_abs = False  # absolute moves fail as a lost socket does (VISCA down, HTTP still up)
        self.on_abs = None  # called while an absolute move is being sent (another thread acts meanwhile)
        self.lock = threading.Lock()

    def send(self, body):
        if self.down or (body.startswith(ABS) and self.fail_abs):
            raise ConnectionResetError(104, "Connection reset by peer")
        if body == b"\x09\x06\x12" and self.fail_polls:
            self.fail_polls -= 1
            raise ConnectionResetError(104, "Connection reset by peer")
        if body.startswith(ABS) and self.on_abs is not None:
            self.on_abs()
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
                if self.jump:
                    p, t = body[5:9], body[9:13]
                    self.pan = ptz._s16(ptz._from_nibbles(p))
                    self.tilt = ptz._s16(ptz._from_nibbles(t))
            return b"\x90\x51\xff"

    def close(self):
        pass

    def abs_sent(self):
        with self.lock:
            return [b for b in self.sent if b.startswith(ABS)]

    def stops_after_last_abs(self):
        with self.lock:
            last = max((i for i, b in enumerate(self.sent) if b.startswith(ABS)), default=None)
            return [] if last is None else [b for b in self.sent[last + 1:] if b == PT_STOP]


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
        c.http_sent = []
        c._http = lambda *args: c.http_sent.append(args)  # the HTTP fallback, recorded
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


class StopRaceTest(PointTest):
    def test_a_stop_while_the_goal_is_being_sent_still_stops_the_camera(self):
        c, v = self.make(jump=False)
        v.on_abs = lambda: (v.__setattr__("on_abs", None), c.stop())  # the user's Stop lands during the send
        c.pan_tilt_to(2000, -431, 0.2)
        self.assertTrue(wait(lambda: v.abs_sent()))
        self.assertTrue(wait(lambda: v.stops_after_last_abs()), "no pan/tilt stop after the goal went out")
        self.assertIsNone(c.pt_goal)

    def test_switching_ptz_control_off_stops_a_swing(self):
        for via_stop in (True, False):  # set_enabled(False) calls stop(); the worker's own check must do it too
            with self.subTest(via_stop=via_stop):
                c, v = self.make(jump=False)
                c.pan_tilt_to(2000, -431, 0.2)  # about 8 s of travel
                self.assertTrue(wait(lambda: v.abs_sent()))
                time.sleep(0.2)
                c.cfg.data["ptz"]["enabled"] = False
                if via_stop:
                    c.stop()
                self.assertTrue(wait(lambda: v.stops_after_last_abs()))
                time.sleep(0.4)
                self.assertEqual(len(v.stops_after_last_abs()), 1)  # once, not on every pass

    def test_an_idle_camera_gets_nothing_when_ptz_control_is_switched_off(self):
        c, v = self.make()
        time.sleep(0.3)  # the worker's start-up stops have gone out
        with v.lock:
            before = len(v.sent)
        c.cfg.data["ptz"]["enabled"] = False
        time.sleep(0.4)
        with v.lock:
            after = [b for b in v.sent[before:] if b.startswith(b"\x01")]
        self.assertEqual(after, [])  # its own remote stays in charge

    def test_a_goal_is_dropped_while_visca_is_down_and_never_replayed(self):
        c, v = self.make(jump=False)
        v.down = True
        c.pan_tilt_to(300, -431)
        self.assertTrue(wait(lambda: c.pt_goal is None))  # dropped: absolute moves have no HTTP form
        self.assertIsNone(c._want_abs)
        time.sleep(0.3)
        self.assertTrue(c.connected)  # the worker carries on over HTTP
        v.down = False
        c._visca_retry = 0.0  # VISCA is back
        c.zoom(direction="in", speed=0.5, hold=0.2)  # any command that goes out over VISCA
        self.assertTrue(wait(lambda: c._visca_ok))
        time.sleep(0.4)
        self.assertEqual(v.abs_sent(), [])  # the old goal was not sent minutes late

    def test_a_sent_goal_is_not_sent_again_after_a_reconnect(self):
        c, v = self.make()
        c.pan_tilt_to(120, -400)
        self.assertTrue(wait(lambda: v.pan == 120))
        self.assertIsNone(c._want_abs)
        with c._cond:
            c._sent_abs = None  # as the worker's error path does
        time.sleep(0.4)
        self.assertEqual(len(v.abs_sent()), 1)

    def test_a_swing_resumes_after_a_glitch_mid_way(self):
        c, v = self.make(jump=False)
        c.pan_tilt_to(800, -150, 1.0)
        self.assertTrue(wait(lambda: v.abs_sent()))
        v.fail_polls = 1  # one position inquiry fails while it travels
        self.assertTrue(wait(lambda: len(v.abs_sent()) == 2, timeout=5), "the goal was not sent again")
        self.assertEqual(c.pt_goal, (800, -150))

    def test_a_goal_that_cannot_be_sent_stops_the_one_still_travelling(self):
        c, v = self.make(jump=False)
        c.pan_tilt_to(2000, -431, 0.2)  # about 8 s of travel
        self.assertTrue(wait(lambda: v.abs_sent()))
        v.fail_abs = True  # VISCA goes down for absolute moves; HTTP still works
        c.pan_tilt_to(1500, -431, 0.2)
        self.assertTrue(wait(lambda: ("ptzstop", 10, 10) in c.http_sent or v.stops_after_last_abs(), timeout=5),
                        "the camera was left driving to the old goal")

    def test_a_stop_during_the_send_does_not_mark_the_goal_sent(self):
        c, v = self.make(jump=False)
        v.on_abs = lambda: (v.__setattr__("on_abs", None), c.stop())
        c.pan_tilt_to(800, -150, 1.0)
        self.assertTrue(wait(lambda: v.abs_sent()))
        self.assertTrue(wait(lambda: v.stops_after_last_abs()))
        self.assertIsNone(c._sent_abs)
        c.pan_tilt_to(800, -150, 1.0)  # the same goal again (someone standing still)
        self.assertTrue(wait(lambda: len(v.abs_sent()) == 2), "the repeat goal was skipped as already sent")

    def test_ptz_control_off_after_a_glitch_still_stops_the_swing(self):
        c, v = self.make(jump=False)
        c.pan_tilt_to(2000, -431, 0.2)
        self.assertTrue(wait(lambda: v.abs_sent()))
        with c._cond:  # as the worker's error path leaves it: it forgot what it sent, not the goal
            c._sent_abs = c._want_abs = None
            c._sent_pt = None
        c.cfg.data["ptz"]["enabled"] = False
        c.stop()
        self.assertTrue(wait(lambda: v.stops_after_last_abs()), "no stop after PTZ control was switched off")

    def test_pose_and_arrival_survive_the_goal_vanishing_between_two_reads(self):
        class Racy(PTZController):
            """pt_goal reads alternate between a goal and None: another thread (stop, home) clears it in between."""
            reads = 0

            @property
            def pt_goal(self):
                self.reads += 1
                return (800, -431) if self.reads % 2 else None

            @pt_goal.setter
            def pt_goal(self, value):
                pass

        c = Racy(FakeConfig(ptz={"home_preset": 0}))
        c.pan_pos, c.tilt_pos, c._pt_from = 10, -431, (0, -431)
        c._pt_goal_t, c._pt_travel = time.monotonic(), 10.0  # travelling
        for _ in range(4):  # both phases of the alternation
            c.pt_arrived
            c.pose_estimate()

if __name__ == "__main__":
    unittest.main()
