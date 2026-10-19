import tempfile
import unittest
from pathlib import Path

from gpta import DEFAULT_MODULES_DIR
from gpta.registry import RegistryError, discover

VALID_MODULE = (
    "from gpta.module_api import Module, Block\n"
    "class M(Module):\n"
    "    id = '{id}'\n"
    "    name = 'Good'\n"
    "    description = ''\n"
    "    defaults = {{'enabled': False, 'interval_seconds': 60, "
    "'limits': {{'max_items': 1, 'max_chars': 100}}, 'options': {{}}}}\n"
    "    def fetch(self, ctx):\n"
    "        return [Block('x', ('y',))]\n"
)


class RegistryTests(unittest.TestCase):
    def test_discovers_project_modules(self):
        errors: dict[str, str] = {}
        modules = discover(DEFAULT_MODULES_DIR, errors=errors)
        self.assertEqual(
            sorted(modules),
            ["affirmations", "google_tasks", "gutenberg", "hackernews"],
        )
        self.assertEqual(errors, {})

    def test_folder_id_mismatch_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "wrong"
            folder.mkdir()
            (folder / "module.py").write_text(VALID_MODULE.format(id="other"))
            errors: dict[str, str] = {}
            modules = discover(Path(tmp), errors=errors)
        self.assertEqual(modules, {})
        self.assertIn("wrong", errors)
        self.assertIn("must match folder", errors["wrong"])

    def test_broken_module_does_not_hide_healthy_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good"
            good.mkdir()
            (good / "module.py").write_text(VALID_MODULE.format(id="good"))

            broken = Path(tmp) / "broken"
            broken.mkdir()
            (broken / "module.py").write_text("raise RuntimeError('kaboom')\n")

            errors: dict[str, str] = {}
            modules = discover(Path(tmp), errors=errors)
        self.assertEqual(sorted(modules), ["good"])
        self.assertIn("broken", errors)
        self.assertIn("kaboom", errors["broken"])

    def test_missing_directory_still_raises(self):
        with self.assertRaises(RegistryError):
            discover(Path("/nonexistent/gpta-modules"))


if __name__ == "__main__":
    unittest.main()
