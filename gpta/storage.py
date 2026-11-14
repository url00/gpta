"""Durable storage: one SQLite file, ``data/gpta.db``. Standard library only.

Three things live in it:

- the host's per-module run state (:class:`gpta.state.StateStore`),
- each module's own key/value store (:class:`ModuleStore`, ``ctx.store``), and
- the print queue and its history (:mod:`gpta.printing.jobs`).

Settings stay in ``config.json`` (people edit it by hand) and downloads stay
in the file cache (large, and safe to evict); see docs/decisions.md.

The database holds runtime facts, not configuration, so it is never fatal: a
file that cannot be opened is moved aside and a new one started, and if that
fails too the server runs on an in-memory database until the next restart.

New tables are added by appending a script to :data:`MIGRATIONS`; the
position in the list is the schema version (``PRAGMA user_version``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger("gpta.storage")

#: Schema migrations, applied in order. Never edit an entry that has shipped;
#: append a new one.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE module_state (
        module_id  TEXT PRIMARY KEY,
        data       TEXT NOT NULL,
        updated_at REAL NOT NULL
    ) WITHOUT ROWID;
    CREATE TABLE module_store (
        module_id  TEXT NOT NULL,
        key        TEXT NOT NULL,
        value      TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (module_id, key)
    ) WITHOUT ROWID;
    """,
    """
    CREATE TABLE print_jobs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        source      TEXT NOT NULL,
        kind        TEXT NOT NULL,
        state       TEXT NOT NULL,
        position    REAL NOT NULL,
        approved    INTEGER NOT NULL DEFAULT 0,
        width_px    INTEGER NOT NULL,
        height_px   INTEGER NOT NULL,
        image       BLOB,
        error       TEXT,
        created_at  REAL NOT NULL,
        started_at  REAL,
        finished_at REAL
    );
    CREATE INDEX print_jobs_state ON print_jobs (state, position);
    CREATE TABLE host_settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID;
    """,
)

#: Bounds on what one module may keep in its store.
MAX_KEY_CHARS = 200
MAX_VALUE_BYTES = 1_000_000
MAX_KEYS_PER_MODULE = 10_000


class StoreError(Exception):
    """A module store call was invalid, over a bound, or could not be saved."""


class Database:
    """The SQLite file, shared by every thread through one connection.

    All access goes through :meth:`transaction`, which holds a lock, so
    statements from different threads never interleave. ``path=None`` gives
    an in-memory database (tests, and the fallback when the disk fails).
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._conn = self._open()

    # -- opening -------------------------------------------------------
    def _open(self) -> sqlite3.Connection:
        if self.path is None:
            return self._connect(":memory:")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return self._connect(str(self.path))
        except (sqlite3.Error, OSError) as exc:
            log.warning("cannot open %s: %s", self.path, exc)
        try:
            aside = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}")
            self.path.replace(aside)
            for suffix in ("-wal", "-shm"):
                self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)
            log.warning("moved it to %s and started a new database", aside.name)
            return self._connect(str(self.path))
        except (sqlite3.Error, OSError) as exc:
            log.error(
                "cannot create %s (%s); state will not survive a restart",
                self.path,
                exc,
            )
            return self._connect(":memory:")

    @staticmethod
    def _connect(target: str) -> sqlite3.Connection:
        # Autocommit mode: transactions are opened explicitly below.
        conn = sqlite3.connect(
            target, timeout=10, isolation_level=None, check_same_thread=False
        )
        try:
            if target != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                raise sqlite3.DatabaseError(
                    f"schema version {version} is newer than this GPTA"
                )
            for number in range(version, len(MIGRATIONS)):
                conn.executescript(
                    f"BEGIN;{MIGRATIONS[number]};PRAGMA user_version={number + 1};"
                    "COMMIT;"
                )
        except BaseException:
            conn.close()
            raise
        return conn

    # -- access --------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """The connection inside one write transaction: committed when the
        block ends, rolled back if it raises."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __del__(self) -> None:
        try:
            self._conn.close()
        except Exception:  # never opened, or the interpreter is exiting
            pass


_DELETED = object()


def _check_key(key: Any) -> str:
    if not isinstance(key, str) or not key:
        raise StoreError("store key must be a non-empty string")
    if len(key) > MAX_KEY_CHARS:
        raise StoreError(f"store key is longer than {MAX_KEY_CHARS} characters")
    return key


