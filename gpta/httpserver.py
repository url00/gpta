"""Threaded HTTP API and static web console."""

from __future__ import annotations

import json
import logging
import mimetypes
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .config import ConfigError
from .printer import PrinterError, PrinterRateLimited
from .service import (
    AlreadyRunning,
    NotFound,
    PrinterDisabled,
    Service,
    ValidationError,
)

log = logging.getLogger("gpta.httpserver")

MAX_BODY = 64 * 1024


class GptaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        service: Service,
        web_root: Path,
    ):
        self.service = service
        self.web_root = Path(web_root).resolve()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "gpta/0.1"
    protocol_version = "HTTP/1.1"

    # -- verbs ---------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch(self._get)

    def do_POST(self) -> None:
        self._dispatch(self._post)

    def do_PATCH(self) -> None:
        self._dispatch(self._patch)

    def do_HEAD(self) -> None:
        self._send_headers(405, "text/plain; charset=utf-8", 0)

    # -- routing -------------------------------------------------------
    def _get(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        if path == "/":
            self._serve_static("index.html")
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
            return
        if path == "/api/state":
            self._json(200, self.server.service.snapshot())
            return
        if path == "/api/logs":
            query = urllib.parse.parse_qs(parsed.query)
            raw = query.get("since", ["0"])[0]
            try:
                since = int(raw)
            except ValueError as exc:
                raise ValidationError("since must be an integer") from exc
            entries = self.server.service.logbus.since(since)
            next_seq = entries[-1]["seq"] if entries else since
            self._json(200, {"entries": entries, "next": next_seq})
            return
        if path.startswith("/api/output/"):
            key = path[len("/api/output/") :]
            if not key or "/" in key:
                raise NotFound("unknown output")
            text = self.server.service.output_text(key)
            self._text(200, text)
            return
        raise NotFound("not found")

    def _post(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        if path.startswith("/api/modules/"):
            parts = [p for p in path[len("/api/modules/") :].split("/") if p]
            if len(parts) == 2 and parts[1] == "run":
                result = self.server.service.run_module(parts[0], trigger="manual")
                self._json(200, result)
                return
        if path == "/api/printer/test":
            self._json(200, self.server.service.test_printer())
            return
        raise NotFound("not found")

    def _patch(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        body = self._read_json()
        if path.startswith("/api/modules/"):
            module_id = path[len("/api/modules/") :]
            if not module_id or "/" in module_id:
                raise NotFound("unknown module")
            module = self.server.service.update_module(module_id, body)
            self._json(200, {"module": module})
            return
        if path == "/api/printer":
            printer = self.server.service.set_printer(body)
            self._json(200, {"printer": printer})
            return
        raise NotFound("not found")

    # -- dispatch ------------------------------------------------------
    def _dispatch(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except NotFound as exc:
            self._json(404, {"error": str(exc)})
        except (ValidationError, ConfigError) as exc:
            self._json(400, {"error": str(exc)})
        except AlreadyRunning as exc:
            self._json(409, {"error": str(exc)})
        except PrinterDisabled as exc:
            self._json(409, {"error": str(exc)})
        except PrinterRateLimited as exc:
            self._json(429, {"error": str(exc)})
        except PrinterError as exc:
            self._json(500, {"error": str(exc)})
        except BrokenPipeError:  # pragma: no cover - client went away
            pass
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("unhandled request error")
            self._json(500, {"error": f"internal error: {exc}"})

    # -- responses -----------------------------------------------------
    def _send_headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode("utf-8")
        self._send_headers(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def _text(self, status: int, text: str) -> None:
        data = text.encode("utf-8")
        self._send_headers(status, "text/plain; charset=utf-8", len(data))
        self.wfile.write(data)

    def _serve_static(self, rel: str) -> None:
        if not rel or "\x00" in rel:
            raise NotFound("not found")
        root = self.server.web_root
        candidate = (root / rel).resolve()
        if candidate != root and root not in candidate.parents:
            raise NotFound("not found")
        if not candidate.is_file():
            raise NotFound("not found")
        try:
            data = candidate.read_bytes()
        except OSError as exc:
            raise NotFound("not found") from exc
        ctype = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in (
            "application/javascript",
            "application/json",
        ):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if candidate.suffix in (".html", ".htm"):
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self'; script-src 'self'; "
                "img-src 'self' data:",
            )
        self.end_headers()
        self.wfile.write(data)

    # -- request body --------------------------------------------------
    def _read_json(self) -> dict[str, Any]:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ValidationError("Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise ValidationError("invalid Content-Length") from exc
        if length < 0 or length > MAX_BODY:
            raise ValidationError("request body too large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ValidationError("JSON body must be an object")
        return payload

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)


def create_server(
    host: str,
    port: int,
    service: Service,
    web_root: Path,
) -> GptaHTTPServer:
    return GptaHTTPServer((host, port), service, web_root)
