import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gpta.config import ConfigStore
from gpta.errors import InvalidRequest, NotFound
from gpta.logbus import LogBus
from gpta.module_api import Block, Module
from gpta.printing import (
    PrinterError,
    PrinterRateLimited,
    PrinterWriter,
    PrintStopped,
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
            "max_prints_per_hour": 100,
            "failure_threshold": 2,
        }
        config.update(overrides)
        self.store.update_printer(config)

    def idle(self):
        self.assertTrue(self.service.printer.wait_idle(5))

    def queue(self):
        return self.service.queue()

    def big_job(self, source="instant", **kwargs):
        """A job longer than a 100 mm warning limit."""
        image = Image.new("1", (576, 2000), 1)
        return self.service.printer.submit(image, source, manual=True, **kwargs)

    def small_job(self, source="instant", **kwargs):
        kwargs.setdefault("manual", True)
        return self.service.printer.submit(IMAGE, source, **kwargs)

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
            self.idle()
        self.assertEqual(max_active, 1)
        self.assertEqual(calls, 2)

    def test_instant_print_needs_no_auto_print(self):
        self.enable_printer(auto_print=False)
        with patch("gpta.printing.transport.send_to_device") as send:
            self.service.test_printer()
            result = self.service.instant_print({"text": "milk\neggs", "size": "large"})
            self.idle()
        self.assertEqual(result["job"]["source"], "instant")
        self.assertEqual(send.call_count, 2)
        image = send.call_args.args[0]
        self.assertEqual(image.width, 576)
        self.assertEqual(self.service.printer.status()["last_job"]["module"], "instant")
        self.assertEqual(
            [job["state"] for job in self.queue()["history"]], ["done", "done"]
        )

    def test_reader_print(self):
        from gpta.reader import Page

        self.enable_printer(auto_print=False, warn_length_mm=100_000)
        words = " ".join(f"word{i:05d}" for i in range(3000))
        html = f"<html><body><article><p>{words}</p></article></body></html>"
        self.service.reader_fetcher = lambda url: Page("https://x.example/", html)
        page = self.service.reader_fetch({"url": "https://x.example/"})
        with patch("gpta.printing.transport.send_to_device") as send:
            self.service.reader_print({"id": page["id"]})
            self.idle()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.service.printer.status()["last_job"]["module"], "reader")
        # A long print gets more time than the configured write timeout.
        image, config = send.call_args.args
        self.assertEqual(image.height, page["height_px"])
        base = self.service.config.snapshot()["printer"]["write_timeout_seconds"]
        self.assertGreater(config["write_timeout_seconds"], base + 10)

    def scheduled_run(self):
        return self.service.runner.run("fake", trigger="schedule")

    def test_automatic_prints_follow_each_other_without_a_delay(self):
        self.enable_printer()
        with patch("gpta.printing.transport.send_to_device") as send:
            first = self.scheduled_run()
            self.idle()
            second = self.scheduled_run()
            self.idle()
            self.assertTrue(first["queued"] and second["queued"])
            self.assertEqual(send.call_count, 2)
            self.assertIsNone(self.queue()["blocked"])

    def test_newer_scheduled_receipt_replaces_the_waiting_one(self):
        self.enable_printer()
        self.service.printer.pause()
        with patch("gpta.printing.transport.send_to_device") as send:
            first = self.scheduled_run()["job"]
            second = self.scheduled_run()["job"]
            manual = self.service.run_module("fake")["job"]  # Run now: kept too
            pending = [job["id"] for job in self.queue()["pending"]]
            self.assertEqual(pending, [second["id"], manual["id"]])
            replaced = self.service.printer.jobs.get(first["id"])
            self.assertEqual(replaced["state"], "cancelled")
            self.assertIn("replaced", replaced["error"])
            send.assert_not_called()

    def test_auto_print_off_is_reported(self):
        self.enable_printer(auto_print=False)
        with patch("gpta.printing.transport.send_to_device") as send:
            result = self.service.run_module("fake")
            self.idle()
        send.assert_not_called()
        self.assertFalse(result["queued"])
        self.assertEqual(result["print_skipped"], "auto print is off")

    def test_printer_disabled_reports_nothing(self):
        with patch("gpta.printing.transport.send_to_device") as send:
            result = self.service.run_module("fake")
        send.assert_not_called()
        self.assertFalse(result["queued"])
        self.assertIsNone(result["print_skipped"])
        self.assertEqual(self.queue()["pending"], [])

    def test_hourly_cap_holds_the_queue(self):
        self.enable_printer(max_prints_per_hour=1)
        with patch("gpta.printing.transport.send_to_device") as send:
            self.service.test_printer()
            self.service.test_printer()
            self.idle()
            self.assertEqual(send.call_count, 1)
            queue = self.queue()
            self.assertEqual(len(queue["pending"]), 1)
            self.assertIn("hourly limit", queue["blocked"])
            self.assertIn(
                "hourly limit", self.service.snapshot()["printer"]["queue"]["blocked"]
            )
            self.store.update_printer({"max_prints_per_hour": 5})
            self.service.printer.wake()
            self.idle()
            self.assertEqual(send.call_count, 2)

    def test_queue_is_bounded(self):
        self.enable_printer()
        self.service.printer.pause()
        with patch("gpta.printing.jobs.MAX_PENDING", 2):
            self.small_job()
            self.small_job()
            with self.assertRaises(PrinterRateLimited) as caught:
                self.small_job()
        self.assertIn("queue is full", str(caught.exception))

    def test_pause_holds_everything_and_survives_restart(self):
        self.enable_printer()
        self.service.queue_pause(True)
        with patch("gpta.printing.transport.send_to_device") as send:
            self.small_job()
            self.small_job("reader")
            self.idle()
            send.assert_not_called()
            self.assertTrue(self.queue()["paused"])
            self.assertEqual(self.queue()["blocked"], "printing is paused")
            # A restart keeps both the pause and the waiting jobs.
            self.service.stop()
            self.service = Service(
                self.store, self.registry, LogBus(), fetch_timeout=1.0
            )
            self.service.start()
            self.idle()
            send.assert_not_called()
            self.assertTrue(self.queue()["paused"])
            self.assertEqual(len(self.queue()["pending"]), 2)
            self.service.queue_pause(False)
            self.idle()
            self.assertEqual(send.call_count, 2)
        self.assertEqual(self.queue()["pending"], [])

    def test_long_job_holds_the_queue_at_the_head(self):
        self.enable_printer(warn_length_mm=100)
        with patch("gpta.printing.transport.send_to_device") as send:
            self.service.printer.pause()
            short = self.small_job()
            long = self.big_job()
            behind = self.small_job("reader")
            self.assertTrue(long["needs_approval"])
            self.assertFalse(short["needs_approval"])
            self.service.printer.resume()
            self.idle()
            # The short job ahead of it printed; the long one holds the rest.
            self.assertEqual(send.call_count, 1)
            queue = self.queue()
            self.assertEqual(
                [job["id"] for job in queue["pending"]], [long["id"], behind["id"]]
            )
            self.assertIn("approve or cancel", queue["blocked"])
            self.service.queue_action(str(long["id"]), "approve", {})
            self.idle()
            self.assertEqual(send.call_count, 3)
            self.assertEqual(send.call_args_list[1].args[0].height, 2000)

    def test_cancelling_the_held_job_releases_the_queue(self):
        self.enable_printer(warn_length_mm=100)
        with patch("gpta.printing.transport.send_to_device") as send:
            long = self.big_job()
            behind = self.small_job()
            self.idle()
            send.assert_not_called()
            self.service.queue_action(str(long["id"]), "cancel", {})
            self.idle()
            self.assertEqual(send.call_count, 1)
        states = {job["id"]: job["state"] for job in self.queue()["history"]}
        self.assertEqual(states, {long["id"]: "cancelled", behind["id"]: "done"})
        # A cancelled job's image is dropped.
        with self.assertRaises(NotFound):
            self.service.queue_png(str(long["id"]))

    def test_confirmed_long_print_is_not_held(self):
        self.enable_printer(warn_length_mm=100)
        text = "line\n" * 200
        with patch("gpta.printing.transport.send_to_device") as send:
            held = self.service.instant_print({"text": text})["job"]
            self.assertTrue(held["needs_approval"])
            self.idle()
            send.assert_not_called()
            self.service.queue_action(str(held["id"]), "cancel", {})
            ok = self.service.instant_print({"text": text, "confirmed": True})["job"]
            self.assertFalse(ok["needs_approval"])
            self.idle()
            self.assertEqual(send.call_count, 1)

    def test_reorder_and_clear(self):
        self.enable_printer()
        self.service.printer.pause()
        a, b, c = (self.small_job(name)["id"] for name in "abc")

        def order():
            return [job["id"] for job in self.queue()["pending"]]

        self.service.queue_action(str(c), "move", {"to": "top"})
        self.assertEqual(order(), [c, a, b])
        self.service.queue_action(str(c), "move", {"to": "down"})
        self.assertEqual(order(), [a, c, b])
        self.service.queue_action(str(a), "move", {"to": "bottom"})
        self.assertEqual(order(), [c, b, a])
        self.service.queue_action(str(a), "move", {"to": "up"})
        self.assertEqual(order(), [c, a, b])
        with self.assertRaises(InvalidRequest):
            self.service.queue_action(str(a), "move", {"to": "sideways"})
        with self.assertRaises(NotFound):
            self.service.queue_action("9999", "cancel", {})
        with patch("gpta.printing.transport.send_to_device") as send:
            self.service.queue_clear()
            self.service.printer.resume()
            self.idle()
            send.assert_not_called()
        self.assertEqual(order(), [])

    def test_old_jobs_expire(self):
        self.enable_printer(job_expiry_hours=1)
        self.service.printer.pause()
        job = self.small_job()
        printer = self.service.printer
        clock = printer._clock
        printer._clock = lambda: clock() + 2 * 3600
        with patch("gpta.printing.transport.send_to_device") as send:
            with self.assertLogs("gpta.printing", "WARNING"):
                printer.wake()
                self.idle()
            printer.resume()
            self.idle()
            send.assert_not_called()
        self.assertEqual(printer.jobs.get(job["id"])["state"], "expired")

    def test_stop_the_printing_job(self):
        started = threading.Event()

        def slow_send(image, config):
            started.set()
            if config["stop"].wait(5):
                raise PrintStopped("stopped")

        self.enable_printer()
        with patch("gpta.printing.transport.send_to_device", side_effect=slow_send):
            job = self.small_job()
            self.assertTrue(started.wait(5))
            self.assertEqual(self.queue()["current"]["id"], job["id"])
            self.service.queue_action(str(job["id"]), "cancel", {})
            self.idle()
        ended = self.service.printer.jobs.get(job["id"])
        self.assertEqual(
            (ended["state"], ended["error"]), ("cancelled", "stopped while printing")
        )
        # A stop is not a device failure.
        self.assertIsNone(self.service.printer.status()["last_error"])

    def test_interrupted_job_is_not_reprinted_after_restart(self):
        self.enable_printer()
        self.service.printer.pause()
        job = self.small_job()
        jobs = self.service.printer.jobs
        self.assertTrue(jobs.start(job["id"], time.time()))  # as if mid-print
        self.service.stop()
        with self.assertLogs("gpta.printing", "WARNING"):
            self.service = Service(
                self.store, self.registry, LogBus(), fetch_timeout=1.0
            )
        with patch("gpta.printing.transport.send_to_device") as send:
            self.service.queue_pause(False)
            self.idle()
            send.assert_not_called()
            failed = self.queue()["history"][0]
            self.assertEqual((failed["state"], failed["can_retry"]), ("failed", True))
            self.assertIn("restart", failed["error"])
            self.service.queue_action(str(job["id"]), "retry", {})
            self.idle()
            self.assertEqual(send.call_count, 1)

    def test_consecutive_failures_trip_circuit_breaker(self):
        self.enable_printer(failure_threshold=2)
        with patch(
            "gpta.printing.transport.send_to_device",
            side_effect=PrinterError("offline"),
        ) as send:
            self.service.run_module("fake")
            self.idle()
            self.assertTrue(self.store.snapshot()["printer"]["enabled"])
            self.small_job("a")
            self.small_job("b")
            self.idle()
            # The second failure disables the printer; the third job waits.
            self.assertEqual(send.call_count, 2)
        printer = self.service.snapshot()["printer"]
        self.assertFalse(printer["enabled"])
        self.assertTrue(printer["circuit_open"])
        self.assertIn("auto-disabled", printer["disabled_reason"])
        self.assertEqual(printer["last_error"], "offline")
        self.assertEqual(printer["queue"]["pending"], 1)
        self.assertIn("auto-disabled", printer["queue"]["blocked"])
        module = self.service.snapshot()["modules"][0]
        self.assertEqual(module["runtime"]["print_error"], "offline")
        # Re-enabling releases the waiting job.
        with patch("gpta.printing.transport.send_to_device") as send:
            self.service.set_printer({"enabled": True})
            self.idle()
            self.assertEqual(send.call_count, 1)

    def test_prints_the_rendered_image_at_paper_width(self):
        self.enable_printer()
        for paper, width in (("80mm", 576), ("58mm", 384)):
            self.store.update_printer({"paper": paper})
            with self.subTest(paper=paper):
                with patch("gpta.printing.transport.send_to_device") as send:
                    self.service.run_module("fake")
                    self.service.test_printer()
                    self.idle()
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
