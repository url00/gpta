import json
import tempfile
import unittest
from pathlib import Path

from gpta.config import ConfigStore
from gpta.logbus import LogBus
from gpta.module_api import Block, FetchError, Module
from gpta.service import Service


class DailyModule(Module):
    id = "daily"
    name = "Daily"
    description = "runs once a day"
    defaults = {
        "enabled": True,
        "interval_seconds": 86400,
        "limits": {"max_chars": 500},
        "options": {},
    }
    fail = False

    def fetch(self, ctx):
        if self.fail:
            raise FetchError("down")
        return [Block("DAILY", ("hello",))]


class RestartTests(unittest.TestCase):
    """Each Service built on the same data dir stands in for a restart."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.wall = [1_800_000_000.0]
        self.mono = [100.0]
        self.module = DailyModule()
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.stop()
        self.tmp.cleanup()

    def start(self):
        registry = {"daily": self.module}
        store = ConfigStore(self.data / "config.json", registry)
        store.load()
        service = Service(
            store,
            registry,
            LogBus(),
            clock=lambda: self.wall[0],
            monotonic=lambda: self.mono[0],
            fetch_timeout=2.0,
        )
        self.services.append(service)
        return service

    def tick(self, service):
        service.tick()
        self.assertTrue(service.wait_for_idle(3))

    def runtime(self, service):
        return service.runner._runtime["daily"]

    def restart(self, advance_wall, mono_after_boot=5.0):
        self.wall[0] += advance_wall
        self.mono[0] = mono_after_boot  # a new process's monotonic clock
        return self.start()

    def test_restart_does_not_rerun_recent_module(self):
        first = self.start()
        self.tick(first)
        self.assertEqual(self.runtime(first).run_count, 1)

        second = self.restart(advance_wall=3600)
        self.tick(second)
        runtime = self.runtime(second)
        self.assertEqual(runtime.run_count, 1)  # restored, not re-run
        self.assertTrue(runtime.last_ok)
        module = second.snapshot()["modules"][0]
        self.assertEqual(module["runtime"]["next_due_seconds"], 86400 - 3600)
        self.assertIn("hello", module["output"]["text"])  # simulator keeps output
        self.assertIn("hello", second.output_text("total"))

    def test_overdue_module_runs_once_after_restart(self):
        first = self.start()
        self.tick(first)
        second = self.restart(advance_wall=2 * 86400)
        self.tick(second)
        self.tick(second)
        self.assertEqual(self.runtime(second).run_count, 2)

    def test_clock_going_backwards_counts_as_just_run(self):
        first = self.start()
        self.tick(first)
        second = self.restart(advance_wall=-7200)
        self.tick(second)
        self.assertEqual(self.runtime(second).run_count, 1)
        due = second.snapshot()["modules"][0]["runtime"]["next_due_seconds"]
        self.assertEqual(due, 86400)

    def test_backoff_survives_restart(self):
        self.module.fail = True
        first = self.start()
        self.tick(first)
        second = self.restart(advance_wall=60)
        runtime = second.snapshot()["modules"][0]["runtime"]
        self.assertEqual(runtime["consecutive_failures"], 1)
        self.assertEqual(runtime["last_error"], "down")
        self.assertEqual(runtime["effective_interval_seconds"], 2 * 86400)

    def test_bad_state_file_is_ignored(self):
        for content in ("{not json", '{"version": 99}', '["x"]'):
            with self.subTest(content=content):
                (self.data / "state.json").write_text(content)
                with self.assertLogs("gpta.state", "WARNING"):
                    service = self.start()
                self.assertIsNone(self.runtime(service).last_run_at)

    def test_invalid_fields_and_unknown_modules_are_skipped(self):
        (self.data / "state.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "modules": {
                        "ghost": {"last_run_ts": 1.0},
                        "daily": {
                            "last_run_ts": "yesterday",
                            "run_count": -3,
                            "last_ok": "yes",
                            "output": {"lines": [1, 2], "generated_at": "x"},
                        },
                    },
                }
            )
        )
        service = self.start()
        runtime = self.runtime(service)
        self.assertIsNone(runtime.last_run_mono)
        self.assertEqual(runtime.run_count, 0)
        self.assertIsNone(runtime.last_ok)
        self.assertIsNone(service.outputs.get("daily"))

    def test_state_file_is_written_after_each_run(self):
        service = self.start()
        service.run_module("daily")
        saved = json.loads((self.data / "state.json").read_text())
        entry = saved["modules"]["daily"]
        self.assertEqual(entry["last_run_ts"], self.wall[0])
        self.assertEqual(entry["run_count"], 1)
        self.assertIn("hello", "\n".join(entry["output"]["lines"]))
        self.assertEqual(list(self.data.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
