import json
import tempfile
import unittest
from pathlib import Path

from gpta.config import ConfigError, ConfigStore, validate_module_defaults
from gpta.module_api import MAX_ITEMS_LIMIT, Block, Module


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
    limits_schema = {"max_items": MAX_ITEMS_LIMIT}
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
        self.assertEqual(snap["printer"]["paper"], "80mm")
        self.assertNotIn("width", snap["printer"])
        self.assertEqual(snap["server"]["run_timeout_seconds"], 120)
        self.assertEqual(snap["printer"]["min_print_spacing_seconds"], 30)
        self.assertEqual(snap["printer"]["failure_threshold"], 3)
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
        for patch in ({"paper": "A4"}, {"paper": 80}, {"width": 48}):
            with self.subTest(patch=patch), self.assertRaises(ConfigError):
                self.store.update_printer(patch)
        self.assertEqual(self.store.update_printer({"paper": "58mm"})["paper"], "58mm")

    def test_printer_device_must_stay_under_dev(self):
        for device in ("/etc/passwd", "/dev/../etc/passwd", "/dev", "dev/lp0"):
            with self.subTest(device=device):
                with self.assertRaises(ConfigError):
                    self.store.update_printer({"device": device})
        self.store.update_printer({"device": "/dev/null"})

    def test_allowed_hosts_normalised(self):
        data = json.loads(self.path.read_text())
        data["server"]["allowed_hosts"] = ["localhost", " GPTA.Example.Test "]
        self.path.write_text(json.dumps(data))
        store = ConfigStore(self.path, {"echo": EchoModule()})
        store.load()
        self.assertEqual(
            store.snapshot()["server"]["allowed_hosts"],
            ["localhost", "gpta.example.test"],
        )

    def test_cups_queue_validated(self):
        self.store.update_printer({"cups_queue": "POS-80"})
        self.assertEqual(self.store.snapshot()["printer"]["cups_queue"], "POS-80")
        for bad in ("two words", "-o raw;rm", "x" * 200, 5):
            with self.subTest(queue=bad):
                with self.assertRaises(ConfigError):
                    self.store.update_printer({"cups_queue": bad})

    def test_print_guard_config_is_validated(self):
        with self.assertRaises(ConfigError):
            self.store.update_printer({"min_print_spacing_seconds": -1})
        with self.assertRaises(ConfigError):
            self.store.update_printer({"max_prints_per_hour": 0})
        with self.assertRaises(ConfigError):
            self.store.update_printer({"failure_threshold": 0})
        with self.assertRaises(ConfigError):
            self.store.update_printer({"write_timeout_seconds": 0})

    def write_raw(self, mutate):
        data = json.loads(self.path.read_text())
        mutate(data)
        self.path.write_text(json.dumps(data))

    def reload(self, module=None):
        store = ConfigStore(self.path, {"echo": module or EchoModule()})
        with self.assertLogs("gpta.config", "WARNING") as logs:
            store.load()
        return store, "\n".join(logs.output)

    def test_new_schema_field_takes_default(self):
        self.store.update_module("echo", {"enabled": True})

        class EchoV2(EchoModule):
            defaults = {
                **EchoModule.defaults,
                "options": {"text": "hello", "loud": False},
            }
            options_schema = {
                **EchoModule.options_schema,
                "loud": {"type": "bool", "label": "Loud"},
            }

        store = ConfigStore(self.path, {"echo": EchoV2()})
        store.load()
        cfg = store.module_config("echo")
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["options"], {"text": "hello", "loud": False})

    def test_removed_and_invalid_fields_fall_back(self):
        def mutate(data):
            data["modules"]["echo"]["options"]["gone"] = 1
            data["modules"]["echo"]["options"]["text"] = "x" * 500
            data["modules"]["echo"]["interval_seconds"] = 1
            data["modules"]["echo"]["enabled"] = True
            data["printer"]["paper"] = "A4"
            data["printer"]["feed_lines"] = 5
            data["server"]["bogus"] = True
            data["extra"] = {}

        self.write_raw(mutate)
        store, logs = self.reload()
        cfg = store.module_config("echo")
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["interval_seconds"], 60)
        self.assertEqual(cfg["options"], {"text": "hello"})
        printer = store.snapshot()["printer"]
        self.assertEqual(printer["paper"], "80mm")
        self.assertEqual(printer["feed_lines"], 5)
        for fragment in (
            "gone",
            "longer than",
            "interval_seconds",
            "paper",
            "bogus",
            "extra",
        ):
            self.assertIn(fragment, logs)

    def test_old_column_width_is_dropped(self):
        # Before receipts were images, printer.width was 32 or 48 columns.
        def mutate(data):
            del data["printer"]["paper"]
            data["printer"]["width"] = 32
            data["printer"]["cut"] = False

        self.write_raw(mutate)
        store, logs = self.reload()
        printer = store.snapshot()["printer"]
        self.assertNotIn("width", printer)
        self.assertEqual(printer["paper"], "80mm")
        self.assertFalse(printer["cut"])
        self.assertIn("unknown key: width", logs)
        store.update_printer({"paper": "58mm"})
        self.assertNotIn("width", json.loads(self.path.read_text())["printer"])

    def test_unreadable_file_is_fatal(self):
        self.path.write_text("{not json")
        with self.assertRaises(ConfigError):
            ConfigStore(self.path, {"echo": EchoModule()}).load()

    def test_api_updates_stay_strict(self):
        with self.assertRaises(ConfigError):
            self.store.update_module("echo", {"options": {"text": "x" * 500}})
        with self.assertRaises(ConfigError):
            self.store.update_module("echo", {"limits": "nope"})

    def test_unknown_module_config_ignored(self):
        data = json.loads(self.path.read_text())
        data["modules"]["ghost"] = {"enabled": True}
        self.path.write_text(json.dumps(data))
        store = ConfigStore(self.path, {"echo": EchoModule()})
        store.load()
        self.assertNotIn("ghost", store.snapshot()["modules"])


class DefaultsValidationTests(unittest.TestCase):
    def test_valid_defaults_pass(self):
        validate_module_defaults(EchoModule())

    def test_inconsistent_defaults_rejected(self):
        class MissingOption(EchoModule):
            defaults = {**EchoModule.defaults, "options": {}}

        class BadValue(EchoModule):
            defaults = {**EchoModule.defaults, "options": {"text": 5}}

        class RedefinesCore(EchoModule):
            limits_schema = {
                "max_items": MAX_ITEMS_LIMIT,
                "max_chars": {"type": "int", "label": "x"},
            }

        class ExtraLimit(EchoModule):
            limits_schema = {}

        for cls in (MissingOption, BadValue, RedefinesCore, ExtraLimit):
            with self.subTest(cls=cls.__name__):
                with self.assertRaises(ConfigError):
                    validate_module_defaults(cls())


if __name__ == "__main__":
    unittest.main()
