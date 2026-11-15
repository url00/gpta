import json
import sys
import tempfile
import unittest
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from gpta import DEFAULT_MODULES_DIR
from gpta.blocks import text_of
from gpta.http import FetchError as HttpFetchError
from gpta.module_api import (
    BarChart,
    Cache,
    Checkbox,
    Document,
    FetchError,
    Heading,
    KeyValue,
    ModuleContext,
    QrCode,
    QrItem,
    Reading,
    Rule,
    Skip,
    Text,
    fingerprint,
)
from gpta.registry import discover
from gpta.renderer import build_receipt, render_image, render_text

# Use the module objects the registry loaded, rather than importing
# modules.<id>.module a second time under another name.
MODULES = discover(DEFAULT_MODULES_DIR)


def source(module_id):
    return sys.modules[type(MODULES[module_id]).__module__]


HN = source("hackernews")
GT = source("google_tasks")
GC = source("google_calendar")
WX = source("weather")

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
        cls.modules = MODULES

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
        self.assertIsInstance(blocks[0], Heading)
        self.assertIn("2026-10-18", " ".join(map(text_of, blocks)))
        self.assertGreater(render_image(build_receipt("A", blocks), 576).height, 100)

    HN_RECENT = int(NOW.timestamp()) - 86400
    HN_STORIES = {
        1: {
            "id": 1,
            "type": "story",
            "title": "One",
            "score": 100,
            "time": HN_RECENT,
            "descendants": 3,
            "url": "https://www.one.example/post",
        },
        2: {
            "id": 2,
            "type": "story",
            "title": "Ask HN: Two?",
            "score": 200,
            "time": HN_RECENT,
            "descendants": 4,
            "text": "<p>Own text of the post.</p>",
        },
        3: {
            "id": 3,
            "type": "story",
            "title": "Old",
            "score": 300,
            "time": int(NOW.timestamp()) - 10 * 86400,
        },
        4: {
            "id": 4,
            "type": "story",
            "title": "A PDF",
            "score": 50,
            "time": HN_RECENT,
            "url": "https://pdf.example/paper.pdf",
        },
    }

    HN_LONG = (Heading("One"), Rule(heavy=True), *[Text("word " * 60)] * 40)

    class HnReader:
        """Fake ``ctx.reader``: pages by URL; anything else cannot be read."""

        def __init__(self, pages):
            self.pages = pages
            self.read_urls = []

        def read(self, url):
            self.read_urls.append(url)
            if url not in self.pages:
                raise FetchError("not a web page (application/pdf)")
            return self.pages[url]

        def read_html(self, html, url, title=""):
            blocks = (Heading(title), Rule(heavy=True), Text(html))
            return Reading(blocks, words=len(html.split()))

    def hn_ctx(self, tmp, stories=None, pages=None, max_items=3, **options):
        stories = self.HN_STORIES if stories is None else stories
        http = FakeHttp(
            {
                HN.TOP_STORIES: list(stories),
                **{HN.ITEM.format(n): story for n, story in stories.items()},
            }
        )
        ctx = make_ctx(
            "hackernews",
            http,
            tmp,
            limits={"max_items": max_items, "max_chars": 3000},
            options={
                "window_days": 7,
                "min_score": 0,
                "scan_limit": 100,
                "text_only": False,
                "min_words": 400,
                "comments": False,
                **options,
            },
        )
        if pages is None:
            pages = {"https://www.one.example/post": Reading(self.HN_LONG, 2400)}
        return replace(ctx, reader=self.HnReader(pages))

    def test_hackernews_list_has_codes_and_paper_estimates(self):
        module = self.modules["hackernews"]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.hn_ctx(tmp)
            blocks = module.fetch(ctx)
            items = [block for block in blocks if isinstance(block, QrItem)]
            # Highest score first; the story outside the window is left out.
            self.assertEqual(
                [item.title for item in items], ["Ask HN: Two?", "One", "A PDF"]
            )
            self.assertEqual([item.label for item in items], ["1.", "2.", "3."])
            two, one, pdf = (item.note for item in items)
            self.assertIn("200 points · 4 comments · news.ycombinator.com", two)
            self.assertIn("about 5 cm", two)
            self.assertIn("one.example", one)
            self.assertRegex(one, r"about \d\.\d m$")
            self.assertTrue(pdf.endswith("length unknown"))
            for item in items:
                self.assertTrue(item.data.startswith("http://gpta.test/s/"))
            chart = blocks[-1]
            self.assertIsInstance(chart, BarChart)
            self.assertEqual([bar.value for bar in chart.bars], [200, 100, 50])
            self.assertEqual(render_image(build_receipt("HN", blocks), 384).width, 384)

            ctx = self.hn_ctx(tmp, max_items=1, paper_estimates=False)
            plain = module.fetch(ctx)
            notes = [block.note for block in plain if isinstance(block, QrItem)]
            self.assertEqual(notes, ["200 points · 4 comments · news.ycombinator.com"])
            self.assertEqual(ctx.reader.read_urls, [])  # nothing downloaded

    def test_hackernews_text_only_lists_what_reads_well_in_print(self):
        prose = (Heading("T"), Rule(heavy=True), Text("word " * 50))
        kinds = {
            "prose": Reading(prose, words=1200, media=1),
            "long with pictures": Reading(prose, words=5000, media=3),
            "short": Reading(prose, words=150),
            "pictures": Reading(prose, words=1200, media=2),
            "code": Reading(prose, words=1200, code_words=400),
            "tables": Reading(prose, words=1200, table_rows=30),
            "talk [video]": Reading(prose, words=1200),
        }
        recent = int(NOW.timestamp()) - 3600
        stories, pages = {}, {}
        for number, (title, reading) in enumerate(kinds.items(), start=1):
            url = f"https://site.example/{number}"
            pages[url] = reading
            stories[number] = {
                "id": number,
                "type": "story",
                "title": title,
                "score": 100 - number,
                "time": recent,
                "url": url,
            }
        stories[8] = {**stories[1], "id": 8, "title": "gone", "url": "https://x/pdf"}
        module = self.modules["hackernews"]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.hn_ctx(tmp, stories, pages, max_items=10, text_only=True)
            blocks = module.fetch(ctx)
            titles = [block.title for block in blocks if isinstance(block, QrItem)]
            self.assertEqual(titles, ["prose", "long with pictures"])

            # A lower bar for length lets the short one in.
            ctx = self.hn_ctx(
                tmp, stories, pages, max_items=10, text_only=True, min_words=100
            )
            titles = [b.title for b in module.fetch(ctx) if isinstance(b, QrItem)]
            self.assertEqual(titles, ["prose", "long with pictures", "short"])

            # Reading stops once enough stories qualify.
            many = {n: {**stories[1], "id": n, "score": 500 - n} for n in range(1, 41)}
            ctx = self.hn_ctx(tmp, many, pages, max_items=3, text_only=True)
            self.assertEqual(
                len([b for b in module.fetch(ctx) if isinstance(b, QrItem)]), 3
            )
            self.assertEqual(len(ctx.reader.read_urls), HN.READ_WORKERS)

    def test_hackernews_select_prints_the_article(self):
        module = self.modules["hackernews"]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.hn_ctx(tmp)
            linked = module.on_select(ctx, {"id": 1})
            self.assertEqual(len(linked), 42)
            own = module.on_select(ctx, {"id": 2})
            self.assertEqual(text_of(own[0]), "Ask HN: Two?")
            self.assertIn("Own text", text_of(own[-1]))
            for payload, problem in (
                ({"id": 3}, "no article"),
                ({"id": 4}, "not a web page"),
                ({"id": 99}, "missing fake"),
                (None, "does not name a story"),
            ):
                with self.assertRaisesRegex(FetchError, problem):
                    module.on_select(ctx, payload)

    def hn_comment(self, number, by, text, kids=(), **extra):
        return {
            "id": number,
            "type": "comment",
            "by": by,
            "text": text,
            "time": int(NOW.timestamp()) - 3 * 3600,
            "kids": list(kids),
            **extra,
        }

    def hn_thread(self):
        """Story 1 with a small tree: 10/11/12 on top (11 is dead), replies
        20-23 under 10, and 30 under 20, which has a reply of its own."""
        c = self.hn_comment
        long = "word " * 200
        return {
            **self.HN_STORIES,
            1: {**self.HN_STORIES[1], "kids": [10, 11, 12, 13], "descendants": 9},
            10: c(
                10, "ann", "Top <i>level</i>.<p>Second &amp; last.", [20, 21, 22, 23]
            ),
            11: c(11, "ghost", "gone", dead=True),
            12: c(12, "cat", long),
            13: c(13, "dan", "fourth thread"),
            20: c(20, "bob", "First reply", [30]),
            21: c(21, "eve", long + '<p><a href="https://x.example/long">x.ex...</a>'),
            22: c(22, "fay", "<pre><code>a = 1\n  b = 2</code></pre>"),
            23: c(23, "gus", "not shown"),
            30: c(30, "hal", "Deep reply", [40, 41]),
        }

    def test_hackernews_comment_text(self):
        self.assertEqual(
            HN.paragraphs(
                "A &gt; B <i>it</i>"
                '<p><a href="https://x.example/a?b=1&amp;c=2" rel="nofollow">x...</a>'
                "<p><pre><code>x\n  y</code></pre>"
            ),
            [
                ("A > B it", False),
                ("https://x.example/a?b=1&c=2", False),
                ("x\n  y", True),
            ],
        )
        self.assertEqual(HN.paragraphs(None), [])
        parts = [("one two three", False), ("four five", False)]
        self.assertEqual(HN.shorten(parts, 0), (parts, False))
        self.assertEqual(HN.shorten(parts, 100), (parts, False))
        self.assertEqual(HN.shorten(parts, 9), ([("one two…", False)], True))
        self.assertEqual(
            HN.shorten(parts, 18), ([("one two three", False), ("four…", False)], True)
        )
        self.assertEqual(
            [HN.age(100000, 100000 - s) for s in (59, 3600, 47 * 3600, 49 * 3600)],
            ["0m", "1h", "47h", "2d"],
        )

    def test_hackernews_comments_print_as_a_second_job(self):
        module = self.modules["hackernews"]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.hn_ctx(
                tmp,
                self.hn_thread(),
                comments=True,
                comment_threads=2,
                comment_replies=3,
                reply_chars=600,
            )
            parts = module.on_select(ctx, {"id": 1})
            self.assertEqual([type(part) for part in parts], [Document, Document])
            self.assertEqual(len(parts[0].elements), 42)  # the article
            blocks = parts[1].elements
            self.assertEqual(text_of(blocks[0]), "One")
            self.assertIn("first 2 threads of 9 comments", text_of(blocks[1]))
            self.assertEqual(blocks[2], QrCode(HN.DISCUSSION.format(1)))
            body = blocks[4:]
            shown = [
                (b.indent, text_of(b).split("\n")[0][:14])
                for b in body
                if not isinstance(b, Rule)
            ]
            self.assertEqual(
                shown,
                [
                    (0, "ann · 3h"),
                    (0, "Second & last."),
                    (1, "bob · 3h"),
                    (2, "hal · 3h"),
                    (3, "+ 2 more repli"),
                    (1, "eve · 3h"),
                    (1, HN.CUT_NOTE[:14]),
                    (1, "fay · 3h"),
                    (1, "a = 1"),
                    (1, "+ 1 more reply"),
                    (0, "cat · 3h"),  # the dead comment's place is taken
                ],
            )
            ann = body[0]
            self.assertEqual(ann.text, "ann · 3h\nTop level.")
            self.assertEqual(ann.bold_spans, ((0, 8),))
            # A long reply is cut, with a code that links to it (not a print).
            eve = next(b for b in body if text_of(b).startswith("eve"))
            self.assertTrue(eve.text.endswith("…"))
            self.assertLess(len(eve.text), 620)
            cut = next(b for b in body if isinstance(b, QrItem))
            self.assertEqual(cut.data, HN.DISCUSSION.format(21))
            # A long top-level comment prints whole.
            cat = next(b for b in body if text_of(b).startswith("cat"))
            self.assertGreater(len(cat.text), 900)
            # Threads are separated by a rule, and it all renders.
            self.assertEqual(sum(isinstance(b, Rule) for b in body), 1)
            lines = render_text(build_receipt("HN", list(blocks)), 48)
            self.assertIn("| | hal · 3h", lines)
            for width in (576, 384):
                render_image(build_receipt("HN", list(blocks)), width)

            # The list says what both jobs would use.
            notes = [b.note for b in module.fetch(ctx) if isinstance(b, QrItem)]
            self.assertRegex(
                notes[1], r"article about \d\.\d m · comments about \d+ cm$"
            )
            self.assertTrue(notes[0].endswith("article about 5 cm · no comments"))
            self.assertTrue(notes[2].endswith("article length unknown · no comments"))

            # No comments, or comments off: just the article, as one job.
            self.assertIsInstance(module.on_select(ctx, {"id": 2}), list)
            off = self.hn_ctx(tmp, self.hn_thread())
            self.assertEqual(len(module.on_select(off, {"id": 1})), 42)

    def test_hackernews_paper_length_wording(self):
        self.assertEqual(HN.paper_length(12), "about 5 cm")
        self.assertEqual(HN.paper_length(430), "about 45 cm")
        self.assertEqual(HN.paper_length(995), "about 1.0 m")
        self.assertEqual(HN.paper_length(1234), "about 1.2 m")

    GT_OPTIONS = {
        "morning_time": "08:00",
        "timezone": "UTC",
        "task_list": "",
        "show_completed": True,
        "credentials_file": "",
    }
    GT_PENDING = [
        {"title": "Later", "status": "needsAction", "position": "0002"},
        {
            "title": "Soon",
            "status": "needsAction",
            "due": "2026-10-20T00:00:00.000Z",
            "notes": "first line\nsecond line",
            "position": "0001",
        },
        {"title": "  ", "status": "needsAction", "position": "0003"},
    ]

    def gt_http(self, pending, done=(), list_id="@default", lists=None):
        midnight = NOW.replace(hour=0, minute=0)
        return FakeHttp(
            {
                GT.LISTS_URL: {"items": lists or []},
                GT.pending_url(list_id): {"items": list(pending)},
                GT.completed_url(list_id, midnight): {"items": list(done)},
            },
            form_response={"access_token": "token"},
        )

    def gt_ctx(self, tmp, http, *, now=NOW, store=None, trigger="schedule", **options):
        (Path(tmp) / "google_credentials.json").write_text(
            json.dumps(
                {"client_id": "id", "client_secret": "secret", "refresh_token": "r"}
            )
        )
        ctx = make_ctx(
            "google_tasks",
            http,
            tmp,
            limits={"max_items": 15, "max_chars": 2000},
            options={**self.GT_OPTIONS, **options},
        )
        changes = {"now": now, "trigger": trigger}
        if store is not None:
            changes["store"] = store
        return replace(ctx, **changes)

    def test_google_tasks(self):
        module = self.modules["google_tasks"]
        done = [{"title": "Done", "status": "completed", "completed": "2026-10-18T09"}]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.gt_ctx(tmp, self.gt_http(self.GT_PENDING, done))
            blocks = module.fetch(ctx)
        self.assertEqual(
            blocks,
            [
                Heading("Tasks"),
                Text("2 open, 1 done today", size="small"),
                Checkbox("Soon", value="Oct 20", note="first line"),
                Checkbox("Later"),
                Checkbox("Done", checked=True),
            ],
        )
        self.assertEqual(ctx.store.get("printed_day"), "2026-10-18")
        self.assertEqual(render_image(build_receipt("GT", blocks), 576).width, 576)

    def test_google_tasks_prints_only_changes_after_the_morning_list(self):
        module = self.modules["google_tasks"]
        http = self.gt_http(self.GT_PENDING)
        with tempfile.TemporaryDirectory() as tmp:
            first = self.gt_ctx(tmp, http)
            store = first.store

            def at(hour, minute=0, day=18, **kwargs):
                now = NOW.replace(day=day, hour=hour, minute=minute)
                return self.gt_ctx(tmp, http, now=now, store=store, **kwargs)

            module.fetch(first)  # the first run of the day prints
            with self.assertRaisesRegex(Skip, "no task changes"):
                module.fetch(at(12, 5))
            # A manual run prints even when nothing changed.
            self.assertTrue(module.fetch(at(12, 6, trigger="manual")))

            # Ticking a task off moves it to the completed query: print again.
            midnight = NOW.replace(hour=0, minute=0)
            http.json_by_url[GT.pending_url("@default")] = {
                "items": self.GT_PENDING[1:]
            }
            http.json_by_url[GT.completed_url("@default", midnight)] = {
                "items": [{**self.GT_PENDING[0], "status": "completed"}]
            }
            changed = module.fetch(at(12, 10))
            self.assertIn(Checkbox("Later", checked=True), changed)
            with self.assertRaises(Skip):
                module.fetch(at(12, 15))

            # Next day: nothing (not even a fetch) before 08:00, then the
            # morning list although the tasks are the same as last night.
            next_midnight = midnight.replace(day=19)
            http.json_by_url[GT.completed_url("@default", next_midnight)] = {}
            http.form_response = FetchError("must not be called")
            with self.assertRaisesRegex(Skip, "waiting for 08:00"):
                module.fetch(at(7, 55, day=19))
            http.form_response = {"access_token": "token"}
            # An early manual run does not stand in for the morning list.
            module.fetch(at(7, 56, day=19, trigger="manual"))
            self.assertEqual(
                module.fetch(at(8, 0, day=19))[1], Text("1 open", size="small")
            )
            with self.assertRaises(Skip):
                module.fetch(at(8, 5, day=19))

    def test_fingerprint_ignores_fields_left_at_their_defaults(self):
        @dataclass(frozen=True)
        class Newer:  # Text after a release adds an optional field
            kind: ClassVar[str] = "text"
            text: str
            size: str = "body"
            added_later: int = 0

        blocks = [Heading("Tasks"), Checkbox("Soon", value="Oct 20")]
        self.assertEqual(fingerprint(blocks), fingerprint(list(blocks)))
        self.assertNotEqual(fingerprint(blocks), fingerprint(blocks[:1]))
        self.assertNotEqual(
            fingerprint([Checkbox("Soon")]), fingerprint([Checkbox("Soon", True)])
        )
        self.assertEqual(fingerprint([Newer("a")]), fingerprint([Text("a")]))
        self.assertNotEqual(
            fingerprint([Newer("a", added_later=1)]), fingerprint([Text("a")])
        )

    def test_google_tasks_does_not_reprint_after_an_upgrade(self):
        module = self.modules["google_tasks"]
        code = sys.modules[type(module).__module__]
        http = self.gt_http(self.GT_PENDING)
        with tempfile.TemporaryDirectory() as tmp:
            first = self.gt_ctx(tmp, http)
            store = first.store
            blocks = module.fetch(first)
            # 0.10.0 and earlier saved a hash that covered every block field.
            store.set("fingerprint", code._old_fingerprint(blocks))
            with self.assertRaisesRegex(Skip, "no task changes"):
                module.fetch(self.gt_ctx(tmp, http, store=store))
            self.assertEqual(store.get("fingerprint"), fingerprint(blocks))
            store.set("fingerprint", "something else")
            self.assertTrue(module.fetch(self.gt_ctx(tmp, http, store=store)))

    def test_google_tasks_named_list_and_time_zone(self):
        module = self.modules["google_tasks"]
        lists = [{"id": "L1", "title": "My Tasks"}, {"id": "L2", "title": "Chores"}]
        pending = [
            {"title": "Bins", "status": "needsAction", "due": "2026-10-18T00:00:00Z"},
            {"title": "Old", "status": "needsAction", "due": "2026-10-01T00:00:00Z"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            http = self.gt_http(pending, list_id="L2", lists=lists)
            # 12:00 UTC is 21:00 in Tokyo, still the 18th.
            midnight = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)
            http.json_by_url[GT.completed_url("L2", midnight)] = {}
            ctx = self.gt_ctx(tmp, http, task_list="chores", timezone="Asia/Tokyo")
            blocks = module.fetch(ctx)
            self.assertEqual(
                blocks[:1] + blocks[2:],
                [
                    Heading("Chores"),
                    Checkbox("Old", value="Overdue"),
                    Checkbox("Bins", value="Today"),
                ],
            )
            with self.assertRaisesRegex(FetchError, "found: My Tasks, Chores"):
                module.fetch(self.gt_ctx(tmp, http, task_list="Nope"))
            with self.assertRaisesRegex(FetchError, "HH:MM"):
                module.fetch(self.gt_ctx(tmp, http, morning_time="8am"))

    def test_google_tasks_credential_errors(self):
        module = self.modules["google_tasks"]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_ctx("google_tasks", FakeHttp(), tmp, options=self.GT_OPTIONS)
            with self.assertRaisesRegex(FetchError, "google_oauth.py"):
                module.fetch(ctx)
            rejected = HttpFetchError("HTTP 400", status=400)
            ctx = self.gt_ctx(tmp, FakeHttp(form_response=rejected))
            with self.assertRaisesRegex(FetchError, "rejected the saved sign-in"):
                module.fetch(ctx)

    GC_OPTIONS = {
        "morning_time": "08:00",
        "timezone": "America/Chicago",
        "calendar": "",
        "clock": "12-hour",
        "credentials_file": "",
    }
    # NOW is 12:00 UTC on Oct 18: 07:00 in Chicago (UTC-5), so tests use 15:00.
    GC_NOW = NOW.replace(hour=15)
    GC_DAY = (
        datetime(2026, 10, 18, 5, 0, tzinfo=UTC),
        datetime(2026, 10, 19, 5, 0, tzinfo=UTC),
    )
    GC_EVENTS = [
        {"id": "a", "summary": "Holiday", "start": {"date": "2026-10-18"}},
        {
            "id": "b",
            "summary": "Dentist",
            "location": "12 Main St\nSuite 4",
            "start": {"dateTime": "2026-10-18T09:00:00-05:00"},
            "end": {"dateTime": "2026-10-18T09:45:00-05:00"},
        },
        {
            "id": "c",
            "summary": "Call",
            "start": {"dateTime": "2026-10-18T19:30:00Z"},  # 14:30 local
            "end": {"dateTime": "2026-10-18T20:00:00Z"},
        },
        {
            "id": "d",
            "summary": "Skipped",
            "start": {"dateTime": "2026-10-18T10:00:00-05:00"},
            "end": {"dateTime": "2026-10-18T11:00:00-05:00"},
            "attendees": [{"self": True, "responseStatus": "declined"}],
        },
        {"id": "e", "eventType": "workingLocation", "start": {"date": "2026-10-18"}},
        {
            "id": "f",
            "summary": "Red-eye",
            "start": {"dateTime": "2026-10-18T23:00:00-05:00"},
            "end": {"dateTime": "2026-10-19T03:00:00-05:00"},
        },
    ]

    def gc_http(self, events, calendar_id="primary", calendars=None):
        return FakeHttp(
            {
                GC.CALENDARS_URL: {"items": calendars or []},
                GC.events_url(calendar_id, *self.GC_DAY): {"items": list(events)},
            },
            form_response={"access_token": "token"},
        )

    def gc_ctx(self, tmp, http, *, now=None, store=None, trigger="schedule", **opts):
        scope = opts.pop("scope", GC.SCOPE)
        (Path(tmp) / "google_credentials.json").write_text(
            json.dumps(
                {
                    "client_id": "id",
                    "client_secret": "secret",
                    "refresh_token": "r",
                    "scope": f"other {scope}",
                }
            )
        )
        ctx = make_ctx(
            "google_calendar",
            http,
            tmp,
            limits={"max_items": 15, "max_chars": 2000},
            options={**self.GC_OPTIONS, **opts},
        )
        changes = {"now": now or self.GC_NOW, "trigger": trigger}
        if store is not None:
            changes["store"] = store
        return replace(ctx, **changes)

    def test_google_calendar(self):
        module = self.modules["google_calendar"]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.gc_ctx(tmp, self.gc_http(self.GC_EVENTS))
            blocks = module.fetch(ctx)
            self.assertEqual(
                blocks,
                [
                    Heading("Sunday, Oct 18"),
                    Text("4 events", size="small"),
                    Text("All day", bold=True),
                    Text("Holiday"),
                    Text("9:00 AM - 9:45 AM", bold=True),
                    Text("Dentist"),
                    Text("12 Main St", size="small"),
                    Text("2:30 PM - 3:00 PM", bold=True),
                    Text("Call"),
                    Text("from 11:00 PM", bold=True),
                    Text("Red-eye"),
                ],
            )
            self.assertEqual(ctx.store.get("printed_day"), "2026-10-18")
            self.assertEqual(render_image(build_receipt("GC", blocks), 576).width, 576)
            ctx = self.gc_ctx(tmp, self.gc_http(self.GC_EVENTS[1:2]), clock="24-hour")
            self.assertEqual(module.fetch(ctx)[2], Text("09:00 - 09:45", bold=True))
            self.assertEqual(
                module.fetch(self.gc_ctx(tmp, self.gc_http([])))[1],
                Text("Nothing scheduled."),
            )

    def test_google_calendar_reprints_only_when_the_day_changes(self):
        module = self.modules["google_calendar"]
        http = self.gc_http(self.GC_EVENTS[:2])
        url = GC.events_url("primary", *self.GC_DAY)
        with tempfile.TemporaryDirectory() as tmp:
            first = self.gc_ctx(tmp, http)
            store = first.store

            def at(hour, minute=0, **kwargs):
                now = NOW.replace(hour=hour, minute=minute)
                return self.gc_ctx(tmp, http, now=now, store=store, **kwargs)

            http.form_response = FetchError("must not be called")
            with self.assertRaisesRegex(Skip, "waiting for 08:00"):
                module.fetch(at(12, 55))  # 07:55 in Chicago
            http.form_response = {"access_token": "token"}
            module.fetch(at(13, 0))  # 08:00: the morning itinerary
            with self.assertRaisesRegex(Skip, "no calendar changes"):
                module.fetch(at(13, 5))
            self.assertTrue(module.fetch(at(13, 6, trigger="manual")))

            moved = {
                **self.GC_EVENTS[1],
                "start": {"dateTime": "2026-10-18T10:00:00-05:00"},
                "end": {"dateTime": "2026-10-18T10:45:00-05:00"},
            }
            http.json_by_url[url] = {"items": [self.GC_EVENTS[0], moved]}
            self.assertIn(
                Text("10:00 AM - 10:45 AM", bold=True), module.fetch(at(13, 10))
            )
            http.json_by_url[url] = {
                "items": [*http.json_by_url[url]["items"], self.GC_EVENTS[2]]
            }
            self.assertIn(Text("Call"), module.fetch(at(13, 15)))
            with self.assertRaises(Skip):
                module.fetch(at(13, 20))

    def test_google_calendar_named_and_all_calendars(self):
        module = self.modules["google_calendar"]
        calendars = [
            {"id": "me@example.com", "summary": "Me", "selected": True},
            {
                "id": "fam",
                "summary": "x",
                "summaryOverride": "Family",
                "selected": True,
            },
            {"id": "hidden", "summary": "Hidden"},
        ]
        shared = {**self.GC_EVENTS[1], "iCalUID": "uid-1"}
        with tempfile.TemporaryDirectory() as tmp:
            http = self.gc_http([shared], "fam", calendars)
            http.json_by_url[GC.events_url("me@example.com", *self.GC_DAY)] = {
                "items": [{**shared, "id": "copy"}, self.GC_EVENTS[2]]
            }
            blocks = module.fetch(self.gc_ctx(tmp, http, calendar="*"))
            self.assertEqual(blocks[1], Text("2 events", size="small"))
            blocks = module.fetch(self.gc_ctx(tmp, http, calendar="family"))
            self.assertEqual(blocks[1], Text("1 event", size="small"))
            with self.assertRaisesRegex(FetchError, "found: Me, Family, Hidden"):
                module.fetch(self.gc_ctx(tmp, http, calendar="Work"))

    def test_google_calendar_sign_in_errors(self):
        module = self.modules["google_calendar"]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.gc_ctx(tmp, self.gc_http([]), scope="tasks.readonly")
            with self.assertRaisesRegex(FetchError, "does not include Calendar"):
                module.fetch(ctx)

            class Refusing(FakeHttp):
                def get_json(self, url, headers=None):
                    raise HttpFetchError("HTTP 403", status=403)

            ctx = self.gc_ctx(tmp, Refusing(form_response={"access_token": "t"}))
            with self.assertRaisesRegex(FetchError, "enable the Google Calendar API"):
                module.fetch(ctx)

    WX_OPTIONS = {
        "morning_time": "08:00",
        "timezone": "America/Chicago",
        "latitude": "",
        "longitude": "",
        "units": "imperial",
        "clock": "12-hour",
        "alerts": "all",
    }
    WX_PLACE = {"latitude": 44.81133, "longitude": -91.49849}
    WX_FORECAST = {
        "current": {
            "time": "2026-10-18T10:00",
            "temperature_2m": 41.4,
            "apparent_temperature": 35.2,
            "weather_code": 3,
        },
        "hourly": {
            "time": [f"2026-10-18T{hour:02d}:00" for hour in range(24)],
            "temperature_2m": [30 + hour for hour in range(24)],
            "weather_code": [61 if hour == 14 else 0 for hour in range(24)],
            "precipitation_probability": [
                60 if hour == 14 else 10 for hour in range(24)
            ],
        },
        "daily": {
            "time": ["2026-10-18"],
            "weather_code": [61],
            "temperature_2m_max": [53.6],
            "temperature_2m_min": [-0.2],
            "precipitation_probability_max": [60],
            "precipitation_sum": [0.123],
            "wind_speed_10m_max": [12.2],
            "wind_gusts_10m_max": [25.7],
            "wind_direction_10m_dominant": [310],
            "sunrise": ["2026-10-18T07:21"],
            "sunset": ["2026-10-18T18:14"],
            "uv_index_max": [3.4],
        },
    }

    @staticmethod
    def wx_alert(alert_id, event, severity="Moderate", **extra):
        properties = {
            "id": alert_id,
            "event": event,
            "severity": severity,
            "status": "Actual",
            "messageType": "Alert",
            "ends": "2026-10-19T06:00:00-05:00",
            "description": "* WHAT...Snow.\nUp to 6\ninches.\n\n* WHERE...Here.",
            "instruction": "Slow down.",
            **extra,
        }
        return {"properties": properties}

    def wx_http(self, alerts=(), units="imperial", **place):
        place = {**self.WX_PLACE, "city": "Eau Claire", "region": "Wisconsin", **place}
        spot = round(place["latitude"], 2), round(place["longitude"], 2)
        return FakeHttp(
            {
                WX.LOCATE_URL: place,
                WX.forecast_url(*spot, units, "America/Chicago"): self.WX_FORECAST,
                WX.alerts_url(*spot): {"features": list(alerts)},
            }
        )

    def wx_ctx(self, tmp, http, *, hour=15, minute=0, store=None, **opts):
        trigger = opts.pop("trigger", "schedule")
        ctx = make_ctx(
            "weather",
            http,
            tmp,
            limits={"max_chars": 3000},
            options={**self.WX_OPTIONS, **opts},
        )
        # NOW is 07:00 in Chicago (UTC-5); hour 15 is 10:00.
        changes = {"now": NOW.replace(hour=hour, minute=minute), "trigger": trigger}
        if store is not None:
            changes["store"] = store
        return replace(ctx, **changes)

    def test_weather_forecast(self):
        module = self.modules["weather"]
        alert = self.wx_alert("urn:1", "Winter Storm Warning", "Severe")
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.wx_ctx(tmp, self.wx_http([alert]))
            blocks = module.fetch(ctx)
            self.assertEqual(
                blocks,
                [
                    Heading("Sunday, Oct 18"),
                    Text("Eau Claire, Wisconsin", size="small"),
                    Text("Light rain", size="large", bold=True),
                    KeyValue("High / Low", "54° / 0°"),
                    KeyValue("Now", "41° (feels 35°)"),
                    KeyValue("Precipitation", '60%, 0.12"'),
                    KeyValue("Wind", "NW 12 mph, gusts 26"),
                    KeyValue("UV index", "3"),
                    KeyValue("Sun", "7:21 AM - 6:14 PM"),
                    Rule(),
                    KeyValue("11 AM", "41° Clear"),
                    KeyValue("2 PM", "44° Light rain 60%"),
                    KeyValue("5 PM", "47° Clear"),
                    KeyValue("8 PM", "50° Clear"),
                    KeyValue("11 PM", "53° Clear"),
                    Rule(heavy=True),
                    Text("Alerts in effect", size="small"),
                    Text("Winter Storm Warning", bold=True),
                    Text("Until 6:00 AM Mon, Oct 19", size="small"),
                ],
            )
            self.assertEqual(ctx.store.get("printed_day"), "2026-10-18")
            self.assertEqual(ctx.store.get("alerts_printed"), ["urn:1"])
            self.assertEqual(ctx.store.get("location")["lat"], 44.81)
            self.assertEqual(render_image(build_receipt("WX", blocks), 576).width, 576)

            # Metric units, a 24-hour clock and fixed coordinates.
            http = self.wx_http(units="metric", latitude=51.5, longitude=-0.12)
            del http.json_by_url[WX.LOCATE_URL]
            ctx = self.wx_ctx(
                tmp,
                http,
                units="metric",
                clock="24-hour",
                latitude="51.5",
                longitude="-0.12",
            )
            blocks = module.fetch(ctx)
            self.assertEqual(blocks[1], Text("Light rain", size="large", bold=True))
            self.assertIn(KeyValue("Precipitation", "60%, 0.1 mm"), blocks)
            self.assertIn(KeyValue("Wind", "NW 12 km/h, gusts 26"), blocks)
            self.assertIn(KeyValue("Sun", "07:21 - 18:14"), blocks)
            self.assertIn(KeyValue("14:00", "44° Light rain 60%"), blocks)

    def test_weather_prints_once_a_day_then_only_new_alerts(self):
        module = self.modules["weather"]
        http = self.wx_http()
        alerts = http.json_by_url[WX.alerts_url(44.81, -91.5)]
        forecast = WX.forecast_url(44.81, -91.5, "imperial", "America/Chicago")
        watch = self.wx_alert("urn:1", "Frost Advisory", "Minor")
        warning = self.wx_alert("urn:2", "Tornado Warning", "Extreme")
        with tempfile.TemporaryDirectory() as tmp:
            store = self.wx_ctx(tmp, http).store

            def run(hour, minute=0, **kwargs):
                ctx = self.wx_ctx(tmp, http, hour=hour, minute=minute, store=store)
                return module.fetch(replace(ctx, **kwargs))

            saved = http.json_by_url.pop(forecast)  # not fetched before 08:00
            with self.assertRaisesRegex(Skip, "forecast at 08:00"):
                run(12, 55)  # 07:55 in Chicago
            alerts["features"] = [watch]
            self.assertEqual(  # alerts print at any hour, in full
                run(12, 56),
                [
                    Heading("Frost Advisory"),
                    Text("Until 6:00 AM Mon, Oct 19", bold=True),
                    Text("* WHAT...Snow. Up to 6 inches.\n* WHERE...Here."),
                    Text("Slow down."),
                ],
            )
            http.json_by_url[forecast] = saved
            self.assertIn(Text("Frost Advisory", bold=True), run(13, 0))
            del http.json_by_url[forecast]
            del http.json_by_url[WX.LOCATE_URL]  # looked up once a day
            with self.assertRaisesRegex(Skip, "no new weather alerts$"):
                run(13, 5)

            alerts["features"] = [watch, warning]
            blocks = run(13, 10)
            self.assertEqual(blocks[0], Heading("Tornado Warning"))
            self.assertNotIn(Heading("Frost Advisory"), blocks)
            with self.assertRaises(Skip):
                run(13, 15)
            alerts["features"] = [watch]
            with self.assertRaises(Skip):
                run(13, 20)
            self.assertEqual(store.get("alerts_printed"), ["urn:1"])

            http.json_by_url[forecast] = saved
            http.json_by_url[WX.LOCATE_URL] = self.WX_PLACE
            self.assertEqual(
                run(13, 25, trigger="manual")[0], Heading("Sunday, Oct 18")
            )

    def test_weather_alert_filter_and_failures(self):
        module = self.modules["weather"]
        minor = self.wx_alert("urn:1", "Frost Advisory", "Minor")
        severe = self.wx_alert("urn:2", "Blizzard Warning", "Severe")
        test = self.wx_alert("urn:3", "Test Message", "Extreme", status="Test")
        with tempfile.TemporaryDirectory() as tmp:
            http = self.wx_http([minor, test, severe])
            blocks = module.fetch(self.wx_ctx(tmp, http, alerts="severe and up"))
            self.assertEqual(blocks[-3], Text("Alerts in effect", size="small"))
            self.assertEqual(blocks[-2], Text("Blizzard Warning", bold=True))
            blocks = module.fetch(self.wx_ctx(tmp, http))
            self.assertEqual(blocks[-4], Text("Blizzard Warning", bold=True))
            self.assertEqual(blocks[-2], Text("Frost Advisory", bold=True))
            blocks = module.fetch(self.wx_ctx(tmp, http, alerts="off"))
            self.assertNotIn(Text("Alerts in effect", size="small"), blocks)

            class NoAlerts(FakeHttp):
                status = 400

                def get_json(self, url, headers=None):
                    if url.startswith(WX.ALERTS_API):
                        raise HttpFetchError("HTTP error", status=self.status)
                    return super().get_json(url, headers)

            # Outside the US there is no alert service: not an error.
            http = NoAlerts(self.wx_http().json_by_url)
            first = self.wx_ctx(tmp, http)
            self.assertIsInstance(module.fetch(first)[-1], KeyValue)
            with self.assertRaises(Skip):
                module.fetch(self.wx_ctx(tmp, http, store=first.store))
            # An outage fails an alert check but not the morning forecast.
            http.status = 503
            self.assertIsInstance(module.fetch(self.wx_ctx(tmp, http))[-1], KeyValue)
            with self.assertRaises(FetchError):
                module.fetch(self.wx_ctx(tmp, http, store=first.store))

            with self.assertRaisesRegex(FetchError, "set latitude and longitude"):
                module.fetch(self.wx_ctx(tmp, FakeHttp()))
            with self.assertRaisesRegex(FetchError, "must both be numbers"):
                module.fetch(self.wx_ctx(tmp, FakeHttp(), latitude="44.8"))
            with self.assertRaisesRegex(FetchError, "HH:MM"):
                module.fetch(self.wx_ctx(tmp, FakeHttp(), morning_time="8am"))


if __name__ == "__main__":
    unittest.main()
