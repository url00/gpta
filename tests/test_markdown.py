import unittest

from gpta.blocks import Checkbox, Heading, Rule, Text
from gpta.markdown import inline, to_blocks


def bold_parts(block):
    return [block.text[a:b] for a, b in block.bold_spans]


class InlineTests(unittest.TestCase):
    def test_bold_spans(self):
        plain, spans = inline("Buy **milk** and __two eggs__ today")
        self.assertEqual(plain, "Buy milk and two eggs today")
        self.assertEqual([plain[a:b] for a, b in spans], ["milk", "two eggs"])

    def test_marks_dropped(self):
        cases = {
            "*soft* and _quiet_": "soft and quiet",
            "~~old~~ `code` here": "old code here",
            r"not \*bold\* at all": "not *bold* at all",
            "snake_case_name stays": "snake_case_name stays",
            "[docs](https://x.example/d) and [anchor](#top)": (
                "docs (https://x.example/d) and anchor"
            ),
            "[https://x.example](https://x.example)": "https://x.example",
            "<https://x.example/a> ![a cat](cat.png)": "https://x.example/a a cat",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(inline(source), (expected, ()))

    def test_bold_inside_link_and_italic(self):
        plain, spans = inline("see [**this**](https://x.example) *and **that***")
        self.assertEqual(plain, "see this (https://x.example) and that")
        self.assertEqual([plain[a:b] for a, b in spans], ["this", "that"])


class BlockTests(unittest.TestCase):
    def test_document(self):
        blocks = to_blocks(
            "# Shopping\n"
            "\n"
            "Get these **today**,\nplease.  \nThanks!\n"
            "\n"
            "## Lists\n"
            "- apples\n"
            "  - green ones\n"
            "1. first\n"
            "2) second\n"
            "- [ ] call mum\n"
            "- [x] post letter\n"
            "\n"
            "### Small heading\n"
            "> quoted **words**\n"
            "> go on\n"
            "\n"
            "---\n"
            "```python\n"
            "def f():\n"
            "    return 1\n"
            "```\n"
            "| Item | Qty |\n"
            "|------|----:|\n"
            "| milk | **2** |\n"
            "Title\n"
            "=====\n"
        )
        self.assertEqual(blocks[0], Heading("Shopping"))
        self.assertEqual(blocks[1].text, "Get these today, please.\nThanks!")
        self.assertEqual(bold_parts(blocks[1]), ["today"])
        self.assertEqual(blocks[2], Heading("Lists"))
        self.assertEqual(
            [b.text for b in blocks[3:7]],
            ["• apples", "  • green ones", "1. first", "2) second"],
        )
        self.assertEqual(
            blocks[7:9], [Checkbox("call mum"), Checkbox("post letter", True)]
        )
        self.assertEqual(blocks[9], Text("Small heading", bold=True))
        self.assertEqual(blocks[10].text, "“quoted words go on”")
        self.assertEqual(bold_parts(blocks[10]), ["words"])
        self.assertEqual(blocks[11], Rule())
        self.assertEqual(blocks[12], Text("def f():\n    return 1", size="small"))
        self.assertEqual(blocks[13], Text("Item | Qty", size="small", bold=True))
        self.assertEqual(blocks[14].text, "milk | 2")
        self.assertEqual(bold_parts(blocks[14]), ["2"])
        self.assertEqual(blocks[15], Heading("Title"))
        self.assertEqual(len(blocks), 16)

    def test_plain_lines_stay_paragraphs(self):
        self.assertEqual(to_blocks("just a note\n\n\n"), [Text("just a note")])
        self.assertEqual(to_blocks("  \n\n"), [])
        # A number that starts a wrapped line does not start a list.
        self.assertEqual(to_blocks("we need\n2020 more"), [Text("we need 2020 more")])


if __name__ == "__main__":
    unittest.main()
