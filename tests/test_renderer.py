import json
import unittest
from datetime import UTC, datetime

from gpta.blocks import (
    Bar,
    BarChart,
    Block,
    Checkbox,
    Heading,
    KeyValue,
    QrCode,
    Rule,
    Text,
    from_dict,
)
from gpta.blocks import to_dict as block_to_dict
from gpta.renderer import (
    TRUNCATED,
    Receipt,
    build_receipt,
    check_elements,
    clean_text,
    render_image,
    render_text,
    render_total,
    render_total_image,
    test_receipt,
    to_png,
    truncate,
    wrap_text,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

ALL_KINDS = [
    Block("Section", ("one", "", "two"), {"source": "x"}),
    Heading("Heading"),
    Text("A paragraph of body text that is long enough to wrap.", align="center"),
    Text("small print", size="small", bold=True),
    Text("mixed **bold** words", bold_spans=((6, 10), (15, 20))),
    Rule(),
    Checkbox("Water the plants", value="Oct 20", note="the big fern too"),
    Checkbox("A done task with a long title that has to wrap", checked=True),
    Rule(heavy=True),
    KeyValue("Score", "300"),
    BarChart((Bar("alpha", 3), Bar("beta", 10, "ten"), ("gamma", 0)), title="Bars"),
    QrCode("https://example.com/story"),
]


def black_dots(image):
    return image.histogram()[0]


class TextTests(unittest.TestCase):
    def test_clean_text_keeps_unicode(self):
        self.assertEqual(clean_text("café — “ok”"), "café — “ok”")
        self.assertEqual(clean_text("a\tb\x07c\nd"), "a b c\nd")
        # Characters the font lacks become the replacement glyph.
        self.assertEqual(clean_text("日本"), "��")
        self.assertEqual(clean_text(None), "")

    def test_wrap_text(self):
        lines = wrap_text("one two three four five", 10)
        self.assertTrue(all(len(line) <= 10 for line in lines))
        self.assertEqual(" ".join(lines), "one two three four five")


class BlockTests(unittest.TestCase):
    def test_json_roundtrip(self):
        for element in ALL_KINDS:
            with self.subTest(kind=element.kind):
                data = json.loads(json.dumps(block_to_dict(element)))
                self.assertEqual(from_dict(data), element)

    def test_invalid_blocks_rejected(self):
        for bad in (
            {"kind": "photo"},
            {"kind": "text"},
            {"kind": "text", "text": "x", "size": "giant"},
            {"kind": "bar_chart", "bars": [{"label": "a", "value": "x"}]},
            {"kind": "key_value", "label": 1, "value": "x"},
            {"kind": "qr", "data": ""},
            {"kind": "text", "text": "ab", "bold_spans": [[0, 3]]},
            {"kind": "text", "text": "ab", "bold_spans": [[1, 1]]},
            {"kind": "text", "text": "ab", "bold_spans": "01"},
            {"kind": "text", "text": "ab", "bold_spans": [["0", 1]]},
            {"kind": "qr", "data": "x" * 1001},
            "text",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                from_dict(bad)
        with self.assertRaises(TypeError):
            Block("title", "not a tuple")
        with self.assertRaises(ValueError):
            Bar("nan", float("nan"))

    def test_check_elements(self):
        self.assertEqual(check_elements(ALL_KINDS), ALL_KINDS)
        for bad in ("text", ["text"], [None], None):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                check_elements(bad)

    def test_receipt_roundtrip(self):
        receipt = build_receipt("Test", ALL_KINDS, now=NOW)
        data = json.loads(json.dumps(receipt.to_dict()))
        self.assertEqual(Receipt.from_dict(data), receipt)
        self.assertEqual(receipt.stamp, "2026-10-18 12:00 UTC")


class TruncateTests(unittest.TestCase):
    def test_counts_module_content_only(self):
        elements = [Heading("12345"), Rule(), Text("abcdefghij"), KeyValue("k", "v")]
        out = truncate(elements, 8)  # a rule counts as one character
        self.assertEqual(out[:3], [Heading("12345"), Rule(), Text("ab")])
        self.assertEqual(out[-1].text, TRUNCATED)
        self.assertEqual(truncate(elements, 100), elements)

    def test_rules_and_bars_are_bounded(self):
        self.assertEqual(len(truncate([Rule()] * 500, 100)), 101)
        chart = BarChart(tuple(Bar("", 1) for _ in range(500)))
        self.assertEqual(truncate([chart], 100), [Text(TRUNCATED, size="small")])

    def test_block_lines_are_cut(self):
        out = truncate([Block("TT", ("aaaa", "bbbb"))], 4)
        self.assertEqual(out[0], Block("TT", ("aa",)))
        self.assertEqual(out[1].text, TRUNCATED)


class ImageTests(unittest.TestCase):
    def test_width_and_mode_per_paper(self):
        receipt = build_receipt("Test Module", ALL_KINDS, now=NOW)
        for width in (576, 384):
            with self.subTest(width=width):
                image = render_image(receipt, width)
                self.assertEqual(image.mode, "1")
                self.assertEqual(image.width, width)
                self.assertGreater(image.height, 200)
                self.assertGreater(black_dots(image), 1000)

    def test_deterministic(self):
        receipt = build_receipt("Test", ALL_KINDS, now=NOW)
        first = render_image(receipt, 576).tobytes()
        self.assertEqual(render_image(receipt, 576).tobytes(), first)

    def test_more_content_is_taller(self):
        short = render_image(build_receipt("T", [Text("x")], now=NOW), 576)
        tall = render_image(build_receipt("T", [Text("x " * 200)], now=NOW), 576)
        self.assertGreater(tall.height, short.height)

    def test_bar_lengths_follow_values(self):
        def bar_width(value):
            chart = BarChart((Bar("", value, "x"),), max_value=10)
            image = render_image(Receipt("", (chart,)), 576)
            return black_dots(image)

        self.assertLess(bar_width(2), bar_width(5))
        self.assertLess(bar_width(5), bar_width(10))
        self.assertEqual(bar_width(10), bar_width(50))  # clamped

    def test_checkbox_box_is_filled_only_when_checked(self):
        def ink(checked):
            receipt = build_receipt("", [Checkbox("x", checked=checked)], now=NOW)
            image = render_image(receipt, 576).convert("L")
            return sum(
                1 for v in image.crop((0, 0, 40, image.height)).getdata() if v == 0
            )

        self.assertGreater(ink(True), ink(False))

    def test_print_scale(self):
        from gpta.renderer import PRINT_SIZES, print_scale, theme_for

        receipt = build_receipt("", [Text("hello world " * 20)], now=NOW)
        heights = [
            render_image(receipt, 576, print_scale(size)).height for size in PRINT_SIZES
        ]
        self.assertEqual(heights, sorted(heights))
        self.assertEqual(len(set(heights)), len(heights))
        self.assertEqual(theme_for(576, print_scale("body")), theme_for(576))
        self.assertGreaterEqual(theme_for(384, print_scale("tiny")).small, 11)

    def test_double_columns(self):
        from gpta.renderer import COLUMN_BAND_MAX_PX

        para = "Columns split long text between lines, newspaper style. " * 6
        elements = [Heading("Title"), Rule(heavy=True), Text(para), Text(para)]
        receipt = build_receipt("", elements, now=NOW)
        single = render_image(receipt, 576)
        double = render_image(receipt, 576, layout="double", lead=2)
        self.assertEqual(double.width, 576)
        self.assertLess(double.height, single.height)

        def ink(image, box):
            return sum(1 for v in image.convert("L").crop(box).getdata() if v == 0)

        # Body text sits on both sides of the centre rule, roughly balanced.
        top, bottom = 120, double.height - 60
        left = ink(double, (0, top, 280, bottom))
        right = ink(double, (296, top, 576, bottom))
        self.assertGreater(min(left, right), 0.6 * max(left, right))

        # Long text becomes several bands, none taller than the cap.
        long = build_receipt("", [Text(para)] * 40, now=NOW)
        image = render_image(long, 576, layout="double").convert("L")
        centre = [image.getpixel((287, y)) == 0 for y in range(image.height)]
        runs, length = [], 0
        for dark in centre + [False]:
            if dark:
                length += 1
            elif length:
                runs.append(length)
                length = 0
        self.assertGreater(len(runs), 1)
        self.assertLessEqual(max(runs), COLUMN_BAND_MAX_PX + 60)

    def test_triple_columns(self):
        from gpta.renderer import _split_columns

        para = "Three narrow columns pack more text onto each page. " * 6
        receipt = build_receipt("", [Text(para), Text(para), Text(para)], now=NOW)
        triple = render_image(receipt, 576, layout="triple")
        self.assertEqual(triple.width, 576)

        def ink(image, box):
            return sum(1 for v in image.convert("L").crop(box).getdata() if v == 0)

        # Text in all three columns, roughly balanced, with clear gaps
        # (no dividing rules) between them.
        third = 576 // 3
        body = (8, triple.height - 60)
        inks = [
            ink(triple, (i * third + 12, body[0], (i + 1) * third - 12, body[1]))
            for i in range(3)
        ]
        self.assertGreater(min(inks), 0.6 * max(inks))
        for x in (190, 383):
            column = [triple.convert("L").getpixel((x, y)) for y in range(*body)]
            self.assertNotIn(0, column)

        # Balanced splits; earlier columns keep the extra line.
        self.assertEqual(_split_columns([10] * 7, 3), [3, 5])
        self.assertEqual(_split_columns([10] * 2, 3), [1, 2])
        self.assertEqual(_split_columns([], 3), [0, 0])

    def test_qr_code_dots_match_matrix(self):
        import segno

        url = "https://blog.example/a-long-article?ref=receipt"
        image = render_image(Receipt("", (QrCode(url),), ""), 576).convert("L")
        rows = segno.make(url, error="m", micro=False).matrix
        # Find the drawn code: the first and last black columns and rows.
        box = image.point(lambda v: 255 - v).getbbox()
        module = (box[2] - box[0]) // len(rows)
        self.assertGreaterEqual(module, 2)
        self.assertEqual(box[2] - box[0], module * len(rows))
        self.assertLess(abs((box[0] + box[2]) / 2 - 288), module)  # centred
        for r, row in enumerate(rows):
            for c, dark in enumerate(row):
                x = box[0] + c * module + module // 2
                y = box[1] + r * module + module // 2
                self.assertEqual(image.getpixel((x, y)) == 0, bool(dark), (r, c))
        self.assertIn(
            f"[QR: {url}]", "\n".join(render_text(Receipt("", (QrCode(url),), ""), 80))
        )

    def test_bold_spans(self):
        def draw(**kwargs):
            text = Text("plain then bold then plain", **kwargs)
            return render_image(Receipt("", (text,), ""), 576)

        plain, mixed, bold = draw(), draw(bold_spans=((11, 15),)), draw(bold=True)
        self.assertEqual(plain.size, mixed.size)
        self.assertLess(black_dots(plain), black_dots(mixed))
        self.assertLess(black_dots(mixed), black_dots(bold))
        # Only the bold word changes; the text around it stays put.
        diff = [
            x
            for x in range(576)
            for y in range(plain.height)
            if plain.getpixel((x, y)) != mixed.getpixel((x, y))
        ]
        char = 576 // 40  # 24px DejaVu Sans Mono is about 14.4 px a character
        self.assertGreater(min(diff), 8 + 10 * char)
        self.assertLess(max(diff), 8 + 16 * char)
        # Plain blocks still save without the field.
        self.assertNotIn("bold_spans", block_to_dict(Text("x")))

    def test_leading_spaces_kept(self):
        lines = render_image(Receipt("", (Text("x\n    x"),), ""), 576).convert("L")
        box = [
            lines.crop((0, y0, 576, y0 + 30)).point(lambda v: 255 - v).getbbox()
            for y0 in (8, 8 + 29)
        ]
        self.assertGreater(box[1][0] - box[0][0], 50)

    def test_checkbox_text(self):
        lines = render_text(
            Receipt("", (Checkbox("Soon", value="Oct 20", note="n"),), ""), 20
        )
        self.assertEqual(lines[:2], ["[ ] Soon      Oct 20", "    n"])

    def test_total_image_stacks_with_separator(self):
        one = render_image(build_receipt("A", [Text("a")], now=NOW), 576)
        two = render_image(build_receipt("B", [Text("b")], now=NOW), 576)
        total = render_total_image([one, two])
        self.assertEqual(total.width, 576)
        self.assertGreater(total.height, one.height + two.height)
        self.assertIsNone(render_total_image([]))

    def test_test_page_and_png(self):
        image = render_image(test_receipt(NOW), 576)
        self.assertEqual(image.width, 576)
        # The ruler marks both edges of the paper.
        self.assertTrue(any(image.getpixel((0, y)) == 0 for y in range(image.height)))
        self.assertTrue(any(image.getpixel((575, y)) == 0 for y in range(image.height)))
        self.assertTrue(to_png(image).startswith(b"\x89PNG\r\n\x1a\n"))


class PlainTextTests(unittest.TestCase):
    def test_receipt_text(self):
        receipt = build_receipt(
            "Test Module",
            [Block("HEADING", ("a long line " * 20,)), KeyValue("Score", "300")],
            max_chars=80,
            now=NOW,
        )
        lines = render_text(receipt, 32)
        self.assertIn(TRUNCATED, lines)
        self.assertTrue(all(len(line) <= 32 for line in lines))
        self.assertEqual(lines[0].strip(), "TEST MODULE")
        self.assertEqual(lines[-2].strip(), "2026-10-18 12:00 UTC")

    def test_every_kind_fits_width(self):
        receipt = build_receipt("Test", ALL_KINDS, now=NOW)
        for width in (32, 48):
            lines = render_text(receipt, width)
            self.assertTrue(all(len(line) <= width for line in lines))
        text = "\n".join(render_text(receipt, 48))
        self.assertIn("Score" + " " * 40 + "300", text)
        self.assertIn("ten", text)

    def test_legacy_lines(self):
        receipt = Receipt.from_lines(["OLD RECEIPT", "-----", "hello"])
        self.assertEqual(
            render_text(receipt, 32), ["OLD RECEIPT", "-----", "hello", ""]
        )
        self.assertEqual(render_image(receipt, 384).width, 384)

    def test_total_sections(self):
        lines = render_total([["one"], ["two"]], 10)
        self.assertEqual(lines, ["one", "", "=" * 10, "", "two"])
        self.assertEqual(render_total([], 10), ["No output from enabled modules."])


if __name__ == "__main__":
    unittest.main()
