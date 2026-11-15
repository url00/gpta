#!/usr/bin/env python3
"""One-time Google sign-in for the Google Tasks module.

Runs the OAuth flow for a "Desktop app" client (loopback redirect + PKCE)
and writes a refresh token to data/google_credentials.json.

    python3 scripts/google_oauth.py --client-file ~/Downloads/client_secret_*.json

On a machine without a browser, add --paste: open the printed link on any
other device, approve, and paste back the address the browser ends up on
(it will fail to load; that is expected).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
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
LISTS_URL = "https://tasks.googleapis.com/tasks/v1/users/@me/lists?maxResults=100"
SCOPE = "https://www.googleapis.com/auth/tasks.readonly"
PASTE_PORT = 8765  # nothing listens here in --paste mode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--client-file",
        help="the OAuth client JSON downloaded from Google Cloud",
    )
    parser.add_argument("--client-id", default=os.environ.get("GOOGLE_CLIENT_ID"))
    parser.add_argument(
        "--client-secret", default=os.environ.get("GOOGLE_CLIENT_SECRET")
    )
    parser.add_argument("--data-dir", default=os.environ.get("GPTA_DATA_DIR"))
    parser.add_argument(
        "--paste",
        action="store_true",
        help="paste the redirect address by hand (no browser on this machine)",
    )
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    return parser


def read_client(args: argparse.Namespace) -> tuple[str, str]:
    """The client id and secret, from --client-file or the flags."""
    if args.client_file:
        data = json.loads(Path(args.client_file).expanduser().read_text("utf-8"))
        if "installed" not in data:
            raise ValueError(
                "not a Desktop-app client file (no 'installed' section); "
                "create an OAuth client of type 'Desktop app'"
            )
        return data["installed"]["client_id"], data["installed"]["client_secret"]
    if not args.client_id or not args.client_secret:
        raise ValueError(
            "pass --client-file, or --client-id and --client-secret "
            "(GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET)"
        )
    return args.client_id, args.client_secret


def parse_redirect(url: str, state: str) -> str:
    """The authorization code in a redirect address; raises ValueError."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url.strip()).query)
    if "error" in query:
        raise ValueError(f"Google returned: {query['error'][0]}")
    if query.get("state", [""])[0] != state:
        raise ValueError("state mismatch (use the link from this run)")
    if not query.get("code"):
        raise ValueError("no authorization code in that address")
    return query["code"][0]


def listen_for_redirect(httpd: HTTPServer, state: str, timeout: int) -> str:
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if urllib.parse.urlsplit(self.path).path != "/":
                self.send_error(404)  # e.g. /favicon.ico
                return
            try:
                captured["code"] = parse_redirect(self.path, state)
                message = "Google authorization received"
            except ValueError as exc:
                captured["error"] = str(exc)
                message = "Authorization failed"
            body = (
                f"<html><body><h2>{message}</h2>"
                "<p>You can close this window and return to the terminal.</p>"
                "</body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            pass

    httpd.RequestHandlerClass = Handler
    httpd.timeout = 1
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not captured:
        httpd.handle_request()
    if "error" in captured:
        raise ValueError(captured["error"])
    if "code" not in captured:
        raise ValueError("timed out waiting for authorization")
    return captured["code"]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client_id, client_secret = read_client(args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"cannot read the OAuth client: {exc}", file=sys.stderr)
        return 2

    data_dir = Path(args.data_dir or (ROOT / "data")).expanduser().resolve()
    creds_path = data_dir / "google_credentials.json"

    httpd = None
    port = PASTE_PORT
    if not args.paste:
        httpd = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = httpd.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/"

    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    auth_url = (
        AUTH_URL
        + "?"
        + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": SCOPE,
                "access_type": "offline",  # ask for a refresh token
                "prompt": "consent",  # ...even if access was granted before
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    print("Open this link and approve access:\n")
    print(auth_url + "\n")

    try:
        if httpd is None:
            print(
                "After approving, the browser goes to an address starting with\n"
                f"{redirect_uri} that will not load. Copy that whole address "
                "and paste it here."
            )
            code = parse_redirect(input("Address: "), state)
        else:
            if not args.no_browser:
                try:
                    webbrowser.open(auth_url)
                except Exception:
                    pass
            code = listen_for_redirect(httpd, state, args.timeout)
    except (ValueError, EOFError, KeyboardInterrupt) as exc:
        print(f"authorization failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if httpd is not None:
            httpd.server_close()

    http = HttpClient(timeout=30)
    try:
        token = http.post_form_json(
            TOKEN_URL,
            {
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    except FetchError as exc:
        print(f"token exchange failed: {exc}", file=sys.stderr)
        return 1
    refresh_token = token.get("refresh_token") if isinstance(token, dict) else None
    if not refresh_token:
        print("Google returned no refresh token; try again", file=sys.stderr)
        return 1

    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "scope": SCOPE,
    }
    fd = os.open(creds_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # the mode above only applies when creating the file
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    print(f"wrote credentials to {creds_path}")

    # Prove the token works, and show the list names the module can use.
    try:
        lists = http.get_json(
            LISTS_URL, headers={"Authorization": f"Bearer {token['access_token']}"}
        )
    except (FetchError, KeyError) as exc:
        print(
            f"but reading your task lists failed: {exc}\n"
            "Check that the Google Tasks API is enabled for the project.",
            file=sys.stderr,
        )
        return 1
    titles = [str(item.get("title")) for item in lists.get("items", [])]
    print("task lists: " + (", ".join(titles) or "(none)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
