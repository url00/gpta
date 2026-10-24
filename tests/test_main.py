import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gpta.__main__ import EXIT_CONFIG, _stderr_is_journal, local_url, main


class MainTests(unittest.TestCase):
    def test_broken_config_exits_with_config_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.json").write_text("{not json")
            with (
                patch("gpta.__main__._configure_logging"),
                self.assertLogs("gpta", "ERROR") as logs,
            ):
                status = main(["--data-dir", tmp, "--no-scheduler"])
        self.assertEqual(status, EXIT_CONFIG)
        self.assertIn("cannot start", "\n".join(logs.output))

    def test_print_url_reads_config_and_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.json").write_text(
                '{"server": {"host": "0.0.0.0", "port": 9001}}'
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["--data-dir", tmp, "--print-url"]), 0)
                main(["--data-dir", tmp, "--print-url", "--port", "9002"])
            self.assertEqual(
                out.getvalue().split(),
                ["http://127.0.0.1:9001", "http://127.0.0.1:9002"],
            )
            self.assertFalse((Path(tmp) / "server.log").exists())

    def test_journal_detection_checks_stderr_itself(self):
        info = os.fstat(2)
        real = f"{info.st_dev}:{info.st_ino}"
        for value, expected in ((real, True), ("1:2", False), ("junk", False)):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"JOURNAL_STREAM": value}):
                    self.assertEqual(_stderr_is_journal(), expected)
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_stderr_is_journal())

    def test_local_url(self):
        self.assertEqual(local_url("::", 1), "http://[::1]:1")
        self.assertEqual(local_url("192.168.1.5", 2), "http://192.168.1.5:2")


if __name__ == "__main__":
    unittest.main()
