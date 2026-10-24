import os
import tempfile
import time
import unittest
from pathlib import Path

from gpta.module_api import Cache


class CacheTests(unittest.TestCase):
    def test_rejects_unsafe_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(Path(tmp))
            for key in ("", "../escape", "/absolute/path"):
                with self.subTest(key=key):
                    with self.assertRaises(ValueError):
                        cache.path(key)

    def test_size_cap_evicts_oldest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(Path(tmp), max_bytes=10)
            cache.write_text("a.txt", "12345")
            old = time.time() - 1000
            os.utime(cache.path("a.txt"), (old, old))
            cache.write_text("b.txt", "12345")
            cache.write_text("c.txt", "12345")
            self.assertFalse(cache.path("a.txt").exists())
            self.assertTrue(cache.path("c.txt").exists())

    def test_max_age_evicts_expired_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(Path(tmp), max_age_seconds=60)
            cache.write_text("old.txt", "x")
            old = time.time() - 1000
            os.utime(cache.path("old.txt"), (old, old))
            cache.write_text("new.txt", "y")
            self.assertFalse(cache.path("old.txt").exists())
            self.assertTrue(cache.path("new.txt").exists())

    def test_live_temp_files_survive_eviction(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(Path(tmp))
            live = Path(tmp) / ".a.txt.123.tmp"
            stale = Path(tmp) / ".b.txt.456.tmp"
            live.write_text("in progress")
            stale.write_text("crashed")
            old = time.time() - 2 * Cache.STALE_TMP_SECONDS
            os.utime(stale, (old, old))
            cache.write_text("c.txt", "x")
            self.assertTrue(live.exists())
            self.assertFalse(stale.exists())
            self.assertEqual(sorted(p.name for p in Path(tmp).glob("*.txt")), ["c.txt"])

    def test_undecodable_entry_reads_as_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(Path(tmp))
            cache.path("bad.txt").write_bytes(b"\xff\xfe\xfa")
            self.assertIsNone(cache.read_text("bad.txt"))

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Cache(Path(tmp))
            self.assertIsNone(cache.read_text("nested/item.txt"))
            cache.write_text("nested/item.txt", "value")
            self.assertEqual(cache.read_text("nested/item.txt"), "value")


if __name__ == "__main__":
    unittest.main()
