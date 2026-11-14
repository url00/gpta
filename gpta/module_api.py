"""Public module ABI: everything a module imports comes from here.

Every module lives in ``modules/<id>/module.py`` and defines exactly one
subclass of :class:`Module` whose ``id`` matches its folder name. ``fetch()``
returns receipt blocks, or a :class:`Document` to also choose the layout.

This file is a facade. The pieces live in their own files and are
re-exported, so modules never import host internals:

- :mod:`gpta.blocks`: block kinds and :class:`Document`
- :mod:`gpta.markdown`: :func:`markdown_blocks`
- :mod:`gpta.cache`: ``ctx.cache``
- :mod:`gpta.storage`: ``ctx.store``

It must stay importable without Pillow, so it never imports the renderer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .blocks import (
    LAYOUTS,
    PRINT_SIZES,
    Bar,
    BarChart,
    Block,
    Checkbox,
    Document,
    Element,
    Heading,
    KeyValue,
    QrCode,
    Rule,
    Text,
)
from .cache import Cache
from .markdown import to_blocks as markdown_blocks
from .storage import Database, ModuleStore, StoreError

__all__ = [
    "CORE_LIMITS_SCHEMA",
    "LAYOUTS",
    "MAX_ITEMS_LIMIT",
    "PRINT_SIZES",
    "Bar",
    "BarChart",
    "Block",
    "Cache",
    "Checkbox",
    "Document",
    "Element",
    "FetchError",
    "Heading",
    "KeyValue",
    "Module",
    "ModuleContext",
    "ModuleStore",
    "QrCode",
    "Rule",
    "StoreError",
    "Text",
    "limits_schema",
    "markdown_blocks",
]


class FetchError(Exception):
    """Raised when source data cannot be fetched.

    ``gpta.http.FetchError`` subclasses this, so ``except FetchError`` in a
    module catches both its own errors and failures raised by ``ctx.http``.
    """


def _memory_store() -> ModuleStore:
    return ModuleStore(Database(None), "test")


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
    #: The module's saved state. The host always passes one; the default is
    #: an empty in-memory store, so tests can build a context without it.
    store: ModuleStore = field(default_factory=_memory_store)

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


def limits_schema(module: Module) -> dict[str, dict[str, Any]]:
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

    def fetch(self, ctx: ModuleContext) -> list[Element] | Document:  # pragma: no cover
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} id={self.id!r}>"
