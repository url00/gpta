import subprocess
import unittest
from unittest.mock import patch

from PIL import Image

from gpta.printing import PrinterError, send_to_device, transport

IMAGE = Image.new("1", (16, 3), 1)


class FakeCups:
    """Stands in for lp / lpstat / cancel."""

    def __init__(self, polls_pending=1, disabled_after=False, disabled=False):
        self.polls_pending = polls_pending
        self.disabled_after = disabled_after
        self.disabled = disabled
        self.calls = []
        self.printed = None
        self.status_checks = 0

    def __call__(self, args, input=None, **kwargs):
        self.calls.append(args)
        out = ""
        if args[0] == "lp":
            self.printed = input
            out = "request id is POS-80-9 (1 file(s))\n"
        elif args[:2] == ["lpstat", "-p"]:
            self.status_checks += 1
            off = self.disabled or (self.disabled_after and self.status_checks > 1)
            out = (
                "printer POS-80 disabled since today -\n\tunplugged\n"
                if off
                else "printer POS-80 is idle.  enabled since today\n"
            )
        elif args[:2] == ["lpstat", "-o"]:
            if self.polls_pending > 0:
                self.polls_pending -= 1
                out = "POS-80-9  nathanb  1024  today\n"
        return subprocess.CompletedProcess(args, 0, out.encode(), b"")


CONFIG = {"cups_queue": "POS-80", "write_timeout_seconds": 30, "cut": True}


class CupsTransportTests(unittest.TestCase):
    def run_with(self, fake, config=CONFIG):
        with (
            patch("gpta.printing.transport.subprocess.run", side_effect=fake),
            patch("gpta.printing.transport.time.sleep"),
        ):
            send_to_device(IMAGE, config)

    def test_sends_raw_escpos_and_waits_for_job(self):
        fake = FakeCups(polls_pending=2)
        self.run_with(fake)
        lp = next(call for call in fake.calls if call[0] == "lp")
        self.assertEqual(lp, ["lp", "-d", "POS-80", "-o", "raw", "-t", "gpta", "-"])
        # ESC @, then one GS v 0 band: 2 bytes per row, 3 rows, all white.
        self.assertEqual(
            fake.printed,
            b"\x1b@\x1dv0\x00\x02\x00\x03\x00" + bytes(6) + b"\x1dV\x41\x00",
        )
        polls = [call for call in fake.calls if call[:2] == ["lpstat", "-o"]]
        self.assertEqual(len(polls), 3)

    def test_stopped_queue_is_an_error_before_printing(self):
        fake = FakeCups(disabled=True)
        with self.assertRaises(PrinterError) as caught:
            self.run_with(fake)
        self.assertIn("stopped", str(caught.exception))
        self.assertIsNone(fake.printed)

    def test_queue_stopping_during_job_is_reported(self):
        fake = FakeCups(disabled_after=True)
        with self.assertRaises(PrinterError) as caught:
            self.run_with(fake)
        self.assertIn("POS-80-9 failed", str(caught.exception))

    def test_unfinished_job_is_cancelled(self):
        fake = FakeCups(polls_pending=10**6)
        clock = iter(range(0, 10**6, 10))
        with patch("gpta.printing.transport.time.monotonic", lambda: next(clock)):
            with self.assertRaises(PrinterError) as caught:
                self.run_with(fake)
        self.assertIn("cancelled", str(caught.exception))
        self.assertIn(["cancel", "POS-80-9"], fake.calls)

    def test_missing_lp_is_a_clear_error(self):
        with patch(
            "gpta.printing.transport.subprocess.run", side_effect=FileNotFoundError
        ):
            with self.assertRaises(PrinterError) as caught:
                send_to_device(IMAGE, CONFIG)
        self.assertIn("CUPS client tools", str(caught.exception))

    def test_lp_failure_reports_stderr(self):
        def fake(args, **kwargs):
            if args[0] == "lp":
                return subprocess.CompletedProcess(
                    args, 1, b"", b"lp: The printer or class does not exist."
                )
            return FakeCups()(args, **kwargs)

        with self.assertRaises(PrinterError) as caught:
            self.run_with(fake)
        self.assertIn("does not exist", str(caught.exception))

    def test_without_queue_uses_device(self):
        with patch.object(transport, "_send_to_device_node") as device:
            send_to_device(IMAGE, {"cups_queue": "", "device": "/dev/null"})
        device.assert_called_once()


if __name__ == "__main__":
    unittest.main()
