import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from gpta import DEFAULT_MODULES_DIR
from gpta.blocks import text_of
from gpta.http import FetchError as HttpFetchError
from gpta.module_api import (
    BarChart,
    Cache,
    Checkbox,
    FetchError,
    Heading,
    ModuleContext,
    Skip,
    Text,
)
from gpta.registry import discover
from gpta.renderer import build_receipt, render_image

# Use the module objects the registry loaded, rather than importing
# modules.<id>.module a second time under another name.
MODULES = discover(DEFAULT_MODULES_DIR)


def source(module_id):
    return sys.modules[type(MODULES[module_id]).__module__]


HN = source("hackernews")
GT = source("google_tasks")
GC = source("google_calendar")

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

    def test_hackernews_sort_and_window(self):
        old = int(NOW.timestamp()) - 10 * 86400
        recent = int(NOW.timestamp()) - 86400
        http = FakeHttp(
            {
                HN.TOP_STORIES: [1, 2, 3],
                HN.ITEM.format(1): {
                    "id": 1,
                    "type": "story",
                    "title": "One",
                    "score": 100,
                    "time": recent,
                    "descendants": 3,
                    "url": "https://one.example",
                },
                HN.ITEM.format(2): {
                    "id": 2,
                    "type": "story",
                    "title": "Two",
                    "score": 200,
                    "time": recent,
                    "descendants": 4,
                },
                HN.ITEM.format(3): {
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
        text = "\n".join(map(text_of, blocks))
        self.assertIn("Two", text)
        self.assertIn("One", text)
        self.assertNotIn("Old", text)
        self.assertLess(text.index("Two"), text.index("One"))
        self.assertIn("200 points", text)
        chart = blocks[-1]
        self.assertIsInstance(chart, BarChart)
        self.assertEqual([bar.value for bar in chart.bars], [200, 100])
        self.assertEqual(render_image(build_receipt("HN", blocks), 384).width, 384)

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


if __name__ == "__main__":
    unittest.main()
