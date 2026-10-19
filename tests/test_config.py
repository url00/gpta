import json
import tempfile
import unittest
from pathlib import Path

from gpta.config import ConfigError, ConfigStore
from gpta.module_api import Block, Module


class EchoModule(Module):
    id = "echo"
    name = "Echo"
    description = "test"
    defaults = {
        "enabled": False,
        "interval_seconds": 60,
        "limits": {"max_items": 2, "max_chars": 500},
        "options": {"text": "hello"},
    }
    options_schema = {"text": {"type": "str", "label": "Text", "max_length": 50}}

    def fetch(self, ctx):
        return [Block("ECHO", (ctx.options["text"],))]


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.store = ConfigStore(self.path, {"echo": EchoModule()})
        self.store.load()

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_defaults(self):
        self.assertTrue(self.path.is_file())
        snap = self.store.snapshot()
        self.assertFalse(snap["modules"]["echo"]["enabled"])
        self.assertEqual(snap["printer"]["width"], 32)
        self.assertEqual(list(Path(self.tmp.name).glob("*.tmp")), [])

    def test_update_and_roundtrip(self):
        updated = self.store.update_module(
            "echo", {"enabled": True, "limits": {"max_items": 1}}
        )
        self.assertTrue(updated["enabled"])
        self.assertEqual(updated["limits"]["max_items"], 1)
        second = ConfigStore(self.path, {"echo": EchoModule()})
        second.load()
        self.assertEqual(second.module_config("echo")["limits"]["max_items"], 1)

    def test_invalid_updates_rejected(self):
        with self.assertRaises(ConfigError):
            self.store.update_module("echo", {"interval_seconds": 1})
        with self.assertRaises(ConfigError):
            self.store.update_module("echo", {"options": {"nope": 1}})
        with self.assertRaises(ConfigError):
            self.store.update_module("echo", {"unknown": True})
        with self.assertRaises(ConfigError):
            self.store.update_printer({"width": 40})

    def test_unknown_module_config_ignored(self):
        data = json.loads(self.path.read_text())
        data["modules"]["ghost"] = {"enabled": True}
        self.path.write_text(json.dumps(data))
        store = ConfigStore(self.path, {"echo": EchoModule()})
        store.load()
        self.assertNotIn("ghost", store.snapshot()["modules"])


if __name__ == "__main__":
    unittest.main()
