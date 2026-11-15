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
- :mod:`gpta.selections`: ``ctx.selections`` and :class:`Choice`

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
    QrItem,
    Rule,
    Text,
)
from .cache import Cache
from .markdown import to_blocks as markdown_blocks
from .selections import Choice, SelectionError, Selections, SelectionStore
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
    "Choice",
    "Document",
    "Element",
    "FetchError",
    "Heading",
    "KeyValue",
    "Module",
    "ModuleContext",
    "ModuleStore",
    "QrCode",
    "QrItem",
    "Reading",
    "Rule",
    "SelectionError",
    "Selections",
    "Skip",
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


class Skip(Exception):  # noqa: N818 - control flow, not an error
    """Raised by ``fetch()`` when there is nothing new to print.

    The run counts as successful: the module's ``ctx.store`` writes are
    saved, the previous receipt stays in the console, and nothing is queued
    for the printer. The message is logged as the reason.
    """


def _memory_store() -> ModuleStore:
    return ModuleStore(Database(None), "test")


@dataclass(frozen=True)
class Reading:
    """A web page read by ``ctx.reader``: its ``blocks`` (a header, a heavy
    rule, the text) and what it is made of, for judging how well it prints.

    ``words`` counts the text; ``code_words`` of them are code listings.
    ``table_rows`` is the rows of tables, and ``media`` the pictures,
    videos, embeds and formulas that were left out (icons not counted).
    """

    blocks: tuple[Element, ...]
    words: int = 0
    code_words: int = 0
    table_rows: int = 0
    media: int = 0


class _NoReader:
    """``ctx.reader`` outside the host: there is no network to read from."""

    def read(self, url: str) -> Reading:
        raise FetchError("this context has no page reader")

    def read_html(self, html: str, url: str, title: str = "") -> Reading:
        raise FetchError("this context has no page reader")

    def article(self, url: str) -> list[Element]:
        return list(self.read(url).blocks)

    def html(self, html: str, url: str, title: str = "") -> list[Element]:
        return list(self.read_html(html, url, title).blocks)


def _default_paper_mm(content: Any) -> int:
    """Paper length on 80 mm paper with default settings (tests only; the
    host measures with the module's real settings)."""
    from .renderer import build_receipt, render_image  # needs Pillow

    return round(render_image(build_receipt("", content), 576).height / 8)


def _memory_selections() -> Selections:
    return Selections(SelectionStore(Database(None)), "test", "http://gpta.test")


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
    #: What started the run: ``"schedule"``, ``"manual"`` (the Run button
    #: or the API), or ``"select"`` in ``on_select()``. A module that skips
    #: unchanged runs should still print when asked by hand.
    trigger: str = "schedule"
    #: Makes QR selection lists (``ctx.selections.offer(...)``). The default
    #: is an in-memory one for tests, with links under ``http://gpta.test``.
    selections: Selections = field(default_factory=_memory_selections)
    #: Reader mode for web pages: ``ctx.reader.read(url)`` downloads a
    #: public page and returns a :class:`Reading`; ``read_html(html, url,
    #: title="")`` does the same for HTML the module already has.
    #: ``article()`` and ``html()`` return just the blocks. All raise
    #: :class:`FetchError`. The default has no network; tests pass a fake.
    reader: Any = field(default_factory=_NoReader)
    #: ``ctx.paper_mm(content)``: roughly how many millimetres of paper
    #: ``content`` (blocks or a :class:`Document`) would use if returned
    #: from ``on_select()`` now, with the current paper, size and layout.
    paper_mm: Callable[[Any], int] = _default_paper_mm

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

    def on_select(
        self, ctx: ModuleContext, payload: Any
    ) -> list[Element] | Document | list[Document]:
        """Called when a code from ``ctx.selections.offer()`` is scanned,
        with that choice's ``payload``. Returns what to print, like
        ``fetch()``; the host adds the header and footer but does not apply
        ``max_chars``. A list of :class:`Document`\\ s prints each as its own
        job, in order (an article, then its comments). Raise :class:`Skip`
        to print nothing."""
        raise NotImplementedError(f"{self.id} does not handle selections")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} id={self.id!r}>"
