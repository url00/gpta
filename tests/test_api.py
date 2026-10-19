import http.client
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from gpta import DEFAULT_MODULES_DIR, DEFAULT_WEB_ROOT
from gpta.config import ConfigStore
from gpta.httpserver import create_server
from gpta.logbus import BusLogHandler, LogBus
from gpta.registry import discover
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
        self.server = create_server("127.0.0.1", 0, self.service, DEFAULT_WEB_ROOT)
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
            {"enabled": True, "limits": {"max_items": 1, "max_chars": 500}},
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
        status, _, _ = self.request(
            "PATCH", "/api/modules/affirmations", raw=b"{bad"
        )
        self.assertEqual(status, 400)

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

    def test_host_header_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("GET", "/api/state", skip_host=True)
        conn.putheader("Host", "evil.example")
        conn.endheaders()
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 403)

    def test_token_authentication(self):
        self.server.auth_token = "s3cret"
        status, _, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 401)
        status, _, _ = self.request(
            "GET",
            "/api/state",
            headers={"Authorization": "Bearer s3cret"},
        )
        self.assertEqual(status, 200)
        status, _, _ = self.request(
            "PATCH",
            "/api/modules/affirmations",
            {"enabled": True},
            headers={"X-GPTA-Token": "s3cret"},
        )
        self.assertEqual(status, 200)

    def test_printer_config_roundtrip(self):
        status, body, _ = self.request(
            "PATCH", "/api/printer", {"width": 48, "auto_print": False}
        )
        self.assertEqual(status, 200)
        printer = json.loads(body)["printer"]
        self.assertEqual(printer["width"], 48)
        self.assertFalse(printer["auto_print"])
        status, _, _ = self.request("POST", "/api/printer/test", {})
        self.assertEqual(status, 409)


if __name__ == "__main__":
    unittest.main()
