import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gpta.config import ConfigStore
from gpta.logbus import LogBus
from gpta.module_api import Block, Module
from gpta.printing import (
    PrinterError,
    PrinterRateLimited,
    PrinterWriter,
    send_to_device,
)
from gpta.service import Service


class FakeModule(Module):
    id = "fake"
    name = "Fake"
    description = "fake"
    defaults = {
        "enabled": False,
        "interval_seconds": 60,
        "limits": {"max_chars": 200},
        "options": {},
    }

    def fetch(self, ctx):
        return [Block("FAKE", ("hello",))]


IMAGE = Image.new("1", (8, 1), 1)


class PrinterSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = {"fake": FakeModule()}
        self.store = ConfigStore(Path(self.tmp.name) / "config.json", self.registry)
        self.store.load()
        self.service = Service(self.store, self.registry, LogBus(), fetch_timeout=1.0)

    def tearDown(self):
        self.service.stop()
        self.tmp.cleanup()

    def enable_printer(self, **overrides):
        config = {
            "enabled": True,
            "auto_print": True,
            "min_print_spacing_seconds": 0,
            "max_prints_per_hour": 100,
            "failure_threshold": 2,
        }
        config.update(overrides)
        self.store.update_printer(config)

    def test_writes_are_serialized(self):
        active = 0
        max_active = 0
        calls = 0
        lock = threading.Lock()

        def slow_send(lines, config):
            nonlocal active, max_active, calls
            with lock:
                active += 1
                calls += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1

        self.enable_printer()
        with patch("gpta.printing.transport.send_to_device", side_effect=slow_send):
            first = threading.Thread(target=self.service.run_module, args=("fake",))
            second = threading.Thread(target=self.service.test_printer)
            first.start()
            second.start()
            first.join(5)
            second.join(5)
        self.assertEqual(max_active, 1)
        self.assertEqual(calls, 2)

    def scheduled_run(self):
        return self.service.runner.run("fake", trigger="schedule")

    def test_minimum_spacing_skips_scheduled_autoprint(self):
        self.enable_printer(min_print_spacing_seconds=30)
        with patch("gpta.printing.transport.send_to_device") as send:
            first = self.scheduled_run()
            second = self.scheduled_run()
        self.assertTrue(first["printed"])
        self.assertFalse(second["printed"])
        self.assertIn("minimum spacing", second["print_skipped"])
        self.assertIn("minimum spacing", second["runtime"]["print_skipped"])
        self.assertEqual(send.call_count, 1)

    def test_manual_prints_skip_spacing(self):
        self.enable_printer(min_print_spacing_seconds=30)
        with patch("gpta.printing.transport.send_to_device") as send:
            self.service.test_printer()
            run = self.service.run_module("fake")  # Run now, right after
            self.service.test_printer()
        self.assertTrue(run["printed"])
        self.assertIsNone(run["print_skipped"])
        self.assertEqual(send.call_count, 3)
        # ...but a scheduled print still waits for the spacing.
        with patch("gpta.printing.transport.send_to_device") as send:
            self.assertFalse(self.scheduled_run()["printed"])
        send.assert_not_called()

    def test_skip_reason_clears_after_a_print(self):
        self.enable_printer(min_print_spacing_seconds=30)
        with patch("gpta.printing.transport.send_to_device"):
            self.scheduled_run()
            self.assertTrue(self.scheduled_run()["print_skipped"])
            self.assertIsNone(self.service.run_module("fake")["print_skipped"])

    def test_auto_print_off_is_reported(self):
        self.enable_printer(auto_print=False)
        with patch("gpta.printing.transport.send_to_device") as send:
            result = self.service.run_module("fake")
        send.assert_not_called()
        self.assertEqual(result["print_skipped"], "auto print is off")

    def test_printer_disabled_reports_nothing(self):
        with patch("gpta.printing.transport.send_to_device") as send:
            result = self.service.run_module("fake")
        send.assert_not_called()
        self.assertIsNone(result["print_skipped"])

    def test_hourly_cap_skips_autoprint(self):
        self.enable_printer(max_prints_per_hour=1)
        with patch("gpta.printing.transport.send_to_device") as send:
            first = self.service.run_module("fake")
            second = self.service.run_module("fake")
        self.assertTrue(first["printed"])
        self.assertFalse(second["printed"])
        self.assertEqual(send.call_count, 1)

    def test_manual_test_print_hits_hourly_cap(self):
        self.enable_printer(max_prints_per_hour=1)
        with patch("gpta.printing.transport.send_to_device"):
            self.service.test_printer()
            with self.assertRaises(PrinterRateLimited) as caught:
                self.service.test_printer()
        self.assertIn("hourly limit", str(caught.exception))

    def test_consecutive_failures_trip_circuit_breaker(self):
        self.enable_printer(failure_threshold=2)
        with patch(
            "gpta.printing.transport.send_to_device",
            side_effect=PrinterError("offline"),
        ):
            self.service.run_module("fake")
            self.assertTrue(self.store.snapshot()["printer"]["enabled"])
            self.service.run_module("fake")
        printer = self.service.snapshot()["printer"]
        self.assertFalse(printer["enabled"])
        self.assertTrue(printer["circuit_open"])
        self.assertIn("auto-disabled", printer["disabled_reason"])
        self.assertEqual(printer["last_error"], "offline")

    def test_prints_the_rendered_image_at_paper_width(self):
        self.enable_printer()
        for paper, width in (("80mm", 576), ("58mm", 384)):
            self.store.update_printer({"paper": paper})
            with self.subTest(paper=paper):
                with patch("gpta.printing.transport.send_to_device") as send:
                    self.service.run_module("fake")
                    self.service.test_printer()
                run_image, test_image = (c.args[0] for c in send.call_args_list)
                self.assertEqual((run_image.mode, run_image.width), ("1", width))
                self.assertEqual(test_image.width, width)
                # The print is the same picture as the preview.
                preview = self.service.outputs.image("fake", width)
                self.assertEqual(run_image.tobytes(), preview.tobytes())
        job = self.service.snapshot()["printer"]["last_job"]
        self.assertEqual((job["module"], job["width_px"]), ("test", 384))

    def test_device_path_is_restricted(self):
        for device in ("/etc/passwd", "/dev/../etc/passwd"):
            with self.subTest(device=device):
                with self.assertRaises(PrinterError):
                    send_to_device(IMAGE, {"device": device})
        if not os.path.isdir("/dev/shm"):
            self.skipTest("/dev/shm unavailable")
        fd, path = tempfile.mkstemp(dir="/dev/shm")
        os.close(fd)
        try:
            with self.assertRaises(PrinterError) as caught:
                send_to_device(IMAGE, {"device": path})
            self.assertIn("not a character device", str(caught.exception))
        finally:
            os.unlink(path)

    def test_writer_deadline(self):
        writer = PrinterWriter()
        try:
            with patch(
                "gpta.printing.transport.send_to_device",
                side_effect=lambda *a: time.sleep(0.5),
            ):
                started = time.monotonic()
                with self.assertRaises(PrinterError):
                    writer.print(["hello"], {"device": "/dev/null"}, timeout=0.05)
                self.assertLess(time.monotonic() - started, 0.4)
        finally:
            writer.stop()

    def test_timed_out_queued_job_never_prints(self):
        writer = PrinterWriter()
        release = threading.Event()
        printed = []

        def blocking_send(lines, config):
            printed.append(lines[0])
            if lines[0] == "first":
                release.wait(2)

        try:
            with patch(
                "gpta.printing.transport.send_to_device", side_effect=blocking_send
            ):
                first = threading.Thread(
                    target=lambda: writer.print(["first"], {}, timeout=2)
                )
                first.start()
                while not printed:
                    time.sleep(0.01)
                with self.assertRaises(PrinterError) as caught:
                    writer.print(["second"], {}, timeout=0.05)
                self.assertIn("cancelled", str(caught.exception))
                release.set()
                first.join(2)
                writer.print(["third"], {}, timeout=2)
        finally:
            writer.stop()
        self.assertEqual(printed, ["first", "third"])


if __name__ == "__main__":
    unittest.main()
