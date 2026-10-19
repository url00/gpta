"""Public module ABI.

Every module lives in ``modules/<id>/module.py`` and defines exactly one
subclass of :class:`Module` whose ``id`` matches its folder name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("gpta.module_api")


class FetchError(Exception):
    """Raised by a module when source data cannot be fetched."""


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
    log: Callable[..., None]

    def emit(self, message: str, level: str = "info") -> None:
        self.log(message, level)


class Module:
    """Base class for all data sources/API gateway modules."""

    id: str = ""
    name: str = ""
    description: str = ""

    defaults: dict[str, Any] = {
        "enabled": False,
        "interval_seconds": 3600,
        "limits": {"max_items": 5, "max_chars": 1000},
        "options": {},
    }

    limits_schema: dict[str, dict[str, Any]] = {
        "max_items": {"type": "int", "label": "Max items", "min": 1, "max": 100},
        "max_chars": {
            "type": "int",
            "label": "Max characters",
            "min": 100,
            "max": 10000,
        },
    }

    options_schema: dict[str, dict[str, Any]] = {}

    def fetch(self, ctx: ModuleContext) -> list[Block]:  # pragma: no cover - abstract
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} id={self.id!r}>"
