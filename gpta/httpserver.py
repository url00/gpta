"""Threaded HTTP API and static web console."""

from __future__ import annotations

import html
import json
import logging
import mimetypes
import re
import urllib.parse
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigError, public_host
from .errors import AlreadyRunning, InvalidRequest, NotFound
from .printing import PrinterDisabled, PrinterError, PrinterRateLimited
from .selections import SCAN_PATH
from .service import ScanResult, Service

log = logging.getLogger("gpta.httpserver")

MAX_BODY = 64 * 1024
# Instant prints carry the pasted text itself (up to 150,000 characters).
LARGE_BODY_PATHS = {"/api/instant/preview", "/api/instant/print"}
MAX_LARGE_BODY = 1024 * 1024
STATE_CHANGING = {"POST", "PATCH"}


WILDCARD_HOSTS = {"", "0.0.0.0", "::"}


class Forbidden(Exception):
    """Request rejected by the Host or Origin check (403)."""


class ValidationError(Exception):
    """Malformed request (400)."""


class MethodNotAllowed(Exception):
    """The path exists but not for this method (405)."""

    def __init__(self, allowed: list[str]):
        super().__init__("method not allowed")
        self.allowed = allowed


# (method, path pattern, handler name). Handler ``_h_<name>`` receives the
# pattern's named groups as keyword arguments. POST/PATCH bodies are parsed
# before the handler runs and are available as ``self._body_json``.
ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    (method, re.compile(pattern), name)
    for method, pattern, name in (
        ("GET", r"/", "index"),
        ("GET", r"/privacy", "privacy"),
        ("GET", r"/favicon\.ico", "favicon"),
        ("GET", r"/static/(?P<rel>.+)", "static"),
        ("GET", r"/api/state", "state"),
        ("GET", r"/api/logs", "logs"),
        ("GET", r"/api/output/(?P<key>[^/]+)\.png", "output_png"),
        ("GET", r"/api/output/(?P<key>[^/]+)", "output"),
        ("PATCH", r"/api/modules/(?P<module_id>[^/]+)", "patch_module"),
        ("POST", r"/api/modules/(?P<module_id>[^/]+)/run", "run_module"),
        ("PATCH", r"/api/printer", "patch_printer"),
        ("POST", r"/api/printer/test", "test_printer"),
        ("POST", r"/api/instant/preview", "instant_preview"),
        ("POST", r"/api/instant/print", "instant_print"),
        ("POST", r"/api/reader/fetch", "reader_fetch"),
        ("POST", r"/api/reader/print", "reader_print"),
        # A scanned QR selection code. GET on purpose: a phone's camera opens
        # links. The token is the credential; see gpta/selections.py.
        ("GET", re.escape(SCAN_PATH) + r"(?P<token>[A-Za-z0-9_-]{8,64})", "scan"),
        ("GET", r"/api/selections", "selections"),
        ("POST", r"/api/selections/test", "selection_test"),
        ("PATCH", r"/api/server", "patch_server"),
        ("GET", r"/api/queue", "queue"),
        ("GET", r"/api/queue/(?P<job_id>\d{1,12})\.png", "queue_png"),
        (
            "POST",
            r"/api/queue/(?P<action>pause|resume|clear|double|single)",
            "queue_control",
        ),
        (
            "POST",
            r"/api/queue/(?P<job_id>\d{1,12})/(?P<action>approve|cancel|retry|move)",
            "queue_action",
        ),
    )
]


