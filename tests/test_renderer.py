import unittest
from datetime import UTC, datetime

from gpta.module_api import Block
from gpta.renderer import (
    ascii_safe,
    render_receipt,
    render_total,
    to_escpos,
    wrap_text,
)


class RendererTests(unittest.TestCase):
    def test_wrap_and_ascii(self):
        self.assertEqual(ascii_safe("caf\u00e9 \u2014 ok"), "cafe - ok")
        lines = wrap_text("one two three four five", 10)
        self.assertTrue(all(len(line) <= 10 for line in lines))
        self.assertEqual(" ".join(lines), "one two three four five")

    def test_receipt_width_and_truncation(self):
        now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
        lines = render_receipt(
            "Test Module",
            [Block("HEADING", ("a long line " * 20,))],
            32,
            max_chars=80,
            now=now,
        )
        self.assertIn("[truncated]", lines)
        self.assertTrue(all(len(line) <= 32 for line in lines))
        self.assertTrue(lines[0].strip() == "TEST MODULE")

    def test_total_sections(self):
        lines = render_total([("a", ["one"]), ("b", ["two"])], 10)
        self.assertEqual(lines, ["one", "", "=" * 10, "", "two"])
        self.assertEqual(render_total([], 10), ["No output from enabled modules."])

    def test_escpos_bytes(self):
        data = to_escpos(["hello"], feed_lines=2, cut=True)
        self.assertTrue(data.startswith(b"\x1b@"))
        self.assertTrue(data.endswith(b"\x1dV\x00"))
        self.assertIn(b"hello\n\n", data)


if __name__ == "__main__":
    unittest.main()
