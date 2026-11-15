import http.client
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

from gpta import DEFAULT_MODULES_DIR, DEFAULT_WEB_ROOT
from gpta.config import ConfigStore
from gpta.httpserver import create_server
from gpta.logbus import BusLogHandler, LogBus
from gpta.module_api import Text
from gpta.registry import discover
from gpta.renderer import build_receipt
from gpta.service import Service


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = discover(DEFAULT_MODULES_DIR)
        self.store = ConfigStore(Path(self.tmp.name) / "config.json", self.registry)
        self.store.load()
        self.bus = LogBus()
        import logging

        self.logger = logging.getLogger("gpta")
        self.handler = BusLogHandler(self.bus)
        self.logger.addHandler(self.handler)
        self.service = Service(
            self.store, self.registry, self.bus, modules_dir=DEFAULT_MODULES_DIR
        )
        self.server = create_server(
            "127.0.0.1",
            0,
            self.service,
            DEFAULT_WEB_ROOT,
            allowed_hosts=["localhost", "127.0.0.1", "gpta.example.test"],
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.logger.removeHandler(self.handler)
        self.service.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None, raw=None, headers=None):
        data = raw
        hdrs = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers=hdrs
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            body = exc.read()
            exc.close()
            return exc.code, body, exc.headers

    def json(self, method, path, body=None):
        status, raw, _ = self.request(method, path, body)
        return status, json.loads(raw)

    def test_queue_routes(self):
        from unittest.mock import patch

        from PIL import Image

        status, queue = self.json("GET", "/api/queue")
        self.assertEqual(status, 200)
        self.assertEqual(
            (queue["paused"], queue["pending"], queue["current"]), (False, [], None)
        )
        self.assertEqual(queue["warn_length_mm"], 2500)
        # Printing while the printer is off is refused, not queued.
        status, _ = self.json("POST", "/api/printer/test", {})
        self.assertEqual(status, 409)

        self.request("PATCH", "/api/printer", {"enabled": True, "warn_length_mm": 100})
        status, queue = self.json("POST", "/api/queue/margins", {})
        self.assertEqual((status, queue["wide_margins"]), (200, True))
        status, queue = self.json("POST", "/api/queue/no-margins", {})
        self.assertEqual((status, queue["wide_margins"]), (200, False))
        status, queue = self.json("POST", "/api/queue/pause", {})
        self.assertTrue(queue["paused"])
        state = json.loads(self.request("GET", "/api/state")[1])
        self.assertTrue(state["printer"]["queue"]["paused"])

        with patch("gpta.printing.transport.send_to_device") as send:
            status, test = self.json("POST", "/api/printer/test", {})
            self.assertEqual((status, test["job"]["state"]), (200, "pending"))
            long = self.service.printer.submit(
                Image.new("1", (576, 2000), 1), "instant", manual=True
            )
            first, second = test["job"]["id"], long["id"]
            status, raw, headers = self.request("GET", f"/api/queue/{second}.png")
            self.assertEqual((status, headers["Content-Type"]), (200, "image/png"))
            self.assertTrue(raw.startswith(b"\x89PNG"))

            status, queue = self.json(
                "POST", f"/api/queue/{second}/move", {"to": "top"}
            )
            self.assertEqual([j["id"] for j in queue["pending"]], [second, first])
            self.assertTrue(queue["pending"][0]["needs_approval"])
            status, _ = self.json("POST", f"/api/queue/{second}/move", {"to": "x"})
            self.assertEqual(status, 400)

            status, queue = self.json("POST", "/api/queue/resume", {})
            self.assertTrue(self.service.printer.wait_idle())
            send.assert_not_called()  # the long job holds the queue
            status, queue = self.json("POST", f"/api/queue/{second}/approve", {})
            self.assertEqual(status, 200)
            self.assertTrue(self.service.printer.wait_idle())
            self.assertEqual(send.call_count, 2)

        status, queue = self.json("GET", "/api/queue")
        self.assertEqual(queue["pending"], [])
        self.assertEqual([j["state"] for j in queue["history"]], ["done", "done"])
        for path in (f"/api/queue/{second}/cancel", "/api/queue/999/approve"):
            status, _ = self.json("POST", path, {})
            self.assertEqual(status, 404, path)
        status, _ = self.json("GET", "/api/queue/999.png")
        self.assertEqual(status, 404)
        status, queue = self.json("POST", "/api/queue/clear", {})
        self.assertEqual(status, 200)
        status, _, _ = self.request("GET", "/api/queue/pause")
        self.assertEqual(status, 405)

    def test_module_style_settings(self):
        status, body = self.json(
            "PATCH", "/api/modules/affirmations", {"style": {"size": "small"}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            body["module"]["style"], {"size": "small", "layout": "default"}
        )
        for bad in (
            {"style": {"size": "giant"}},
            {"style": {"font": "x"}},
            {"style": 3},
        ):
            status, _ = self.json("PATCH", "/api/modules/affirmations", bad)
            self.assertEqual(status, 400, bad)
        status, body = self.json(
            "PATCH", "/api/printer", {"module_layout": "double", "module_size": "large"}
        )
        self.assertEqual(status, 200)
        self.request("POST", "/api/modules/affirmations/run", {})
        receipt = self.service.outputs.get("affirmations").receipt
        # The module's own setting beats the printer default; the rest falls
        # back to it.
        self.assertEqual((receipt.size, receipt.layout), ("small", "double"))
        status, _ = self.json("PATCH", "/api/printer", {"module_size": "giant"})
        self.assertEqual(status, 400)

    def test_state_and_static(self):
        status, body, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(
            [m["id"] for m in state["modules"]],
            [
                "affirmations",
                "google_calendar",
                "google_tasks",
                "hackernews",
                "weather",
            ],
        )
        self.assertFalse(state["printer"]["enabled"])
        self.assertNotIn("logs", state)
        self.assertEqual(state["registry_errors"], [])
        for path in ("/", "/privacy", "/static/app.js", "/static/style.css"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(status, 200, path)

    def test_module_patch_and_run(self):
        status, body, _ = self.request(
            "PATCH",
            "/api/modules/affirmations",
            {"enabled": True, "limits": {"max_chars": 500}},
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["module"]["enabled"])
        status, body, _ = self.request("POST", "/api/modules/affirmations/run", {})
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["ok"])
        status, body, _ = self.request("GET", "/api/output/affirmations")
        self.assertEqual(status, 200)
        self.assertIn(b"DAILY AFFIRMATION", body)
        status, body, _ = self.request("GET", "/api/output/total")
        self.assertEqual(status, 200)
        self.assertIn(b"DAILY AFFIRMATION", body)

    def png(self, path):
        status, body, headers = self.request("GET", path)
        self.assertEqual(status, 200, body)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["Cache-Control"], "no-store")
        return Image.open(io.BytesIO(body))

    def test_png_outputs(self):
        for path in ("/api/output/affirmations.png", "/api/output/total.png"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(status, 404, path)  # nothing has run yet
        status, _, _ = self.request("GET", "/api/output/nope.png")
        self.assertEqual(status, 404)

        for module_id in ("affirmations", "hackernews"):
            self.request("PATCH", f"/api/modules/{module_id}", {"enabled": True})
        self.service.outputs.set(
            "hackernews",
            build_receipt("Hacker News Weekly", [Text("Top story.")]),
            "2026-10-18T12:00:00+00:00",
        )
        self.request("POST", "/api/modules/affirmations/run", {})
        module = self.png("/api/output/affirmations.png")
        self.assertEqual(module.size[0], 576)
        self.assertIn(module.mode, ("1", "L"))
        total = self.png("/api/output/total.png")
        self.assertEqual(total.size[0], 576)
        other = self.png("/api/output/hackernews.png")
        self.assertGreater(total.size[1], module.size[1] + other.size[1])
        state = json.loads(self.request("GET", "/api/state")[1])
        self.assertEqual(state["total"]["module_ids"], ["affirmations", "hackernews"])

        # Changing the paper re-renders the existing output at the new width.
        self.request("PATCH", "/api/printer", {"paper": "58mm"})
        self.assertEqual(self.png("/api/output/affirmations.png").size[0], 384)
        self.assertEqual(self.png("/api/output/total.png").size[0], 384)
        status, body, headers = self.request("GET", "/api/output/affirmations")
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.assertTrue(all(len(line) <= 32 for line in body.decode().splitlines()))

    def test_validation_and_not_found(self):
        status, _, _ = self.request(
            "PATCH", "/api/modules/affirmations", {"interval_seconds": 1}
        )
        self.assertEqual(status, 400)
        status, _, _ = self.request(
            "PATCH", "/api/modules/affirmations", {"unknown": True}
        )
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/api/modules/nope/run", {})
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", "/api/output/nope")
        self.assertEqual(status, 404)
        status, _, _ = self.request("PATCH", "/api/modules/affirmations", raw=b"{bad")
        self.assertEqual(status, 400)

    def test_wrong_method_returns_405_with_allow(self):
        status, _, headers = self.request("GET", "/api/printer/test")
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "POST")
        status, _, headers = self.request("DELETE", "/api/state")
        self.assertEqual(status, 501)  # verb not implemented at all
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            status, body = self.raw_request(conn, "HEAD", "/api/state")
            self.assertEqual((status, body), (405, b""))
            status, _ = self.raw_request(conn, "GET", "/api/state")
            self.assertEqual(status, 200)
        finally:
            conn.close()

    def test_static_traversal_blocked(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/static/../gpta/__init__.py")
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 404)

    def test_logs_endpoint_is_incremental(self):
        status, body, _ = self.request("GET", "/api/logs?since=0")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIn("entries", payload)
        self.assertIn("next", payload)
        status, body, _ = self.request("GET", f"/api/logs?since={payload['next']}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["entries"], [])

    def raw_request(self, conn, method, path, body=b"", headers=None, host=None):
        conn.putrequest(method, path, skip_host=host is not None)
        if host is not None:
            conn.putheader("Host", host)
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        if body or method in ("POST", "PATCH"):
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body or None)
        response = conn.getresponse()
        return response.status, response.read()

    def test_allowed_host_header_accepted(self):
        # A tunnel forwards the public hostname listed in allowed_hosts.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            status, body = self.raw_request(
                conn, "GET", "/api/state", host="gpta.example.test:443"
            )
        finally:
            conn.close()
        self.assertEqual(status, 200, body)

    def test_unknown_host_rejected(self):
        # DNS rebinding: the attacker's page still sends its own hostname.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            status, _ = self.raw_request(
                conn, "GET", "/api/state", host="evil.example:8787"
            )
        finally:
            conn.close()
        self.assertEqual(status, 403)

    def test_cross_origin_write_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            status, _ = self.raw_request(
                conn,
                "PATCH",
                "/api/printer",
                b'{"paper": "58mm"}',
                {"Content-Type": "application/json", "Origin": "https://evil.example"},
            )
            self.assertEqual(status, 403)
            status, body = self.raw_request(
                conn,
                "PATCH",
                "/api/printer",
                b'{"paper": "58mm"}',
                {
                    "Content-Type": "application/json",
                    "Origin": "https://gpta.example.test",
                },
            )
            self.assertEqual(status, 200, body)
        finally:
            conn.close()

    def test_post_requires_json(self):
        # A cross-site HTML form can only send "simple" content types.
        status, _, _ = self.request(
            "POST",
            "/api/modules/affirmations/run",
            raw=b"a=b",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/api/printer/test", raw=b"")
        self.assertEqual(status, 400)

    def test_keep_alive_survives_bodies(self):
        # Unread body bytes used to corrupt the next request on the connection.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        json_headers = {"Content-Type": "application/json"}
        try:
            status, _ = self.raw_request(
                conn, "POST", "/api/modules/affirmations/run", b"{}", json_headers
            )
            self.assertEqual(status, 200)
            status, _ = self.raw_request(conn, "GET", "/api/logs?since=0")
            self.assertEqual(status, 200)
            status, _ = self.raw_request(
                conn,
                "PATCH",
                "/api/printer",
                b'{"paper": "58mm"}',
                {"Content-Type": "text/plain"},
            )
            self.assertEqual(status, 400)
            status, _ = self.raw_request(conn, "GET", "/api/logs?since=0")
            self.assertEqual(status, 200)
        finally:
            conn.close()

    def test_printer_config_roundtrip(self):
        status, body, _ = self.request("GET", "/api/state")
        printer = json.loads(body)["printer"]
        self.assertEqual((printer["paper"], printer["width_px"]), ("80mm", 576))
        self.assertNotIn("width", printer)
        status, body, _ = self.request(
            "PATCH", "/api/printer", {"paper": "58mm", "auto_print": False}
        )
        self.assertEqual(status, 200)
        printer = json.loads(body)["printer"]
        self.assertEqual((printer["paper"], printer["width_px"]), ("58mm", 384))
        self.assertFalse(printer["auto_print"])
        for bad in ({"paper": "A4"}, {"paper": None}, {"width": 48}):
            status, _, _ = self.request("PATCH", "/api/printer", bad)
            self.assertEqual(status, 400, bad)
        status, _, _ = self.request("POST", "/api/printer/test", {})
        self.assertEqual(status, 409)

    def test_instant_preview_and_print(self):
        status, body, _ = self.request(
            "POST", "/api/instant/preview", {"text": "héllo — wörld", "size": "huge"}
        )
        self.assertEqual(status, 200)
        preview = json.loads(body)
        self.assertTrue(preview["png"].startswith("data:image/png;base64,"))
        small = json.loads(
            self.request(
                "POST",
                "/api/instant/preview",
                {"text": "héllo — wörld", "size": "small"},
            )[1]
        )
        self.assertGreater(preview["height_px"], small["height_px"])
        self.assertEqual(preview["chars"], 13)
        self.assertEqual(preview["paper_mm"], round(preview["height_px"] / 8))
        # Instant routes take long pastes; other routes keep the small limit.
        big = {"text": "word " * 20_000, "format": "rich"}  # 100 kB
        status, body, _ = self.request("POST", "/api/instant/preview", big)
        self.assertEqual(
            (status, json.loads(body)["error"]),
            (400, "format must be one of plain, markdown"),
        )
        status, body, _ = self.request("POST", "/api/reader/fetch", big)
        self.assertEqual(
            (status, json.loads(body)["error"]), (400, "request body too large")
        )
        for bad in (
            {},
            {"text": "  \n "},
            {"text": "x", "size": "giant"},
            {"text": "x" * 150_001},
            {"text": 5},
        ):
            status, _, _ = self.request("POST", "/api/instant/preview", bad)
            self.assertEqual(status, 400, bad)
        status, body, _ = self.request(
            "POST",
            "/api/instant/preview",
            {"text": "word " * 400, "layout": "double"},
        )
        self.assertEqual(status, 200, body)
        status, _, _ = self.request(
            "POST", "/api/instant/preview", {"text": "x", "layout": "quad"}
        )
        self.assertEqual(status, 400)
        status, body, _ = self.request(
            "POST",
            "/api/instant/preview",
            {"text": "# List\n- **one**\n- two", "format": "markdown"},
        )
        self.assertEqual(status, 200, body)
        for bad in (
            {"text": "x", "format": "html"},
            {"text": "\n---\n", "format": "plain", "layout": "x"},
            {"text": "```\n```", "format": "markdown"},
        ):
            status, _, _ = self.request("POST", "/api/instant/preview", bad)
            self.assertEqual(status, 400, bad)
        # The printer is disabled in these tests.
        status, _, _ = self.request("POST", "/api/instant/print", {"text": "hi"})
        self.assertEqual(status, 409)
        status, _, _ = self.request("POST", "/api/instant/print", {"text": ""})
        self.assertEqual(status, 400)

    def test_reader_fetch_and_print(self):
        from gpta.reader import Page, ReaderError

        html = (
            "<html><head><title>A walk – Blog</title></head><body><article>"
            "<p>The receipt printer hums along quietly, line after line.</p>"
            "</article></body></html>"
        )
        seen = []

        def fetcher(url):
            seen.append(url)
            if url == "https://bad.example/":
                raise ReaderError("the site answered HTTP 404")
            return Page("https://blog.example/walk", html)

        self.service.reader_fetcher = fetcher
        status, body, _ = self.request(
            "POST", "/api/reader/fetch", {"url": "https://blog.example/walk"}
        )
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertEqual(result["title"], "A walk")
        self.assertEqual(result["url"], "https://blog.example/walk")
        self.assertTrue(result["png"].startswith("data:image/png;base64,"))
        self.assertEqual(result["chars"], result["total"])
        self.assertEqual(result["chars"], 56)
        self.assertGreater(result["paper_mm"], 0)
        self.assertEqual(result["paper_mm"], round(result["height_px"] / 8))

        # A long article comes back whole.
        words = " ".join(f"word{i:05d}" for i in range(5000))  # 49999 chars
        long_html = f"<html><body><article><p>{words}</p></article></body></html>"
        self.service.reader_fetcher = lambda url: Page(
            "https://blog.example/l", long_html
        )
        status, body, _ = self.request(
            "POST", "/api/reader/fetch", {"url": "x", "size": "tiny"}
        )
        self.assertEqual(status, 200, body)
        whole = json.loads(body)
        self.assertEqual((whole["chars"], whole["total"]), (49999, 49999))
        self.assertGreater(whole["paper_mm"], 1000)

        # Runaway pages are cut, and the counts say so.
        from gpta import service as service_module

        old_max = service_module.READER_MAX_CHARS
        service_module.READER_MAX_CHARS = 1000
        try:
            status, body, _ = self.request(
                "POST", "/api/reader/fetch", {"url": "x", "size": "tiny"}
            )
        finally:
            service_module.READER_MAX_CHARS = old_max
        cut = json.loads(body)
        self.assertEqual((cut["chars"], cut["total"]), (1000, 49999))
        self.assertLess(cut["height_px"], whole["height_px"])

        status, _, _ = self.request(
            "POST", "/api/reader/fetch", {"url": "x", "size": "giant"}
        )
        self.assertEqual(status, 400)
        self.service.reader_fetcher = fetcher

        status, body, _ = self.request(
            "POST", "/api/reader/fetch", {"url": "https://bad.example/"}
        )
        self.assertEqual(
            (status, json.loads(body)["error"]), (400, "the site answered HTTP 404")
        )

        # The printer is disabled in these tests.
        status, _, _ = self.request("POST", "/api/reader/print", {"id": result["id"]})
        self.assertEqual(status, 409)
        for bad in (
            {},
            {"id": ""},
            {"id": 5},
            {"id": "not-a-fetched-page"},
            {"id": result["id"], "size": "giant"},
            {"id": result["id"], "layout": "quad"},
        ):
            status, _, _ = self.request("POST", "/api/reader/print", bad)
            self.assertEqual(status, 400, bad)


if __name__ == "__main__":
    unittest.main()
