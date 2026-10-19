#!/usr/bin/env python3
"""One-time Google OAuth helper for the Google Tasks module."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gpta.http import FetchError, HttpClient  # noqa: E402

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/tasks"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", default=os.environ.get("GOOGLE_CLIENT_ID"))
    parser.add_argument("--client-secret", default=os.environ.get("GOOGLE_CLIENT_SECRET"))
    parser.add_argument("--data-dir", default=os.environ.get("GPTA_DATA_DIR"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.client_id or not args.client_secret:
        print(
            "client id and secret required (--client-id/--client-secret or "
            "GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET)",
            file=sys.stderr,
        )
        return 2

    data_dir = Path(
        args.data_dir or os.environ.get("GPTA_DATA_DIR") or (Path.cwd() / "data")
    ).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    creds_path = data_dir / "google_credentials.json"

    state = secrets.token_urlsafe(24)
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if "error" in query:
                captured["error"] = query["error"][0]
            elif query.get("state", [""])[0] != state:
                captured["error"] = "state mismatch"
            elif "code" in query:
                captured["code"] = query["code"][0]
            else:
                captured["error"] = "missing authorization code"
            body = (
                "<html><body><h2>Google authorization received</h2>"
                "<p>You can close this window and return to the terminal.</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    httpd.timeout = 1
    port = httpd.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/"
    params = {
        "client_id": args.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print("Open this URL and approve access:")
    print(auth_url)
    if not args.no_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline and "code" not in captured and "error" not in captured:
        httpd.handle_request()
    httpd.server_close()

    if "error" in captured:
        print(f"authorization failed: {captured['error']}", file=sys.stderr)
        return 1
    if "code" not in captured:
        print("timed out waiting for authorization", file=sys.stderr)
        return 1

    try:
        token = HttpClient(timeout=30).post_form_json(
            TOKEN_URL,
            {
                "code": captured["code"],
                "client_id": args.client_id,
                "client_secret": args.client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    except FetchError as exc:
        print(f"token exchange failed: {exc}", file=sys.stderr)
        return 1
    refresh_token = token.get("refresh_token") if isinstance(token, dict) else None
    if not refresh_token:
        print(
            "no refresh token returned; remove prior app access and retry",
            file=sys.stderr,
        )
        return 1

    payload = {
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "refresh_token": refresh_token,
        "scope": SCOPE,
    }
    fd = os.open(creds_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    print(f"wrote credentials to {creds_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
