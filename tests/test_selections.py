import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from gpta import DEFAULT_WEB_ROOT
from gpta.blocks import QrItem, Rule, from_dict, to_dict
from gpta.config import ConfigError, ConfigStore
from gpta.httpserver import create_server
from gpta.logbus import LogBus
from gpta.module_api import (
    Choice,
    Heading,
    Module,
    ModuleContext,
    SelectionError,
    Skip,
    Text,
)
from gpta.renderer import Receipt, render_image, render_text
from gpta.selections import (
    CLAIMED,
    DEBOUNCE_SECONDS,
    EXPIRED,
    MAX_PER_MODULE,
    REPEAT,
    UNKNOWN,
    Selections,
    SelectionStore,
)
from gpta.service import TEST_SELECTIONS, Service
from gpta.storage import Database

PUBLIC = "https://gpta.example.test"


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeWriter:
    """Stands in for the device, so no test reaches a real printer."""

    def __init__(self):
        self.images = []

    def print(self, image, config, *, timeout):
        self.images.append(image)

    def stop(self):
        pass


class Picker(Module):
    id = "picker"
    name = "Picker"
    defaults = {
        "enabled": False,
        "interval_seconds": 3600,
        "limits": {"max_chars": 1000},
        "options": {},
    }

    def fetch(self, ctx: ModuleContext):
        return [
            Heading("Pick one"),
            *ctx.selections.offer(
                [Choice("First story", {"id": 1}, "10 points"), ("Second", {"id": 2})],
                expires_hours=2,
            ),
        ]

    def on_select(self, ctx: ModuleContext, payload):
        if payload["id"] == 2:
            raise Skip("nothing for two")
        ctx.store.set("picked", payload["id"])
        return [Heading(f"Story {payload['id']}"), Text(ctx.trigger)]


class Plain(Module):
    id = "plain"
    name = "Plain"
    defaults = dict(Picker.defaults)

    def fetch(self, ctx):
        return [Text("hi")]


