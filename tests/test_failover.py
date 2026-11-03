import unittest

from ptzcam.failover import MIN_DWELL_S, Health, MainSelector

CFG = {"mode": "auto", "white": 0.5, "switch_after": 20.0, "return_after": 120.0}
CLEAR, WHITE, BLIND = Health(True, 0.02), Health(True, 0.8), Health(True, 0.99, blinded=True)
EYE = Health(True, 0.05)


def run(sel, seconds, ptz, eye=EYE, start=None, cfg=CFG):
    """Tick once a second; returns the times at which the main camera changed."""
    t0 = sel._t if hasattr(sel, "_t") else (start or 1000.0)
    changes = []
    for i in range(int(seconds)):
        now = t0 + i
        if sel.tick(now, cfg, ptz, eye):
            changes.append((now, sel.main))
    sel._t = t0 + int(seconds)
    return changes


class MainSelectorTest(unittest.TestCase):
    def test_one_camera_is_always_main(self):
        s = MainSelector()
        self.assertEqual(run(s, 60, WHITE, eye=None), [])
        self.assertEqual(s.main, "ptz")

    def test_a_washed_out_ptz_hands_over_after_switch_after(self):
        s = MainSelector()
        run(s, 40, CLEAR)
        changes = run(s, 30, WHITE)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0][1], "eye")
        self.assertGreaterEqual(changes[0][0] - 1040, 20)  # not before switch_after
        self.assertEqual(s.reason, "PTZ picture washed out")

    def test_blinded_hands_over_within_seconds(self):
        s = MainSelector()
        run(s, 40, CLEAR)
        changes = run(s, 10, BLIND)
        self.assertEqual(changes[0][1], "eye")
        self.assertLessEqual(changes[0][0] - 1040, 4)

    def test_passing_clouds_do_not_flap(self):
        s = MainSelector()
        run(s, 40, CLEAR)
        for _ in range(10):  # 15 s white, 3 s clearer: never 20 s in a row
            self.assertEqual(run(s, 15, WHITE), [])
            self.assertEqual(run(s, 3, Health(True, 0.3)), [])
        self.assertEqual(s.main, "ptz")

    def test_comes_back_only_after_a_long_clear_spell(self):
        s = MainSelector()
        run(s, 40, CLEAR)
        run(s, 30, BLIND)
        self.assertEqual(s.main, "eye")
        self.assertEqual(run(s, 60, CLEAR), [])
        self.assertEqual(run(s, 10, WHITE), [])  # clear spell broken: the timer restarts
        changes = run(s, 125, CLEAR)
        self.assertEqual([c[1] for c in changes], ["ptz"])
        self.assertEqual(s.reason, "PTZ picture clear again")

    def test_offline_cameras_switch_at_once_even_inside_the_dwell(self):
        s = MainSelector()
        run(s, 40, CLEAR)
        run(s, 5, BLIND)
        self.assertEqual(s.main, "eye")
        changes = run(s, 2, CLEAR, eye=Health(False))
        self.assertEqual(changes[0][1], "ptz")
        self.assertEqual(s.reason, "wide camera offline")
        changes = run(s, 2, Health(False), eye=EYE)
        self.assertEqual(changes[0][1], "eye")

    def test_a_switch_is_held_for_the_minimum_dwell(self):
        s = MainSelector()
        cfg = dict(CFG, return_after=1.0)
        run(s, 40, CLEAR, cfg=cfg)
        run(s, 5, BLIND, cfg=cfg)
        switched = s.since
        changes = run(s, 60, CLEAR, cfg=cfg)
        self.assertGreaterEqual(changes[0][0] - switched, MIN_DWELL_S)

    def test_no_handover_when_the_eye_is_washed_out_too(self):
        s = MainSelector()
        run(s, 40, CLEAR)
        self.assertEqual(run(s, 60, BLIND, eye=Health(True, 0.97, blinded=True)), [])

    def test_forced_modes(self):
        s = MainSelector()
        cfg = dict(CFG, mode="eye")
        run(s, 40, CLEAR, cfg=cfg)
        self.assertEqual(s.main, "eye")
        run(s, 40, CLEAR, cfg=dict(CFG, mode="ptz"))
        self.assertEqual(s.main, "ptz")


if __name__ == "__main__":
    unittest.main()
