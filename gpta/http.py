"""Small standard-library HTTP gateway used by modules.

Modules must use this client rather than urllib directly.  That keeps egress
in one place and lets tests inject a fake transport.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger("gpta.http")


class FetchError(Exception):
    """A remote request failed."""

    def __init__(self, message: str, *, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


class HttpClient:
    def __init__(self, timeout: float = 10.0, user_agent: str = "gpta/0.1"):
        self.timeout = timeout
        self.user_agent = user_agent

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        timeout: float | None = None,
        read_limit: int | None = None,
    ) -> bytes:
        hdrs = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.read() if read_limit is None else resp.read(read_limit)
        except urllib.error.HTTPError as exc:
            raise FetchError(
                f"HTTP {exc.code} for {url}", status=exc.code, url=url
            ) from exc
        except urllib.error.URLError as exc:
            raise FetchError(f"request failed for {url}: {exc.reason}", url=url) from exc
        except TimeoutError as exc:
            raise FetchError(f"request timed out for {url}", url=url) from exc

    def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> Any:
        data = None
        hdrs = dict(headers or {})
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        raw = self._request(url, method=method, headers=hdrs, data=data)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FetchError(f"invalid JSON from {url}", url=url) from exc

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        return self.request_json("GET", url, headers=headers)

    def get_text(
        self,
        url: str,
        *,
        max_bytes: int = 2_000_000,
        headers: dict[str, str] | None = None,
    ) -> str:
        raw = self._request(url, headers=headers, read_limit=max_bytes)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")
        if text.startswith("\ufeff"):
            text = text[1:]
        return text

    def post_form_json(
        self, url: str, data: dict[str, str], headers: dict[str, str] | None = None
    ) -> Any:
        body = urllib.parse.urlencode(data).encode("utf-8")
        hdrs = dict(headers or {})
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        raw = self._request(url, method="POST", headers=hdrs, data=body)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FetchError(f"invalid JSON from {url}", url=url) from exc