def links(blocks):
    return [block.data for block in blocks if isinstance(block, QrItem)]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = SelectionStore(Database(None), self.clock)
        self.offer = Selections(self.store, "m", PUBLIC + "/")

    def test_offer_makes_one_item_per_choice_with_rules_between(self):
        blocks = self.offer.offer([("A", 1), Choice("B", {"x": 2}, "note")])
        self.assertEqual([type(b) for b in blocks], [QrItem, Rule, QrItem])
        first, second = blocks[0], blocks[2]
        self.assertTrue(first.data.startswith(PUBLIC + "/s/"))
        self.assertEqual((first.label, first.title), ("1.", "A"))
        self.assertEqual((second.label, second.note), ("2.", "note"))
        self.assertNotEqual(first.data, second.data)

    def test_offer_options(self):
        blocks = self.offer.offer(
            [("A", 1), ("B", 2)], numbered=False, side="left", qr="small", rules=False
        )
        self.assertEqual(len(blocks), 2)
        self.assertEqual(
            (blocks[0].label, blocks[0].side, blocks[0].qr), ("", "left", "small")
        )
        self.assertEqual(self.offer.offer([]), [])

    def test_offer_rejects_bad_input(self):
        for kwargs in ({"side": "top"}, {"qr": "giant"}, {"expires_hours": 0}):
            with self.assertRaises(SelectionError):
                self.offer.offer([("A", 1)], **kwargs)
        for choices in ([("", 1)], [("A", object())], [("A", "x" * 5000)], ["A"]):
            with self.assertRaises(SelectionError):
                self.offer.offer(choices)
        with self.assertRaises(SelectionError):
            self.offer.offer([("A", 1)] * 51)

    def test_offer_needs_a_public_url(self):
        offer = Selections(self.store, "m", "")
        self.assertFalse(offer.available)
        with self.assertRaisesRegex(SelectionError, "public URL"):
            offer.offer([("A", 1)])

    def token(self, **kwargs):
        return links(self.offer.offer([("A", {"id": 7})], **kwargs))[0].rsplit("/", 1)[
            1
        ]

    def test_claim_debounces_then_allows_reuse(self):
        token = self.token()
        outcome, selection = self.store.claim(token)
        self.assertEqual(outcome, CLAIMED)
        self.assertEqual(selection["payload"], {"id": 7})
        self.assertEqual(selection["use_count"], 1)
        self.clock.now += DEBOUNCE_SECONDS - 1
        self.assertEqual(self.store.claim(token)[0], REPEAT)
        self.clock.now += 2
        outcome, selection = self.store.claim(token)
        self.assertEqual((outcome, selection["use_count"]), (CLAIMED, 2))

    def test_failed_scan_can_be_retried_at_once(self):
        token = self.token()
        self.store.claim(token)
        self.store.finish(token, "failed: boom", ok=False)
        self.assertEqual(self.store.claim(token)[0], CLAIMED)
        self.store.finish(token, "queued", ok=True)
        self.assertEqual(self.store.claim(token)[0], REPEAT)
        self.assertEqual(self.store.recent()[0]["last_result"], "queued")

    def test_expiry_and_unknown(self):
        token = self.token(expires_hours=1)
        self.clock.now += 3601
        self.assertEqual(self.store.claim(token)[0], EXPIRED)
        self.assertEqual(self.store.claim("nope"), (UNKNOWN, None))
        self.token()  # making a new list drops expired codes
        self.assertEqual(self.store.claim(token)[0], UNKNOWN)

    def test_per_module_cap_keeps_the_newest(self):
        first = self.token()
        for _ in range(MAX_PER_MODULE // 50):
            self.clock.now += 1
            self.offer.offer([("x", 1)] * 50)
        self.assertEqual(self.store.claim(first)[0], UNKNOWN)
        self.assertEqual(len(self.store.recent(1000)), MAX_PER_MODULE)

    def test_default_context_works_without_a_host(self):
        ctx = ModuleContext("t", {}, {}, Path("."), None, None, None, print)
        self.assertTrue(
            links(ctx.selections.offer([("A", 1)]))[0].startswith("http://gpta.test/s/")
        )


class QrItemTests(unittest.TestCase):
    def test_round_trip_and_validation(self):
        item = QrItem("https://x.test/s/abc", "Title", "note", "1.", "left", "large")
        self.assertEqual(from_dict(to_dict(item)), item)
        for bad in ({"side": "top"}, {"qr": "big"}):
            with self.assertRaises(ValueError):
                QrItem("x", "t", **bad)
        with self.assertRaises(ValueError):
            QrItem("", "t")

    def dark(self, image, box):
        return sum(1 for v in image.convert("L").crop(box).tobytes() if v == 0)

    def test_code_is_drawn_on_the_chosen_side(self):
        for side in ("left", "right"):
            item = QrItem(
                "https://gpta.example.test/s/abcdefghijklmnop", "T", side=side
            )
            image = render_image(Receipt("", (item,)), 576)
            half = image.width // 2
            left = self.dark(image, (0, 0, half, image.height))
            right = self.dark(image, (half, 0, image.width, image.height))
            self.assertGreater(*((left, right) if side == "left" else (right, left)))

    def test_sizes_and_columns_and_text(self):
        def height(qr):
            item = QrItem("https://gpta.example.test/s/abcdefghijklmnop", "T", qr=qr)
            return render_image(Receipt("", (item,)), 576).height

        self.assertLess(height("small"), height("medium"))
        self.assertLess(height("medium"), height("large"))
        items = tuple(
            QrItem(f"https://x.test/s/{n}", f"Item {n} " * 6, "note") for n in range(4)
        )
        for layout in ("single", "double", "triple"):
            render_image(Receipt("T", items, layout=layout), 384)
        lines = render_text(
            Receipt("", (QrItem("https://x.test/s/a", "Title", label="1."),)), 32
        )
        self.assertEqual(lines, ["1. Title", "[QR: https://x.test/s/a]", ""])


class ConfigTests(unittest.TestCase):
    def test_public_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json", {})
            store.load()
            self.assertEqual(store.snapshot()["server"]["public_url"], "")
            self.assertEqual(
                store.update_server({"public_url": " https://Gpta.example.test/ "})[
                    "public_url"
                ],
                "https://Gpta.example.test",
            )
            for bad in (
                "ftp://x",
                "example.test",
                "https://u:p@x.test",
                "https://x.test/?a=1",
                "https://x y",
                5,
            ):
                with self.assertRaises(ConfigError, msg=bad):
                    store.update_server({"public_url": bad})
            with self.assertRaises(ConfigError):
                store.update_server({"port": 1})
            again = ConfigStore(Path(tmp) / "config.json", {})
            again.load()
            self.assertEqual(
                again.snapshot()["server"]["public_url"], "https://Gpta.example.test"
            )


class ScanTests(unittest.TestCase):
    """The whole path: a module's list, the scan route, the print queue."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = {"picker": Picker(), "plain": Plain()}
        self.config = ConfigStore(Path(self.tmp.name) / "config.json", self.registry)
        self.config.load()
        self.clock = Clock()
        self.writer = FakeWriter()
        self.service = Service(
            self.config,
            self.registry,
            LogBus(),
            clock=self.clock,
            printer_writer=self.writer,
        )
        self.server = create_server(
            "127.0.0.1", 0, self.service, DEFAULT_WEB_ROOT, allowed_hosts=["127.0.0.1"]
        )
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.service.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        hdrs = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers=hdrs
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read().decode(), response.headers
        except urllib.error.HTTPError as exc:
            text = exc.read().decode()
            exc.close()
            return exc.code, text, exc.headers

    def enable(self):
        self.request("PATCH", "/api/server", {"public_url": PUBLIC})
        self.config.update_printer({"enabled": True})

    def codes(self, module="picker"):
        return [
            item
            for item in json.loads(self.request("GET", "/api/selections")[1])[
                "selections"
            ]
            if item["module"] == module
        ]

    def jobs(self):
        queue = self.service.queue()
        return [queue["current"], *queue["pending"], *queue["history"]]

    def test_list_fails_without_public_url(self):
        result = self.service.run_module("picker")
        self.assertFalse(result["ok"])
        self.assertIn("public URL", result["error"])

    def test_scan_prints_the_selected_item(self):
        self.enable()
        self.assertTrue(self.service.run_module("picker")["ok"])
        output = self.service.output_text("picker")
        self.assertIn("1. First story", output)
        self.assertIn(f"{PUBLIC}/s/", output)
        second, first = self.codes()  # newest first
        self.assertEqual(first["title"], "First story")
        before = len([job for job in self.jobs() if job])

        status, page, headers = self.request("GET", first["path"])
        self.assertEqual(status, 200)
        self.assertIn("Printing", page)
        self.assertIn("First story", page)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(len([job for job in self.jobs() if job]), before + 1)

        # A pre-fetch plus the real open prints once.
        status, page, _ = self.request("GET", first["path"])
        self.assertEqual(status, 200)
        self.assertIn("Already sent", page)
        self.assertEqual(len([job for job in self.jobs() if job]), before + 1)
        self.assertEqual(self.codes()[1]["use_count"], 1)
        self.assertIn("queued", self.codes()[1]["last_result"])

        # Reusable after the debounce.
        self.clock.now += DEBOUNCE_SECONDS + 1
        self.assertIn("Printing", self.request("GET", first["path"])[1])

        # Skip prints nothing; on_select's store writes were saved.
        status, page, _ = self.request("GET", second["path"])
        self.assertEqual(status, 200)
        self.assertIn("Nothing to print", page)

    def test_scan_guards(self):
        self.enable()
        self.service.run_module("picker")
        path = self.codes()[1]["path"]
        self.assertEqual(self.request("HEAD", path)[0], 405)
        self.assertEqual(self.request("POST", path, {})[0], 405)
        self.assertEqual(self.request("GET", "/s/" + "x" * 16)[0], 404)
        self.assertEqual(self.request("GET", "/s/short")[0], 404)
        self.assertEqual(
            self.request("GET", path, headers={"Host": "evil.test"})[0], 403
        )
        # The public host is accepted without being in allowed_hosts.
        status, page, _ = self.request(
            "GET", path, headers={"Host": "gpta.example.test"}
        )
        self.assertEqual((status, "Printing" in page), (200, True))
        self.clock.now += 3 * 3600
        self.assertEqual(self.request("GET", path)[0], 410)

    def test_failures_are_reported_and_retryable(self):
        self.request("PATCH", "/api/server", {"public_url": PUBLIC})
        self.service.run_module("picker")
        path = self.codes()[1]["path"]
        status, page, _ = self.request("GET", path)  # printer is off
        self.assertEqual(status, 409)
        self.assertIn("Printer is off", page)
        self.config.update_printer({"enabled": True})
        self.assertEqual(
            self.request("GET", path)[0], 200
        )  # no debounce after a failure

        # A module without on_select, and one that is gone.
        offer = Selections(self.service.runner.selections, "plain", PUBLIC)
        offer.offer([("<b>x</b>", 1)])
        status, page, _ = self.request("GET", self.codes("plain")[0]["path"])
        self.assertEqual(status, 502)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)
        self.assertNotIn("<b>x</b>", page)
        Selections(self.service.runner.selections, "gone", PUBLIC).offer([("y", 1)])
        self.assertEqual(self.request("GET", self.codes("gone")[0]["path"])[0], 404)

    def test_selection_test_screen(self):
        status, text, _ = self.request("POST", "/api/selections/test", {})
        self.assertEqual(status, 400)
        self.assertIn("public URL", text)
        self.enable()
        for bad in (
            {"count": 0},
            {"side": "up"},
            {"qr": "x"},
            {"numbered": 1},
            {"expires_hours": 0},
        ):
            self.assertEqual(
                self.request("POST", "/api/selections/test", bad)[0], 400, bad
            )
        status, text, _ = self.request(
            "POST", "/api/selections/test", {"count": 2, "side": "left", "qr": "small"}
        )
        data = json.loads(text)
        self.assertEqual(status, 200)
        self.assertTrue(data["png"].startswith("data:image/png;base64,"))
        self.assertNotIn("job", data)
        self.assertEqual(len(self.codes(TEST_SELECTIONS)), 2)
        data = json.loads(
            self.request("POST", "/api/selections/test", {"count": 1, "print": True})[1]
        )
        self.assertEqual(data["job"]["source"], "test")
        status, page, _ = self.request("GET", self.codes(TEST_SELECTIONS)[0]["path"])
        self.assertEqual(status, 200)
        self.assertIn("Test item 1", page)

    def test_state_and_server_patch(self):
        self.assertEqual(
            self.request("PATCH", "/api/server", {"public_url": "nope"})[0], 400
        )
        self.assertEqual(
            self.request("PATCH", "/api/server", {"host": "0.0.0.0"})[0], 400
        )
        self.enable()
        state = json.loads(self.request("GET", "/api/state")[1])
        self.assertEqual(state["server"]["public_url"], PUBLIC)


if __name__ == "__main__":
    unittest.main()
