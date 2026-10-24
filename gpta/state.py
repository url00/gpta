"""Runtime state that survives a restart: ``data/state.json``.

Holds, per module, when it last ran (wall-clock epoch seconds), how that run
went, its failure count, and its latest receipt. Without it every restart
would run, and print, every enabled module straight away.

The file is a cache of runtime facts, not configuration: a missing, corrupt,
or partly invalid file is logged and ignored, never fatal.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("gpta.state")

STATE_VERSION = 1


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def load_modules(self) -> dict[str, dict[str, Any]]:
        """Return saved per-module state, or ``{}`` if there is none usable."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable %s: %s", self.path, exc)
            return {}
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            log.warning("ignoring %s: unknown format", self.path)
            return {}
        modules = raw.get("modules")
        if not isinstance(modules, dict):
            return {}
        return {
            module_id: entry
            for module_id, entry in modules.items()
            if isinstance(module_id, str) and isinstance(entry, dict)
        }

    def save_modules(self, modules: dict[str, dict[str, Any]]) -> None:
        """Atomically replace the saved state. Failures are logged, not raised."""
        payload = json.dumps(
            {"version": STATE_VERSION, "modules": modules}, indent=2, sort_keys=True
        )
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(
                    dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(payload + "\n")
                    os.replace(tmp_name, self.path)
                except BaseException:
                    Path(tmp_name).unlink(missing_ok=True)
                    raise
            except OSError as exc:
                log.error("could not save %s: %s", self.path, exc)
