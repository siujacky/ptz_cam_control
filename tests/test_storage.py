import tempfile
import time
import unittest
from pathlib import Path

from ptzcam.storage import Storage, add_days, day_start, event_json


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Storage(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def _event(self, ts, cats=None):
        eid = self.s.insert_event(ts - 5, ts, ts + 25, "person", {}, {}, 0.9)
        self.s.update_event(eid, categories=cats or {"person": 1}, snapshot=f"d/{eid}/snapshot.jpg",
                            thumb=f"d/{eid}/thumb.jpg", clip=f"d/{eid}/clip.mp4")
        return eid

    def test_event_json_and_delete_keeps_counts(self):
        now = time.time()
        eid = self._event(now, {"person": 1, "bicycle": 1})
        self.s.insert_track(eid, 1, "person", "person", now - 1, now, now + 1, 0.9, "ltr")
        self.s.insert_track(eid, 2, "bicycle", "bicycle", now - 1, now, now + 1, 0.8, "ltr")
        j = event_json(self.s.get_event(eid))
        self.assertIsNone(j["clip_url"])  # pending
        self.s.update_event(eid, clip_status="ready")
        self.s.bump_snapshot(eid)
        j = event_json(self.s.get_event(eid))
        self.assertEqual(j["clip_url"], f"/media/d/{eid}/clip.mp4")
        self.assertEqual(j["snapshot_url"], f"/media/d/{eid}/snapshot.jpg?v=1")
        self.assertEqual(j["duration"], 30.0)
        self.assertEqual(j["categories"], {"person": 1, "bicycle": 1})
        self.assertEqual(len(self.s.event_objects(eid)), 2)

        self.assertEqual([r["id"] for r in self.s.list_events(category="bicycle")[0]], [eid])
        self.assertEqual(self.s.list_events(category="animal")[0], [])
        before = self.s.counts_today()
        self.s.delete_event(eid)
        self.assertIsNone(self.s.get_event(eid))
        self.assertEqual(self.s.counts_today(), before)  # stats survive deleting the clip

    def test_paging(self):
        now = time.time()
        ids = [self._event(now - 100 * i) for i in range(5)]
        rows, more = self.s.list_events(limit=2)
        self.assertEqual([r["id"] for r in rows], ids[:2])
        self.assertTrue(more)
        rows, more = self.s.list_events(limit=10, before=rows[-1]["start_ts"])
        self.assertEqual([r["id"] for r in rows], ids[2:])
        self.assertFalse(more)

    def test_stats_buckets(self):
        day = "2026-01-15"  # a Thursday
        base = day_start(day) + 10 * 3600
        for i in range(10):
            self.s.insert_track(None, i, "vehicle", "car", base, base + i * 60, base, 0.8, "rtl" if i % 2 else "ltr")
        self.s.insert_track(None, 99, "person", "person", base, day_start(add_days(day, -1)) + 3600, base, 0.8, "ltr")

        st = self.s.stats("day", day)
        self.assertEqual(st["bucket"], "hour")
        self.assertEqual(len(st["series"]), 24)
        self.assertEqual(st["series"][10]["counts"]["vehicle"], 10)
        self.assertEqual(st["totals"]["vehicle"], 10)
        self.assertEqual(st["previous_totals"]["person"], 1)
        self.assertEqual(st["directions"]["vehicle"], {"ltr": 5, "rtl": 5, "approach": 0, "leave": 0, "unknown": 0})
        self.assertEqual(st["heatmap"][3][10], 10)  # Thursday 10:00
        self.assertEqual(st["busiest"], {"label": "10:00", "count": 10})

        week = self.s.stats("week", day)
        self.assertEqual(len(week["series"]), 7)
        self.assertEqual(week["series"][-1]["counts"]["vehicle"], 10)
        self.assertEqual(week["series"][-2]["counts"]["person"], 1)
        self.assertEqual(week["series"][-1]["label"], "Jan 15")
        self.assertEqual(len(self.s.stats("month", day)["series"]), 30)

        self.assertEqual(self.s.stats("day", day, category="person")["busiest"]["count"], 0)
        days = self.s.days()
        self.assertEqual([d["day"] for d in days], [day, add_days(day, -1)])
        self.assertEqual(days[0]["counts"]["vehicle"], 10)


if __name__ == "__main__":
    unittest.main()