SCAN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{heading} · GPTA</title>
<style>
body {{ font: 1.1rem/1.4 ui-monospace, Menlo, Consolas, monospace; margin: 0;
  min-height: 100vh; display: grid; place-content: center; text-align: center;
  padding: 1.5rem; background: #fff; color: #000; }}
h1 {{ font-size: 1.8rem; margin: 0 0 0.6rem; text-transform: uppercase; }}
p {{ margin: 0; overflow-wrap: anywhere; }}
</style>
</head>
<body data-state="{state}">
<main>
<h1>{heading}</h1>
<p>{detail}</p>
</main>
</body>
</html>
"""


def _hostname(value: str) -> str:
    """Return the lowercase hostname from a Host header or URL authority."""
    value = value.strip().lower()
    if value.startswith("["):  # [::1]:8787
        end = value.find("]")
        return value[1:end] if end != -1 else value
    if value.count(":") == 1:  # host:port (a bare IPv6 address has several)
        return value.split(":", 1)[0]
    return value


class GptaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        service: Service,
        web_root: Path,
        allowed_hosts: Iterable[str],
    ):
        self.service = service
        self.web_root = Path(web_root).resolve()
        self.allowed_hosts = {_hostname(h) for h in allowed_hosts}
        bind_host = _hostname(str(address[0]))
        if bind_host not in WILDCARD_HOSTS:
            self.allowed_hosts.add(bind_host)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = f"gpta/{__version__}"
    protocol_version = "HTTP/1.1"

    # -- verbs ---------------------------------------------------------
    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    # -- routing -------------------------------------------------------
    def _route(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        self._query = urllib.parse.parse_qs(parsed.query)
        allowed: list[str] = []
        for method, pattern, name in ROUTES:
            match = pattern.fullmatch(path)
            if match is None:
                continue
            if method != self.command:
                allowed.append(method)
                continue
            if method in STATE_CHANGING:
                # Every write needs a JSON body (empty is fine). Cross-site
                # HTML forms cannot send application/json.
                self._body_json = self._read_json()
            getattr(self, f"_h_{name}")(**match.groupdict())
            return
        if allowed:
            raise MethodNotAllowed(allowed)
        raise NotFound("not found")

    # -- handlers (one per entry in ROUTES) ----------------------------
    def _h_index(self) -> None:
        self._serve_static("index.html")

    def _h_privacy(self) -> None:
        # Google's OAuth branding form asks for a privacy policy address.
        self._serve_static("privacy.html")

    def _h_favicon(self) -> None:
        self._send_headers(204, "text/plain; charset=utf-8", 0)

    def _h_static(self, rel: str) -> None:
        self._serve_static(rel)

    def _h_state(self) -> None:
        self._json(200, self.server.service.snapshot())

    def _h_logs(self) -> None:
        raw = self._query.get("since", ["0"])[0]
        try:
            since = int(raw)
        except ValueError as exc:
            raise ValidationError("since must be an integer") from exc
        entries = self.server.service.logbus.since(since)
        next_seq = entries[-1]["seq"] if entries else since
        self._json(200, {"entries": entries, "next": next_seq})

    def _h_output(self, key: str) -> None:
        self._text(200, self.server.service.output_text(key))

    def _h_output_png(self, key: str) -> None:
        data = self.server.service.output_png(key)
        self._send_headers(200, "image/png", len(data))
        self.wfile.write(data)

    def _h_patch_module(self, module_id: str) -> None:
        module = self.server.service.update_module(module_id, self._body_json)
        self._json(200, {"module": module})

    def _h_run_module(self, module_id: str) -> None:
        self._json(200, self.server.service.run_module(module_id, trigger="manual"))

    def _h_patch_printer(self) -> None:
        printer = self.server.service.set_printer(self._body_json)
        self._json(200, {"printer": printer})

    def _h_test_printer(self) -> None:
        self._json(200, self.server.service.test_printer())

    def _h_instant_preview(self) -> None:
        self._json(200, self.server.service.instant_preview(self._body_json))

    def _h_instant_print(self) -> None:
        self._json(200, self.server.service.instant_print(self._body_json))

    def _h_reader_fetch(self) -> None:
        self._json(200, self.server.service.reader_fetch(self._body_json))

    def _h_reader_print(self) -> None:
        self._json(200, self.server.service.reader_print(self._body_json))

    def _h_scan(self, token: str) -> None:
        self._scan_page(self.server.service.scan(token))

    def _h_selections(self) -> None:
        self._json(200, self.server.service.selections_list())

    def _h_selection_test(self) -> None:
        self._json(200, self.server.service.selection_test(self._body_json))

    def _h_patch_server(self) -> None:
        self._json(200, {"server": self.server.service.set_server(self._body_json)})

    def _h_queue(self) -> None:
        self._json(200, self.server.service.queue())

    def _h_queue_png(self, job_id: str) -> None:
        data = self.server.service.queue_png(job_id)
        self._send_headers(200, "image/png", len(data))
        self.wfile.write(data)

    def _h_queue_control(self, action: str) -> None:
        service = self.server.service
        if action == "clear":
            self._json(200, service.queue_clear())
        elif action in ("double", "single"):
            self._json(200, service.queue_spacing(action == "double"))
        else:
            self._json(200, service.queue_pause(action == "pause"))

    def _h_queue_action(self, job_id: str, action: str) -> None:
        self._json(
            200, self.server.service.queue_action(job_id, action, self._body_json)
        )

    # -- dispatch ------------------------------------------------------
    def _dispatch(self) -> None:
        self._body: bytes | None = None
        self._body_json: dict[str, Any] = {}
        try:
            self._receive_body()
            self._check_host()
            if self.command in STATE_CHANGING:
                self._check_origin()
            self._route()
        except Forbidden as exc:
            log.warning(
                "403 %s %s (Host=%r Origin=%r): %s",
                self.command,
                self.path,
                self.headers.get("Host"),
                self.headers.get("Origin"),
                exc,
            )
            self._json(403, {"error": str(exc)})
        except NotFound as exc:
            self._json(404, {"error": str(exc)})
        except MethodNotAllowed as exc:
            self._json(
                405,
                {"error": "method not allowed"},
                {"Allow": ", ".join(sorted(set(exc.allowed)))},
            )
        except (ValidationError, InvalidRequest, ConfigError) as exc:
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
        except Exception:  # pragma: no cover - defensive
            log.exception("unhandled request error")
            self._json(500, {"error": "internal error; see the server log"})

    # -- responses -----------------------------------------------------
    def _send_headers(
        self,
        status: int,
        content_type: str,
        length: int,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _json(
        self, status: int, payload: Any, headers: dict[str, str] | None = None
    ) -> None:
        data = json.dumps(payload).encode("utf-8")
        self._send_headers(
            status, "application/json; charset=utf-8", len(data), headers
        )
        if self.command != "HEAD":
            self.wfile.write(data)

    def _text(self, status: int, text: str) -> None:
        data = text.encode("utf-8")
        self._send_headers(status, "text/plain; charset=utf-8", len(data))
        self.wfile.write(data)

    def _scan_page(self, result: ScanResult) -> None:
        """The small page a phone shows after scanning a selection code."""
        page = SCAN_PAGE.format(
            heading=html.escape(result.heading),
            detail=html.escape(result.detail),
            state=html.escape(result.state),
        ).encode("utf-8")
        self._send_headers(
            result.status,
            "text/html; charset=utf-8",
            len(page),
            {
                "Content-Security-Policy": "default-src 'none'; "
                "style-src 'unsafe-inline'",
                "Referrer-Policy": "no-referrer",
                "X-Robots-Tag": "noindex",
            },
        )
        self.wfile.write(page)

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

    # -- request guards ------------------------------------------------
    def _check_host(self) -> None:
        # Rejecting unknown Host headers blocks DNS rebinding: a hostile page
        # that rebinds its own name to 127.0.0.1 still sends its own name.
        host = self.headers.get("Host")
        if not host or _hostname(host) not in self._allowed_hosts():
            raise Forbidden("host not allowed; add it to server.allowed_hosts")

    def _allowed_hosts(self) -> set[str]:
        """The allowlist plus the host of ``server.public_url``, which can
        change while running: printed codes must keep working."""
        public = public_host(
            self.server.service.config.snapshot()["server"]["public_url"]
        )
        allowed = self.server.allowed_hosts
        return allowed | {public} if public else allowed

    def _check_origin(self) -> None:
        # Browsers send Origin on cross-origin and POST/PATCH requests.
        # Non-browser clients (curl, scripts) usually omit it.
        origin = self.headers.get("Origin")
        if origin is None:
            return
        netloc = urllib.parse.urlsplit(origin).netloc
        if not netloc or _hostname(netloc) not in self._allowed_hosts():
            raise Forbidden("cross-origin request rejected")

    # -- request body --------------------------------------------------
    def _receive_body(self) -> None:
        """Read the whole request body before routing.

        The connection is kept alive (HTTP/1.1), so any unread body bytes
        would be parsed as the start of the next request. A body that cannot
        be read safely closes the connection instead.
        """
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise ValidationError("chunked request bodies are not supported")
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length else 0
        except ValueError as exc:
            self.close_connection = True
            raise ValidationError("invalid Content-Length") from exc
        path = urllib.parse.urlsplit(self.path).path
        limit = MAX_LARGE_BODY if path in LARGE_BODY_PATHS else MAX_BODY
        if length < 0 or length > limit:
            self.close_connection = True
            raise ValidationError("request body too large")
        self._body = self.rfile.read(length) if length else b""

    def _read_json(self) -> dict[str, Any]:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ValidationError("Content-Type must be application/json")
        raw = self._body or b""
        if not raw.strip():
            return {}
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
    allowed_hosts: Iterable[str] | None = None,
) -> GptaHTTPServer:
    if allowed_hosts is None:
        allowed_hosts = service.config.snapshot()["server"]["allowed_hosts"]
    return GptaHTTPServer((host, port), service, web_root, allowed_hosts)
