"""Run state that survives a restart, in the ``module_state`` table.

Holds, per module, when it last ran (wall-clock epoch seconds), how that run
went, its failure count, and its latest receipt. Without it every restart
would run, and print, every enabled module straight away.

It is a record of runtime facts, not configuration: missing, unreadable, or
partly invalid data is logged and ignored, never fatal.

Before the database this lived in ``data/state.json``. That file is imported
once, when the table is still empty, and renamed ``state.json.migrated``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from .storage import Database

log = logging.getLogger("gpta.state")

LEGACY_VERSION = 1


class StateStore:
    def __init__(self, db: Database, legacy_path: Path | None = None):
        self.db = db
        self.legacy_path = Path(legacy_path) if legacy_path else None

    def load_modules(self) -> dict[str, dict[str, Any]]:
        """Return saved per-module state, or ``{}`` if there is none usable."""
        try:
            with self.db.transaction() as conn:
                rows = conn.execute(
                    "SELECT module_id, data FROM module_state"
                ).fetchall()
        except sqlite3.Error as exc:
            log.warning("ignoring unreadable run state: %s", exc)
            return {}
        if not rows:
            return self._import_legacy()
        modules: dict[str, dict[str, Any]] = {}
        for module_id, data in rows:
            try:
                entry = json.loads(data)
            except ValueError:
                entry = None
            if isinstance(entry, dict):
                modules[module_id] = entry
            else:
                log.warning("ignoring unreadable run state of %s", module_id)
        return modules

    def save_module(self, module_id: str, entry: dict[str, Any]) -> None:
        """Replace one module's saved state. Failures are logged, not raised."""
        try:
            data = json.dumps(entry, sort_keys=True)
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO module_state VALUES (?, ?, ?)"
                    " ON CONFLICT (module_id) DO UPDATE SET"
                    " data=excluded.data, updated_at=excluded.updated_at",
                    (module_id, data, time.time()),
                )
        except (sqlite3.Error, TypeError, ValueError) as exc:
            log.error("could not save run state of %s: %s", module_id, exc)

    # -- state.json from before the database ---------------------------
    def _import_legacy(self) -> dict[str, dict[str, Any]]:
        path = self.legacy_path
        if path is None:
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable %s: %s", path, exc)
            return {}
        if not isinstance(raw, dict) or raw.get("version") != LEGACY_VERSION:
            log.warning("ignoring %s: unknown format", path)
            return {}
        modules = raw.get("modules")
        if not isinstance(modules, dict):
            return {}
        modules = {
            module_id: entry
            for module_id, entry in modules.items()
            if isinstance(module_id, str) and isinstance(entry, dict)
        }
        for module_id, entry in modules.items():
            self.save_module(module_id, entry)
        try:
            path.replace(path.with_name(path.name + ".migrated"))
        except OSError as exc:
            log.warning("could not rename %s: %s", path, exc)
        log.info("imported %s into the database", path.name)
        return modules
