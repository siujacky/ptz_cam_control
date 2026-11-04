"""Storage cleanup: the oldest events go first, to stay under the user's cap and to keep the disk from filling."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ptzcam import web
from ptzcam.storage import Storage
from tests.helpers import FakeBus, FakeConfig

MB = 1_000_000


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.media = root / "media"
        self.media.mkdir()
        self.storage = Storage(root / "t.db")
        self.ctx = SimpleNamespace(cfg=FakeConfig(storage={"retention_days": 30, "max_storage_gb": 100.0}),
                                   storage=self.storage, pipeline=SimpleNamespace(events=None), bus=FakeBus(),
                                   media_dir=self.media)
        now = time.time()
        self.ids = [self._event(now - 3600 * (5 - i), MB) for i in range(5)]  # oldest first, 1 MB each

    def tearDown(self):
        self.storage.close()
        self.tmp.cleanup()

    def _event(self, ts, size):
        eid = self.storage.insert_event(ts - 5, ts, ts + 25, "vehicle", {}, {}, 0.9)
        d = self.media / "2026-11-03" / str(eid)
        d.mkdir(parents=True)
        (d / "clip.mp4").write_bytes(b"\0" * size)
        self.storage.update_event(eid, categories={"vehicle": 1}, snapshot=f"2026-11-03/{eid}/snapshot.jpg",
                                  clip=f"2026-11-03/{eid}/clip.mp4")
        return eid

    def left(self):
        return [r["id"] for r in self.storage.oldest_events(100)]

    def disk(self, free):
        return mock.patch.object(web.shutil, "disk_usage",
                                 return_value=SimpleNamespace(total=100_000 * MB, used=0, free=free))

    def test_nothing_removed_under_the_cap_with_room_on_the_disk(self):
        with self.disk(50_000 * MB):
            web.run_retention(self.ctx)
        self.assertEqual(self.left(), self.ids)

    def test_cap_removes_the_oldest_first(self):
        self.ctx.cfg.data["storage"]["max_storage_gb"] = 3.5 * MB / 1e9
        with self.disk(50_000 * MB):
            web.run_retention(self.ctx)
        self.assertEqual(self.left(), self.ids[2:])
        self.assertFalse((self.media / "2026-11-03" / str(self.ids[0])).exists())

    def test_a_cap_larger_than_the_disk_still_keeps_room_free(self):
        # The cap (100 GB) is far away, but the disk is 1.5 MB short of the floor: two oldest events go.
        with self.disk(int(web.MIN_FREE_GB * 1e9) - int(1.5 * MB)):
            web.run_retention(self.ctx)
        self.assertEqual(self.left(), self.ids[2:])

    def test_the_larger_shortfall_wins(self):
        self.ctx.cfg.data["storage"]["max_storage_gb"] = 4.5 * MB / 1e9  # 0.5 MB over the cap: one event
        with self.disk(int(web.MIN_FREE_GB * 1e9) - int(2.5 * MB)):  # but 2.5 MB short of the floor: three
            web.run_retention(self.ctx)
        self.assertEqual(self.left(), self.ids[3:])

    def test_the_event_being_recorded_is_kept(self):
        self.ctx.pipeline = SimpleNamespace(events=SimpleNamespace(active=SimpleNamespace(id=self.ids[0]),
                                                                   on_deleted=lambda eid: None))
        with self.disk(0):  # the disk is full: everything else may go, the event being recorded may not
            web.run_retention(self.ctx)
        self.assertEqual(self.left(), [self.ids[0]])

    def test_status_says_how_far_back_recordings_go(self):
        first = self.storage.get_event(self.ids[0])["start_ts"]
        self.assertEqual(self.storage.oldest_start(), first)
        ctx = SimpleNamespace(pipeline=SimpleNamespace(status=lambda: {}), started=time.time(), storage=self.storage,
                              sysinfo=SimpleNamespace(snapshot=lambda: {"media_gb": 0.01}))
        self.assertEqual(web.build_status(ctx)["system"]["media_since"], first)
        for eid in self.ids:
            self.storage.delete_event(eid)
        self.assertIsNone(self.storage.oldest_start())


if __name__ == "__main__":
    unittest.main()
