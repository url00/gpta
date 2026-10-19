import tempfile
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


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = {"fake": FakeModule(), "failing": FailingModule()}
        self.store = ConfigStore(Path(self.tmp.name) / "config.json", self.registry)
        self.store.load()
        self.bus = LogBus()
        self.service = Service(
            self.store, self.registry, self.bus, modules_dir=Path(self.tmp.name)
        )
        self.service._runtime["fake"].last_run_epoch = None
        self.service._runtime["failing"].last_run_epoch = None

    def tearDown(self):
        self.tmp.cleanup()

    def test_manual_run_while_disabled(self):
        result = self.service.run_module("fake")
        self.assertTrue(result["ok"])
        self.assertIn("hello", result["module"]["output"]["text"])
        self.assertEqual(result["module"]["runtime"]["run_count"], 1)
        self.assertEqual(self.service.output_text("total"), "No output from enabled modules.\n")

    def test_tick_respects_enabled(self):
        self.service.tick()
        self.assertEqual(self.service._runtime["fake"].run_count, 0)
        self.store.update_module("fake", {"enabled": True})
        self.service.tick()
        self.assertEqual(self.service._runtime["fake"].run_count, 1)
        self.assertIn("hello", self.service.output_text("total"))

    def test_printer_enabled_and_failure(self):
        self.store.update_printer({"enabled": True, "auto_print": True})
        with patch("gpta.service.send_to_device") as send:
            result = self.service.run_module("fake")
        self.assertTrue(result["printed"])
        send.assert_called_once()
        self.assertTrue(self.service.snapshot()["printer"]["last_job"])
        with patch("gpta.service.send_to_device", side_effect=PrinterError("no device")):
            result = self.service.run_module("fake")
        self.assertTrue(result["ok"])
        self.assertFalse(result["printed"])
        self.assertEqual(result["module"]["runtime"]["print_error"], "no device")

    def test_fetch_error_recorded(self):
        result = self.service.run_module("failing")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "boom")
        module = next(
            item for item in self.service.snapshot()["modules"] if item["id"] == "failing"
        )
        self.assertEqual(module["runtime"]["last_error"], "boom")
        self.assertFalse(module["runtime"]["last_ok"])

    def test_concurrent_run_rejected(self):
        self.service._runtime["fake"].running = True
        with self.assertRaises(AlreadyRunning):
            self.service.run_module("fake")


if __name__ == "__main__":
    unittest.main()
