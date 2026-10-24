import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from gpta import DEFAULT_MODULES_DIR
from gpta.module_api import Cache, FetchError, ModuleContext
from gpta.registry import discover
from modules.google_tasks.module import LISTS_URL, TASKS_URL, TOKEN_URL
from modules.hackernews.module import ITEM as HN_ITEM
from modules.hackernews.module import TOP_STORIES

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


class FakeHttp:
    def __init__(self, json_by_url=None, text_by_url=None, form_response=None):
        self.json_by_url = json_by_url or {}
        self.text_by_url = text_by_url or {}
        self.form_response = form_response or {}

    def get_json(self, url, headers=None):
        if url not in self.json_by_url:
            raise FetchError(f"missing fake response for {url}")
        return self.json_by_url[url]

    def get_text(self, url, max_bytes=2_000_000, headers=None):
        if url not in self.text_by_url:
            raise FetchError(f"missing fake text for {url}")
        return self.text_by_url[url]

    def post_form_json(self, url, data, headers=None):
        if isinstance(self.form_response, Exception):
            raise self.form_response
        return self.form_response


def make_ctx(module_id, http, tmp, limits=None, options=None):
    return ModuleContext(
        module_id=module_id,
        limits=limits or {"max_items": 5, "max_chars": 1000},
        options=options or {},
        data_dir=Path(tmp),
        now=NOW,
        http=http,
        cache=Cache(Path(tmp) / "cache"),
        log=lambda *args, **kwargs: None,
    )


class ModuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = discover(DEFAULT_MODULES_DIR)

    def test_affirmations(self):
        module = self.modules["affirmations"]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(
                "affirmations",
                FakeHttp(),
                tmp,
                limits={"max_items": 1, "max_chars": 500},
                options={"include_date": True},
            )
            blocks = module.fetch(ctx)
        self.assertEqual(len(blocks), 1)
        self.assertIn("2026-10-18", " ".join(blocks[0].lines))

    def test_hackernews_sort_and_window(self):
        old = int(NOW.timestamp()) - 10 * 86400
        recent = int(NOW.timestamp()) - 86400
        http = FakeHttp(
            {
                TOP_STORIES: [1, 2, 3],
                HN_ITEM.format(1): {
                    "id": 1,
                    "type": "story",
                    "title": "One",
                    "score": 100,
                    "time": recent,
                    "descendants": 3,
                    "url": "https://one.example",
                },
                HN_ITEM.format(2): {
                    "id": 2,
                    "type": "story",
                    "title": "Two",
                    "score": 200,
                    "time": recent,
                    "descendants": 4,
                },
                HN_ITEM.format(3): {
                    "id": 3,
                    "type": "story",
                    "title": "Old",
                    "score": 300,
                    "time": old,
                },
            }
        )
        module = self.modules["hackernews"]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(
                "hackernews",
                http,
                tmp,
                limits={"max_items": 2, "max_chars": 1500},
                options={"window_days": 7, "min_score": 0, "scan_limit": 3},
            )
            blocks = module.fetch(ctx)
        text = "\n".join(blocks[0].lines)
        self.assertIn("Two", text)
        self.assertIn("One", text)
        self.assertNotIn("Old", text)
        self.assertLess(text.index("Two"), text.index("One"))

    def test_gutenberg_excerpt_and_cache(self):
        opds = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>urn:gutenberg:11:2</id>
    <title>Alice</title>
    <content type="text">Lewis Carroll</content>
  </entry>
</feed>"""
        text = (
            "*** START OF THE PROJECT GUTENBERG EBOOK ALICE ***\n\n"
            "CHAPTER I\n\nCall me Ishmael. This is a sufficiently long paragraph "
            "that survives metadata filtering and becomes receipt content.\n\n"
            "*** END OF THE PROJECT GUTENBERG EBOOK ALICE ***\n"
        )
        http = FakeHttp(
            {},
            {
                "https://www.gutenberg.org/ebooks/11.opds": opds,
                "https://www.gutenberg.org/cache/epub/11/pg11.txt": text,
            },
        )
        module = self.modules["gutenberg"]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(
                "gutenberg",
                http,
                tmp,
                limits={"max_items": 1, "max_chars": 1800},
                options={"book_id": 11, "language": "en", "cache": True},
            )
            blocks = module.fetch(ctx)
            cached = Path(tmp) / "cache" / "gutenberg" / "11.txt"
            self.assertTrue(cached.is_file())
        joined = "\n".join(blocks[0].lines)
        self.assertIn("Call me Ishmael", joined)
        self.assertIn("Alice", joined)
        self.assertIn("by Lewis Carroll", joined)
        self.assertNotIn("START OF THE PROJECT", joined)

    def test_google_tasks(self):
        http = FakeHttp(
            {
                TOKEN_URL: {"access_token": "token"},
                LISTS_URL: {"items": [{"id": "list1", "title": "My Tasks"}]},
                TASKS_URL.format("list1", "false"): {
                    "items": [
                        {
                            "title": "Later",
                            "status": "needsAction",
                            "position": "0002",
                        },
                        {
                            "title": "Soon",
                            "status": "needsAction",
                            "due": "2026-10-20T00:00:00.000Z",
                            "notes": "first line\nsecond line",
                            "position": "0001",
                        },
                        {
                            "title": "Done",
                            "status": "completed",
                            "position": "0000",
                        },
                    ]
                },
            },
            form_response={"access_token": "token"},
        )
        module = self.modules["google_tasks"]
        with tempfile.TemporaryDirectory() as tmp:
            creds = Path(tmp) / "google_credentials.json"
            creds.write_text(
                json.dumps(
                    {
                        "client_id": "id",
                        "client_secret": "secret",
                        "refresh_token": "refresh",
                    }
                )
            )
            ctx = make_ctx(
                "google_tasks",
                http,
                tmp,
                limits={"max_items": 15, "max_chars": 2000},
                options={
                    "credentials_file": "",
                    "task_list_id": "@default",
                    "show_completed": False,
                },
            )
            blocks = module.fetch(ctx)
        text = "\n".join(blocks[0].lines)
        self.assertIn("TASKS - My Tasks", blocks[0].title)
        self.assertIn("Soon", text)
        self.assertIn("due 2026-10-20", text)
        self.assertIn("first line", text)
        self.assertNotIn("Done", text)
        self.assertLess(text.index("Soon"), text.index("Later"))

    def test_google_tasks_missing_credentials(self):
        module = self.modules["google_tasks"]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx(
                "google_tasks",
                FakeHttp(),
                tmp,
                options={
                    "credentials_file": "",
                    "task_list_id": "@default",
                    "show_completed": False,
                },
            )
            with self.assertRaises(FetchError):
                module.fetch(ctx)


if __name__ == "__main__":
    unittest.main()
