"""Public module ABI.

Every module lives in ``modules/<id>/module.py`` and defines exactly one
subclass of :class:`Module` whose ``id`` matches its folder name.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("gpta.module_api")


class FetchError(Exception):
    """Raised when source data cannot be fetched.

    ``gpta.http.FetchError`` subclasses this, so ``except FetchError`` in a
    module catches both its own errors and failures raised by ``ctx.http``.
    """


class Cache:
    """Bounded on-disk cache shared by modules.

    Entries are addressed by a relative key such as ``"gutenberg/11.txt"``.
    Writes are atomic; reads touch the file so the size-based eviction pass
    approximates LRU. Expired entries and oversized caches are pruned on every
    write.
    """

    DEFAULT_MAX_BYTES = 50 * 1024 * 1024
    DEFAULT_MAX_AGE_SECONDS = 30 * 24 * 3600

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    ):
        self.root = Path(root)
        self.max_bytes = max(0, int(max_bytes))
        self.max_age_seconds = max(0, int(max_age_seconds))
        self._lock = threading.Lock()

    def path(self, key: str) -> Path:
        if not isinstance(key, str) or not key:
            raise ValueError("cache key must be a non-empty string")
        relative = Path(key)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError(f"unsafe cache key: {key!r}")
        return self.root / relative

    def read_text(self, key: str, encoding: str = "utf-8") -> str | None:
        path = self.path(key)
        try:
            text = path.read_text(encoding=encoding)
        except (FileNotFoundError, OSError):
            return None
        try:
            os.utime(path)
        except OSError:
            pass
        return text

    def write_text(self, key: str, text: str, encoding: str = "utf-8") -> None:
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(text, encoding=encoding)
            tmp.replace(path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self.evict()

    def evict(self) -> None:
        """Drop expired entries, then oldest entries until the size cap holds."""
        with self._lock:
            entries: list[tuple[float, int, Path]] = []
            total = 0
            now = time.time()
            if not self.root.is_dir():
                return
            for path in self.root.rglob("*"):
                if not path.is_file():
                    continue
                if path.name.endswith(".tmp"):
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if (
                    self.max_age_seconds
                    and now - stat.st_mtime > self.max_age_seconds
                ):
                    try:
                        path.unlink()
                        continue
                    except OSError:
                        pass
                entries.append((stat.st_mtime, stat.st_size, path))
                total += stat.st_size
            if total <= self.max_bytes:
                return
            for _mtime, size, path in sorted(entries):
                if total <= self.max_bytes:
                    break
                try:
                    path.unlink()
                    total -= size
                except OSError:
                    continue


@dataclass(frozen=True)
class Block:
    """A logical section of a receipt."""

    title: str
    lines: tuple[str, ...]
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModuleContext:
    """Everything a module may use during a fetch."""

    module_id: str
    limits: dict[str, Any]
    options: dict[str, Any]
    data_dir: Path
    now: datetime
    http: Any
    cache: Cache
    log: Callable[..., None]

    def emit(self, message: str, level: str = "info") -> None:
        self.log(message, level)


#: Limits every module has. The host truncates each receipt to ``max_chars``.
CORE_LIMITS_SCHEMA: dict[str, dict[str, Any]] = {
    "max_chars": {"type": "int", "label": "Max characters", "min": 100, "max": 10000},
}

#: A ready-made schema for the common "how many items to print" limit.
MAX_ITEMS_LIMIT: dict[str, Any] = {
    "type": "int",
    "label": "Max items",
    "min": 1,
    "max": 100,
}


def limits_schema(module: "Module") -> dict[str, dict[str, Any]]:
    """The full limits schema for ``module``: core limits plus its own."""
    return {**CORE_LIMITS_SCHEMA, **module.limits_schema}


class Module:
    """Base class for all data sources/API gateway modules."""

    id: str = ""
    name: str = ""
    description: str = ""

    defaults: dict[str, Any] = {
        "enabled": False,
        "interval_seconds": 3600,
        "limits": {"max_chars": 1000},
        "options": {},
    }

    #: Limits beyond the core ``max_chars`` (see :data:`CORE_LIMITS_SCHEMA`).
    #: Declare only limits the module actually applies, e.g.
    #: ``{"max_items": MAX_ITEMS_LIMIT}``.
    limits_schema: dict[str, dict[str, Any]] = {}

    options_schema: dict[str, dict[str, Any]] = {}

    def fetch(self, ctx: ModuleContext) -> list[Block]:  # pragma: no cover - abstract
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} id={self.id!r}>"
