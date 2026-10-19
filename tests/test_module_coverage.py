import re
import unittest
from pathlib import Path

from gpta import DEFAULT_MODULES_DIR
from gpta.registry import discover


class ModuleCoverageTests(unittest.TestCase):
    def test_every_module_has_a_hermetic_test(self):
        errors: dict[str, str] = {}
        modules = discover(DEFAULT_MODULES_DIR, errors=errors)
        self.assertEqual(errors, {}, "project modules must not be quarantined")

        source = Path(__file__).with_name("test_modules.py").read_text(
            encoding="utf-8"
        )
        test_names = re.findall(r"def (test_[A-Za-z0-9_]+)\(", source)
        missing = [
            module_id
            for module_id in modules
            if not any(
                name.startswith(f"test_{module_id}") for name in test_names
            )
        ]
        self.assertEqual(
            missing,
            [],
            "add a hermetic test named test_<module id> in tests/test_modules.py",
        )


if __name__ == "__main__":
    unittest.main()
