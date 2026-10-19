import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from gpta.config import ConfigStore
from gpta.logbus import LogBus
from gpta.module_api import Block, FetchError, Module
from gpta.printer import PrinterError
from gpta.service import AlreadyRunning, Service


class FakeModule(Module):
    id = "fake"
    name = "Fake"
    description = "fake module"
    defaults = {
        "enabled": False,
        "interval_seconds": 60,
        "limits": {"max_items": 2, "max_chars": 500},
        "options": {"text": "hello"},
    }
    options_schema = {"text": {"type": "str", "label": "Text", "max_length": 50}}

    def fetch(self, ctx):
        return [Block("FAKE BODY", (ctx.options["text"],))]


class FailingModule(FakeModule):
    id = "failing"
    name = "Failing"

    def fetch(self, ctx):
        raise FetchError("boom")


class FlakyModule(FakeModule):
    id = "flaky"
    name = "Flaky"
    defaults = {
        "enabled": False,
        "interval_seconds": 5,
        "limits": {"max_items": 2, "max_chars": 500},
        "options": {"text": "hello"},
    }

    def __init__(self):
        self.fail = True

    def fetch(self, ctx):
        if self.fail:
            raise FetchError("nope")
        return super().fetch(ctx)


class SlowModule(FakeModule):
    id = "slow"
    name = "Slow"

    def __init__(self):
        self.release = threading.Event()

    def fetch(self, ctx):
        self.release.wait(10)
        return [Block("SLOW BODY", ("late",))]


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = {
            "fake": FakeModule(),
            "failing": FailingModule(),
            "flaky": FlakyModule(),
            "slow": SlowModule(),
        }
        self.store = ConfigStore(Path(self.tmp.name) / "config.json", self.registry)
        self.store.load()
        self.bus = LogBus()
        self.service = Service(
            self.store,
            self.registry,
            self.bus,
            modules_dir=Path(self.tmp.name),
            fetch_timeout=1.0,
        )

    def tearDown(self):
        self.registry["slow"].release.set()
        self.service.stop()
        self.tmp.cleanup()

    def run_scheduled(self, module_id: str, timeout: float = 3.0) -> None:
        self.store.update_module(module_id, {"enabled": True})
        self.service.tick()
        self.assertTrue(self.service.wait_for_idle(timeout))

    def test_manual_run_while_disabled(self):
        result = self.service.run_module("fake")
        self.assertTrue(result["ok"])
        self.assertIn("hello", result["module"]["output"]["text"])
        self.assertEqual(result["module"]["runtime"]["run_count"], 1)
        self.assertEqual(
            self.service.output_text("total"), "No output from enabled modules.\n"
        )

    def test_tick_respects_enabled(self):
        self.service.tick()
        self.assertTrue(self.service.wait_for_idle(1))
        self.assertEqual(self.service._runtime["fake"].run_count, 0)
        self.run_scheduled("fake")
        self.assertEqual(self.service._runtime["fake"].run_count, 1)
        self.assertIn("hello", self.service.output_text("total"))

    def test_tick_dispatches_without_waiting(self):
        self.store.update_module("slow", {"enabled": True})
        self.store.update_module("fake", {"enabled": True})
        started = time.monotonic()
        self.service.tick()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5)
        self.assertTrue(self.service.wait_for_idle(3))
        self.assertEqual(self.service._runtime["fake"].run_count, 1)
        self.assertFalse(self.service._runtime["slow"].last_ok)
        self.assertIn("timeout after", self.service._runtime["slow"].last_error)

    def test_timeout_bounds_worker_and_recovers(self):
        self.service.fetch_timeout = 0.1
        started = time.monotonic()
        result = self.service.run_module("slow")
        elapsed = time.monotonic() - started
        self.assertFalse(result["ok"])
        self.assertIn("timeout after 0.1s", result["error"])
        self.assertLess(elapsed, 1.0)
        stuck = self.service.run_module("slow")
        self.assertFalse(stuck["ok"])
        self.assertIn("still executing", stuck["error"])

        self.registry["slow"].release.set()
        worker = self.service._stuck["slow"]
        worker.join(2)
        result = self.service.run_module("slow")
        self.assertTrue(result["ok"])

    def test_printer_enabled_and_failure(self):
        self.store.update_printer(
            {
                "enabled": True,
                "auto_print": True,
                "min_print_spacing_seconds": 0,
                "failure_threshold": 10,
            }
        )
        with patch("gpta.printer.send_to_device") as send:
            result = self.service.run_module("fake")
        self.assertTrue(result["printed"])
        send.assert_called_once()
        self.assertTrue(self.service.snapshot()["printer"]["last_job"])
        with patch(
            "gpta.printer.send_to_device", side_effect=PrinterError("no device")
        ):
            result = self.service.run_module("fake")
        self.assertTrue(result["ok"])
        self.assertFalse(result["printed"])
        self.assertEqual(result["module"]["runtime"]["print_error"], "no device")

    def test_fetch_error_recorded(self):
        result = self.service.run_module("failing")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "boom")
        module = next(
            item
            for item in self.service.snapshot()["modules"]
            if item["id"] == "failing"
        )
        self.assertEqual(module["runtime"]["last_error"], "boom")
        self.assertFalse(module["runtime"]["last_ok"])

    def test_concurrent_run_rejected(self):
        self.service._runtime["fake"].running = True
        with self.assertRaises(AlreadyRunning):
            self.service.run_module("fake")

    def test_failure_backoff_doubles_and_resets(self):
        self.store.update_module("flaky", {"interval_seconds": 5})
        first = self.service.run_module("flaky")
        self.assertFalse(first["ok"])
        self.assertEqual(first["module"]["runtime"]["consecutive_failures"], 1)
        self.assertEqual(
            first["module"]["runtime"]["effective_interval_seconds"], 10
        )
        second = self.service.run_module("flaky")
        self.assertEqual(second["module"]["runtime"]["consecutive_failures"], 2)
        self.assertEqual(
            second["module"]["runtime"]["effective_interval_seconds"], 20
        )
        self.registry["flaky"].fail = False
        third = self.service.run_module("flaky")
        self.assertTrue(third["ok"])
        self.assertEqual(third["module"]["runtime"]["consecutive_failures"], 0)
        self.assertEqual(
            third["module"]["runtime"]["effective_interval_seconds"], 5
        )

    def test_monotonic_scheduling_ignores_wall_clock_jump(self):
        wall = [1_000_000.0]
        mono = [0.0]
        service = Service(
            self.store,
            {"fake": FakeModule()},
            self.bus,
            clock=lambda: wall[0],
            monotonic=lambda: mono[0],
            fetch_timeout=1.0,
        )
        try:
            self.store.update_module("fake", {"enabled": True})
            service.tick()
            self.assertTrue(service.wait_for_idle(2))
            self.assertEqual(service._runtime["fake"].run_count, 1)

            mono[0] += 1.0
            wall[0] -= 86400  # backwards wall-clock jump
            service.tick()
            self.assertTrue(service.wait_for_idle(1))
            self.assertEqual(service._runtime["fake"].run_count, 1)

            mono[0] += 61.0
            wall[0] += 86400
            service.tick()
            self.assertTrue(service.wait_for_idle(2))
            self.assertEqual(service._runtime["fake"].run_count, 2)
        finally:
            service.stop()

    def test_emit_cap_suppresses_output(self):
        messages = []
        service = Service(
            self.store,
            {"fake": FakeModule()},
            self.bus,
            fetch_timeout=1.0,
        )
        try:
            emitter = service._make_emitter("fake")
            with patch("gpta.service.Service._log", side_effect=lambda *a, **k: messages.append(a)):
                for index in range(105):
                    emitter(f"line {index}")
            self.assertEqual(len(messages), 101)
            self.assertIn("further output suppressed", messages[-1][0])
            emitter("ignored")
            self.assertEqual(len(messages), 101)
        finally:
            service.stop()

    def test_state_omits_logs(self):
        state = self.service.snapshot()
        self.assertNotIn("logs", state)
        self.assertIn("registry_errors", state)
        self.assertIn("last_tick_age_seconds", state["server"])
        self.assertIn("circuit_open", state["printer"])


if __name__ == "__main__":
    unittest.main()
