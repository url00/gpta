"""QR selection lists: printed items a phone can pick by scanning a code.

A module offers a list of :class:`Choice`\\ s through ``ctx.selections``; each
becomes a :class:`~gpta.blocks.QrItem` whose code holds a link back to this
server, ``<server.public_url>/s/<token>``. Opening the link hands the
choice's ``payload`` to the module's ``on_select()``, and what that returns
is printed (see :meth:`gpta.runner.ModuleRunner.select`).

The token is the whole credential: 96 random bits, looked up in the
``selections`` table of ``data/gpta.db``. Codes work until they expire and
may be scanned again; a repeat within :data:`DEBOUNCE_SECONDS` is ignored,
so a phone that pre-fetches the link and then opens it prints once.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .blocks import QR_SIDES, QR_SIZES, Element, QrItem, Rule
from .storage import Database

#: The path scanned links live under: ``/s/<token>``.
SCAN_PATH = "/s/"
#: Random bytes per token (16 URL-safe characters). Short links keep the
#: codes small; 96 bits cannot be guessed.
TOKEN_BYTES = 12
#: A second scan of the same code this soon after the first is ignored.
DEBOUNCE_SECONDS = 30
#: How long a list's codes work unless the module says otherwise (7 days).
DEFAULT_EXPIRES_HOURS = 168
MAX_EXPIRES_HOURS = 2160  # 90 days
#: Bounds on one list and on what one module may have outstanding.
MAX_CHOICES = 50
MAX_TITLE_CHARS = 300
MAX_PAYLOAD_BYTES = 4000
MAX_PER_MODULE = 500

# Outcomes of :meth:`SelectionStore.claim`.
CLAIMED = "claimed"
REPEAT = "repeat"
EXPIRED = "expired"
UNKNOWN = "unknown"


class SelectionError(Exception):
    """A selection list could not be made (bad arguments, no public URL)."""


@dataclass(frozen=True)
class Choice:
    """One selectable item: ``title`` prints beside the code, ``note`` in
    small text under it, and ``payload`` (anything JSON can hold) is what
    ``on_select()`` receives when the code is scanned."""

    title: str
    payload: Any = None
    note: str = ""


class SelectionStore:
    """The ``selections`` table: every live code, for every module."""

    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self._db = db
        self._clock = clock

    def add(
        self, module_id: str, entries: list[tuple[str, str]], ttl_seconds: float
    ) -> list[str]:
        """Save ``(title, payload JSON)`` entries, good for ``ttl_seconds``,
        and return their tokens. Expired codes are dropped, and the module's
        oldest beyond :data:`MAX_PER_MODULE`."""
        now = self._clock()
        expires_at = now + ttl_seconds
        tokens = [secrets.token_urlsafe(TOKEN_BYTES) for _entry in entries]
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM selections WHERE expires_at <= ?", (now,))
            conn.executemany(
                "INSERT INTO selections"
                " (token, module_id, title, payload, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (token, module_id, title, payload, now, expires_at)
                    for token, (title, payload) in zip(tokens, entries, strict=True)
                ],
            )
            conn.execute(
                "DELETE FROM selections WHERE module_id=? AND token NOT IN ("
                " SELECT token FROM selections WHERE module_id=?"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                (module_id, module_id, MAX_PER_MODULE),
            )
        return tokens

    def claim(self, token: str) -> tuple[str, dict[str, Any] | None]:
        """Record a scan of ``token``. Returns an outcome and the selection:
        :data:`CLAIMED` (act on it), :data:`REPEAT` (scanned moments ago),
        :data:`EXPIRED` or :data:`UNKNOWN`."""
        now = self._clock()
        with self._db.transaction() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM selections WHERE token=?", (token,)
            ).fetchone()
            if row is None:
                return UNKNOWN, None
            selection = _selection(row)
            if selection["expires_at"] <= now:
                return EXPIRED, selection
            if now < row[_DEBOUNCE]:
                return REPEAT, selection
            conn.execute(
                "UPDATE selections SET use_count=use_count+1, last_used_at=?,"
                " debounce_until=? WHERE token=?",
                (now, now + DEBOUNCE_SECONDS, token),
            )
        selection["use_count"] += 1
        selection["last_used_at"] = now
        return CLAIMED, selection

    def finish(self, token: str, result: str, *, ok: bool) -> None:
        """Record how a claimed scan ended. A failed one may be retried at
        once, so its debounce is lifted."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE selections SET last_result=?,"
                " debounce_until=CASE WHEN ? THEN debounce_until ELSE 0 END"
                " WHERE token=?",
                (result[:300], 1 if ok else 0, token),
            )

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """The newest codes, newest first, expired ones included until the
        next list is made."""
        with self._db.transaction() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM selections"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [_selection(row) for row in rows]


