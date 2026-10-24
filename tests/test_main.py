import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gpta.__main__ import EXIT_CONFIG, main


class MainTests(unittest.TestCase):
    def test_broken_config_exits_with_config_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.json").write_text("{not json")
            with patch("gpta.__main__._configure_logging"), self.assertLogs(
                "gpta", "ERROR"
            ) as logs:
                status = main(["--data-dir", tmp, "--no-scheduler"])
        self.assertEqual(status, EXIT_CONFIG)
        self.assertIn("cannot start", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