class ModuleStore:
    """One module's key/value store for one run: ``ctx.store``.

    Keys are strings; values are anything JSON can hold. Reads see the
    module's saved data plus this run's own writes. Writes are kept in memory
    and saved together by :meth:`commit`, which the host calls when the run
    has produced a receipt; a run that fails or times out saves nothing, so
    state and output cannot drift apart ("mark as printed" only sticks if the
    receipt was made). After the run the store is closed and writes raise.
    """

    def __init__(self, db: Database, module_id: str):
        self._db = db
        self._module_id = module_id
        self._pending: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._closed = False

    # -- reads ---------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        """The value saved under ``key``, or ``default``."""
        _check_key(key)
        with self._lock:
            pending = self._pending.get(key)
        if pending is _DELETED:
            return default
        if pending is not None:
            return json.loads(pending)
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT value FROM module_store WHERE module_id=? AND key=?",
                (self._module_id, key),
            ).fetchone()
        return default if row is None else self._decode(key, row[0], default)

    def __contains__(self, key: str) -> bool:
        missing = object()
        return self.get(key, missing) is not missing

    def keys(self, prefix: str = "") -> list[str]:
        """Sorted keys, optionally only those starting with ``prefix``."""
        return [key for key, _value in self.items(prefix)]

    def items(self, prefix: str = "") -> list[tuple[str, Any]]:
        """Sorted ``(key, value)`` pairs, optionally filtered by ``prefix``."""
        if not isinstance(prefix, str):
            raise StoreError("store prefix must be a string")
        with self._db.transaction() as conn:
            rows = conn.execute(
                "SELECT key, value FROM module_store"
                " WHERE module_id=? AND substr(key, 1, ?)=?",
                (self._module_id, len(prefix), prefix),
            ).fetchall()
        missing = object()
        found = {key: self._decode(key, raw, missing) for key, raw in rows}
        with self._lock:
            pending = dict(self._pending)
        for key, raw in pending.items():
            if not key.startswith(prefix):
                continue
            found[key] = missing if raw is _DELETED else json.loads(raw)
        return sorted((k, v) for k, v in found.items() if v is not missing)

    def _decode(self, key: str, raw: str, default: Any) -> Any:
        try:
            return json.loads(raw)
        except ValueError:
            log.warning("ignoring unreadable %s/%s", self._module_id, key)
            return default

    # -- writes --------------------------------------------------------
    def set(self, key: str, value: Any) -> None:
        """Save ``value`` (JSON-serialisable) under ``key``."""
        _check_key(key)
        try:
            raw = json.dumps(value, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise StoreError(f"store value for {key!r} is not JSON: {exc}") from exc
        if len(raw.encode("utf-8")) > MAX_VALUE_BYTES:
            raise StoreError(
                f"store value for {key!r} is larger than {MAX_VALUE_BYTES:,} bytes"
            )
        self._write(key, raw)

    def delete(self, key: str) -> None:
        """Remove ``key``; a missing key is not an error."""
        self._write(_check_key(key), _DELETED)

    def _write(self, key: str, raw: Any) -> None:
        with self._lock:
            if self._closed:
                raise StoreError("the run has ended; its store is closed")
            self._pending[key] = raw

    # -- host side -----------------------------------------------------
    def commit(self) -> None:
        """Save this run's writes in one transaction and close the store.

        Raises :class:`StoreError` (saving nothing) if they cannot be saved
        or would put the module over :data:`MAX_KEYS_PER_MODULE`.
        """
        with self._lock:
            pending, self._pending = self._pending, {}
            self._closed = True
        if not pending:
            return
        now = time.time()
        try:
            with self._db.transaction() as conn:
                for key, raw in pending.items():
                    if raw is _DELETED:
                        conn.execute(
                            "DELETE FROM module_store WHERE module_id=? AND key=?",
                            (self._module_id, key),
                        )
                    else:
                        conn.execute(
                            "INSERT INTO module_store VALUES (?, ?, ?, ?)"
                            " ON CONFLICT (module_id, key) DO UPDATE SET"
                            " value=excluded.value, updated_at=excluded.updated_at",
                            (self._module_id, key, raw, now),
                        )
                count = conn.execute(
                    "SELECT count(*) FROM module_store WHERE module_id=?",
                    (self._module_id,),
                ).fetchone()[0]
                if count > MAX_KEYS_PER_MODULE:
                    raise StoreError(
                        f"store would hold {count:,} keys; "
                        f"the limit is {MAX_KEYS_PER_MODULE:,}"
                    )
        except sqlite3.Error as exc:
            raise StoreError(f"could not save module state: {exc}") from exc

    def discard(self) -> None:
        """Drop this run's writes and close the store."""
        with self._lock:
            self._pending = {}
            self._closed = True