_COLUMNS = (
    "token, module_id, title, payload, created_at, expires_at, use_count,"
    " last_used_at, last_result, debounce_until"
)
_DEBOUNCE = 9


def _selection(row: tuple) -> dict[str, Any]:
    try:
        payload = json.loads(row[3])
    except ValueError:
        payload = None
    return {
        "token": row[0],
        "module": row[1],
        "title": row[2],
        "payload": payload,
        "created_at": row[4],
        "expires_at": row[5],
        "use_count": row[6],
        "last_used_at": row[7],
        "last_result": row[8],
    }


def _choice(item: Any) -> Choice:
    if isinstance(item, Choice):
        return item
    if isinstance(item, (tuple, list)) and 1 <= len(item) <= 3:
        return Choice(*item)
    raise SelectionError("each choice must be a Choice or a (title, payload) pair")


class Selections:
    """``ctx.selections``: makes QR selection lists for one module."""

    def __init__(self, store: SelectionStore, module_id: str, public_url: str):
        self._store = store
        self._module_id = module_id
        self._public_url = public_url.rstrip("/")

    @property
    def available(self) -> bool:
        """Whether codes can be made: ``server.public_url`` is set."""
        return bool(self._public_url)

    def offer(
        self,
        choices: Iterable[Choice | tuple],
        *,
        expires_hours: int = DEFAULT_EXPIRES_HOURS,
        numbered: bool = True,
        side: str = "right",
        qr: str = "medium",
        rules: bool = True,
    ) -> list[Element]:
        """Blocks for a list of ``choices``, one code beside each.

        ``choices`` are :class:`Choice`\\ s or ``(title, payload[, note])``
        tuples. The codes work for ``expires_hours``. ``numbered`` prints
        "1.", "2." before the titles, ``side`` (``left`` or ``right``) and
        ``qr`` (``small``, ``medium``, ``large``) place and size the codes,
        and ``rules`` draws a line between items.

        Raises :class:`SelectionError` when ``server.public_url`` is not
        set, since the codes would lead nowhere.
        """
        if not self._public_url:
            raise SelectionError(
                "QR selection lists need the server's public URL;"
                " set it on the Settings tab (server.public_url)"
            )
        items = [_choice(item) for item in choices]
        if len(items) > MAX_CHOICES:
            raise SelectionError(f"a list may offer at most {MAX_CHOICES} choices")
        if (
            type(expires_hours) is not int
            or not 1 <= expires_hours <= MAX_EXPIRES_HOURS
        ):
            raise SelectionError(
                f"expires_hours must be between 1 and {MAX_EXPIRES_HOURS}"
            )
        if side not in QR_SIDES:
            raise SelectionError(f"side must be one of {', '.join(QR_SIDES)}")
        if qr not in QR_SIZES:
            raise SelectionError(f"qr must be one of {', '.join(QR_SIZES)}")
        entries = []
        for item in items:
            if not isinstance(item.title, str) or not item.title.strip():
                raise SelectionError("a choice needs a title")
            if not isinstance(item.note, str):
                raise SelectionError("a choice's note must be a string")
            try:
                payload = json.dumps(
                    item.payload, allow_nan=False, separators=(",", ":")
                )
            except (TypeError, ValueError) as exc:
                raise SelectionError(f"a choice's payload is not JSON: {exc}") from exc
            if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                raise SelectionError(
                    f"a choice's payload is larger than {MAX_PAYLOAD_BYTES:,} bytes"
                )
            entries.append((item.title.strip()[:MAX_TITLE_CHARS], payload))
        if not entries:
            return []
        tokens = self._store.add(self._module_id, entries, expires_hours * 3600)
        blocks: list[Element] = []
        for number, (item, token) in enumerate(zip(items, tokens, strict=True), 1):
            if rules and blocks:
                blocks.append(Rule())
            blocks.append(
                QrItem(
                    self._public_url + SCAN_PATH + token,
                    item.title.strip(),
                    item.note,
                    f"{number}." if numbered else "",
                    side,
                    qr,
                )
            )
        return blocks
