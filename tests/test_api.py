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

    def test_state_and_static(self):
        status, body, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(
            [m["id"] for m in state["modules"]],
            ["affirmations", "google_tasks", "gutenberg", "hackernews"],
        )
        self.assertFalse(state["printer"]["enabled"])
        self.assertNotIn("logs", state)
        self.assertEqual(state["registry_errors"], [])
        for path in ("/", "/static/app.js", "/static/style.css"):
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

        for module_id in ("affirmations", "gutenberg"):
            self.request("PATCH", f"/api/modules/{module_id}", {"enabled": True})
        self.service.outputs.set(
            "gutenberg",
            build_receipt("Project Gutenberg", [Text("Call me Ishmael.")]),
            "2026-10-18T12:00:00+00:00",
        )
        self.request("POST", "/api/modules/affirmations/run", {})
        module = self.png("/api/output/affirmations.png")
        self.assertEqual(module.size[0], 576)
        self.assertIn(module.mode, ("1", "L"))
        total = self.png("/api/output/total.png")
        self.assertEqual(total.size[0], 576)
        gutenberg = self.png("/api/output/gutenberg.png")
        self.assertGreater(total.size[1], module.size[1] + gutenberg.size[1])
        state = json.loads(self.request("GET", "/api/state")[1])
        self.assertEqual(state["total"]["module_ids"], ["affirmations", "gutenberg"])

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


if __name__ == "__main__":
    unittest.main()
