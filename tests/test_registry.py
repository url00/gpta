import tempfile
import unittest
from pathlib import Path

from gpta import DEFAULT_MODULES_DIR
from gpta.registry import RegistryError, discover


class RegistryTests(unittest.TestCase):
    def test_discovers_project_modules(self):
        modules = discover(DEFAULT_MODULES_DIR)
        self.assertEqual(
            sorted(modules),
            ["affirmations", "google_tasks", "gutenberg", "hackernews"],
        )

    def test_folder_id_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "wrong"
            folder.mkdir()
            (folder / "module.py").write_text(
                "from gpta.module_api import Module, Block\n"
                "class M(Module):\n"
                "    id = 'other'\n"
                "    name = 'Other'\n"
                "    description = ''\n"
                "    defaults = {'enabled': False, 'interval_seconds': 60, "
                "'limits': {'max_items': 1, 'max_chars': 100}, 'options': {}}\n"
                "    def fetch(self, ctx):\n"
                "        return [Block('x', ('y',))]\n"
            )
            with self.assertRaises(RegistryError):
                discover(Path(tmp))


if __name__ == "__main__":
    unittest.main()
