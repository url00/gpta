import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gpta.__main__ import ModuleFormatter
from gpta.config import ConfigStore
from gpta.logbus import BusLogHandler, LogBus
from gpta.module_api import Block, Module
from gpta.service import Service


class EmitModule(Module):
    id = "emit"
    name = "Emit"
    description = "emits"
    defaults = {
        "enabled": False,
        "interval_seconds": 60,
        "limits": {"max_items": 1, "max_chars": 500},
        "options": {},
    }

    def fetch(self, ctx):
        ctx.emit("debug detail", "debug")
        ctx.emit("careful", "warn")
        return [Block("EMIT", ("hello",))]


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        registry = {"emit": EmitModule()}
        store = ConfigStore(Path(self.tmp.name) / "config.json", registry)
        store.load()
        self.bus = LogBus()
        self.logger = logging.getLogger("gpta")
        self.old_level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.handler = BusLogHandler(self.bus)
        self.logger.addHandler(self.handler)
        self.store = store
        self.service = Service(store, registry, self.bus, fetch_timeout=1.0)

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.old_level)
        self.service.stop()
        self.tmp.cleanup()

    def entries(self):
        return self.bus.since(0)

    def test_module_logs_carry_id_once(self):
        self.service.run_module("emit")
        mine = [e for e in self.entries() if e["module"] == "emit"]
        self.assertTrue(mine)
        self.assertFalse(any(e["message"].startswith("[emit]") for e in mine))

    def test_debug_and_warn_levels_reach_the_bus(self):
        self.service.run_module("emit")
        levels = {e["message"]: e["level"] for e in self.entries()}
        self.assertEqual(levels.get("debug detail"), "debug")
        self.assertEqual(levels.get("careful"), "warning")

    def test_skipped_print_is_visible(self):
        self.store.update_printer(
            {"enabled": True, "auto_print": True, "min_print_spacing_seconds": 600}
        )
        with patch("gpta.printing.transport.send_to_device"):
            self.service.run_module("emit")
            self.service.run_module("emit")
        skipped = [e for e in self.entries() if "print skipped" in e["message"]]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["level"], "info")

    def test_text_sinks_prefix_module(self):
        formatter = ModuleFormatter("%(message)s")
        record = logging.LogRecord("gpta.service", logging.INFO, "", 0, "hi", (), None)
        record.gpta_module = "emit"
        self.assertEqual(formatter.format(record), "[emit] hi")
        plain = logging.LogRecord("gpta.service", logging.INFO, "", 0, "hi", (), None)
        self.assertEqual(formatter.format(plain), "hi")


if __name__ == "__main__":
    unittest.main()
