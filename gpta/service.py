"""Application facade used by the HTTP layer.

Wires together the module runner, scheduler, output store, and printer, and
builds the ``/api/state`` snapshot.

Lock order: runner → config. The printer and output store take their own
locks only around their own state and never call out while holding them.
"""

from __future__ import annotations

import base64
import secrets
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__, reader
from .blocks import Rule, Text, size_of
from .config import PAPERS, ConfigStore
from .errors import InvalidRequest, NotFound
from .http import HttpClient
from .logbus import LogBus
from .module_api import Module
from .outputs import OutputStore
from .printing import Printer, PrinterWriter
from .registry import describe
from .renderer import (
    LAYOUTS,
    PRINT_SIZES,
    Receipt,
    build_receipt,
    print_scale,
    render_image,
    render_text,
    test_receipt,
    to_png,
    truncate,
)
from .runner import ModuleRunner, module_log
from .scheduler import Scheduler
from .state import StateStore

#: Longest text the Instant tab may print in one job.
INSTANT_MAX_CHARS = 5000


def print_style(body: dict, elements=()) -> dict[str, Any]:
    """Keyword arguments for :func:`render_image` from a request's ``size``
    and ``layout``. In two columns, everything up to the first heavy rule
    (a web page's header) stays full width."""
    layout = body.get("layout", "single")
    if layout not in LAYOUTS:
        raise InvalidRequest(f"layout must be one of {', '.join(LAYOUTS)}")
    lead = next(
        (i + 1 for i, e in enumerate(elements) if isinstance(e, Rule) and e.heavy), 0
    )
    return {"scale": print_size(body), "layout": layout, "lead": lead}


def print_size(body: dict) -> float:
    """The font scale for ``body["size"]``, one of :data:`PRINT_SIZES`."""
    size = body.get("size", "body")
    if size not in PRINT_SIZES:
        raise InvalidRequest(f"size must be one of {', '.join(PRINT_SIZES)}")
    return print_scale(size)


def instant_receipt(body: Any) -> Receipt:
    """A one-off receipt of pasted ``text``: no header or footer."""
    if not isinstance(body, dict):
        raise InvalidRequest("body must be an object")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise InvalidRequest("text must be a non-empty string")
    if len(text) > INSTANT_MAX_CHARS:
        raise InvalidRequest(f"text is longer than {INSTANT_MAX_CHARS} characters")
    return Receipt(title="", elements=(Text(text.strip("\n")),), stamp="")


#: Longest article text the reader keeps; the rest is cut with a marker.
#: Whole pages print, so this only guards against runaway pages (about
#: 15 m of paper at normal size).
READER_MAX_CHARS = 150_000
#: Fetched pages kept for printing, newest last.
READER_CACHE_SIZE = 8
#: Printer dots per mm (203 dpi), for paper length estimates.
DOTS_PER_MM = 8


def _png_url(image) -> str:
    return "data:image/png;base64," + base64.b64encode(to_png(image)).decode("ascii")


class Service:
    def __init__(
        self,
        config: ConfigStore,
        registry: dict[str, Module],
        logbus: LogBus,
        http: HttpClient | None = None,
        clock=time.time,
        monotonic=time.monotonic,
        modules_dir: Path | None = None,
        fetch_timeout: float | None = None,
        registry_errors: dict[str, str] | None = None,
        printer_writer: PrinterWriter | None = None,
    ):
        self.config = config
        self.registry = registry
        self.logbus = logbus
        self.clock = clock
        # Replaced in tests so reader-mode fetches need no network.
        self.reader_fetcher = reader.fetch_page
        # Fetched pages by id, so a print matches its preview exactly
        # without sending the whole article back from the browser.
        self._reader_pages: OrderedDict[str, list] = OrderedDict()
        self._reader_lock = threading.Lock()
        self.monotonic = monotonic
        self.modules_dir = Path(modules_dir) if modules_dir else None
        self.registry_errors: dict[str, str] = dict(registry_errors or {})
        self.bound_host: str | None = None
        self.bound_port: int | None = None
        if fetch_timeout is None:
            fetch_timeout = config.snapshot()["server"]["run_timeout_seconds"]

        self.outputs = OutputStore()
        self.printer = Printer(
            config, writer=printer_writer, clock=clock, monotonic=monotonic
        )
        self.runner = ModuleRunner(
            config,
            registry,
            self.outputs,
            self.printer,
            http or HttpClient(),
            fetch_timeout=fetch_timeout,
            state=StateStore(config.data_dir / "state.json"),
            clock=clock,
            monotonic=monotonic,
        )
        self.scheduler = Scheduler(
            config, self.runner, clock=clock, monotonic=monotonic
        )
        self._descriptions = {
            module_id: describe(module) for module_id, module in registry.items()
        }
        self._started_mono = monotonic()

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        self.scheduler.start()

    def stop(self) -> None:
        self.scheduler.stop()
        self.printer.stop()

    def tick(self) -> None:
        self.scheduler.tick()

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        return self.scheduler.wait_for_idle(timeout)

    # -- actions -------------------------------------------------------
    def run_module(self, module_id: str, trigger: str = "manual") -> dict[str, Any]:
        result = self.runner.run(module_id, trigger)
        runtime = result.pop("runtime")
        result["module"] = self._module_snapshot(module_id, runtime=runtime)
        return result

    def update_module(self, module_id: str, patch: Any) -> dict[str, Any]:
        if module_id not in self.registry:
            raise NotFound(f"unknown module: {module_id}")
        self.config.update_module(module_id, patch)
        module_log("configuration updated", "info", module_id)
        return self._module_snapshot(module_id)

    def set_printer(self, patch: Any) -> dict[str, Any]:
        self.config.update_printer(patch)
        if isinstance(patch, dict) and patch.get("enabled") is True:
            self.printer.reset_failures()
        module_log("printer configuration updated")
        return self._printer_snapshot()

    def test_printer(self) -> dict[str, Any]:
        paper = PAPERS[self.config.snapshot()["printer"]["paper"]]
        receipt = test_receipt(datetime.fromtimestamp(self.clock(), UTC))
        self.printer.submit(
            render_image(receipt, paper["width_px"]), "test", manual=True
        )
        return {"ok": True, "lines": render_text(receipt, paper["columns"])}

    def instant_preview(self, body: Any) -> dict[str, Any]:
        """The image an instant print would produce, as a PNG data URL."""
        receipt = instant_receipt(body)
        image = render_image(receipt, self._width_px(), **print_style(body))
        return {"png": _png_url(image), "height_px": image.height}

    def instant_print(self, body: Any) -> dict[str, Any]:
        receipt = instant_receipt(body)
        image = render_image(receipt, self._width_px(), **print_style(body))
        self.printer.submit(image, "instant", manual=True)
        module_log(f"instant print: {len(receipt.elements[0].text)} characters")
        return {"ok": True}

    def reader_fetch(self, body: Any) -> dict[str, Any]:
        """Fetch a web page and lay out all of it, reader-mode style.

        Returns a preview with the character count and paper length, and an
        ``id`` for :meth:`reader_print`, so what prints is what was previewed.
        """
        if not isinstance(body, dict):
            raise InvalidRequest("body must be an object")
        print_style(body)  # validate before fetching
        try:
            page = self.reader_fetcher(body.get("url"))
            article = reader.extract(page.html, page.url)
        except reader.ReaderError as exc:
            raise InvalidRequest(str(exc)) from exc
        total = sum(size_of(element) for element in article.body)
        elements = article.elements(truncate(article.body, READER_MAX_CHARS))
        receipt = self._reader_receipt(elements)
        module_log(f"reader fetched {page.url}: {total} characters")
        image = render_image(
            receipt, self._width_px(), **print_style(body, receipt.elements)
        )
        page_id = secrets.token_hex(8)
        with self._reader_lock:
            self._reader_pages[page_id] = elements
            while len(self._reader_pages) > READER_CACHE_SIZE:
                self._reader_pages.popitem(last=False)
        return {
            "id": page_id,
            "title": article.title,
            "url": page.url,
            "chars": min(total, READER_MAX_CHARS),
            "total": total,
            "png": _png_url(image),
            "height_px": image.height,
            "paper_mm": round(image.height / DOTS_PER_MM),
        }

    def reader_print(self, body: Any) -> dict[str, Any]:
        """Print a page from :meth:`reader_fetch` by its ``id``."""
        page_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(page_id, str) or not page_id:
            raise InvalidRequest("nothing to print; fetch a page first")
        print_style(body)  # validate before looking the page up
        with self._reader_lock:
            elements = self._reader_pages.get(page_id)
        if elements is None:
            raise InvalidRequest("that page is no longer loaded; fetch it again")
        receipt = self._reader_receipt(elements)
        style = print_style(body, receipt.elements)
        image = render_image(receipt, self._width_px(), **style)
        self.printer.submit(image, "reader", manual=True)
        module_log(f"reader print: {image.height // DOTS_PER_MM} mm")
        return {"ok": True}

    def _reader_receipt(self, elements: list) -> Receipt:
        now = datetime.fromtimestamp(self.clock(), UTC)
        return build_receipt("", elements, now=now)

    def _width_px(self) -> int:
        return _paper(self.config.snapshot())["width_px"]

    # -- snapshots -----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        config = self.config.snapshot()
        return {
            "server": {
                "version": __version__,
                "host": self.bound_host or config["server"]["host"],
                "port": self.bound_port or config["server"]["port"],
                "uptime_seconds": max(0, int(self.monotonic() - self._started_mono)),
                **self.scheduler.status(),
                "config_path": str(self.config.path),
                "modules_dir": str(self.modules_dir) if self.modules_dir else "",
            },
            "printer": self._printer_snapshot(config["printer"]),
            "modules": [
                self._module_snapshot(module_id, config)
                for module_id in sorted(self.registry)
            ],
            "total": self._total(config),
            "registry_errors": [
                {"module": name, "error": message}
                for name, message in sorted(self.registry_errors.items())
            ],
        }

    def output_text(self, key: str) -> str:
        config = self.config.snapshot()
        if key == "total":
            return self._total(config)["text"]
        if key not in self.registry:
            raise NotFound(f"unknown output: {key}")
        output = self.outputs.get(key)
        return output.text(_paper(config)["columns"]) if output else ""

    def output_png(self, key: str) -> bytes:
        """The image that would be printed for ``key``, as PNG."""
        config = self.config.snapshot()
        width_px = _paper(config)["width_px"]
        if key == "total":
            image = self.outputs.total_image(self._enabled(config), width_px)
        elif key in self.registry:
            image = self.outputs.image(key, width_px)
        else:
            raise NotFound(f"unknown output: {key}")
        if image is None:
            raise NotFound(f"no output yet: {key}")
        return to_png(image)

    def _enabled(self, config: dict[str, Any]) -> list[str]:
        return [
            module_id
            for module_id in sorted(self.registry)
            if config["modules"][module_id]["enabled"]
        ]

    def _total(self, config: dict[str, Any]) -> dict[str, Any]:
        return self.outputs.total(self._enabled(config), _paper(config)["columns"])

    def _module_snapshot(
        self,
        module_id: str,
        config: dict[str, Any] | None = None,
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if config is None:
            config = self.config.snapshot()
        cfg = config["modules"][module_id]
        if runtime is None:
            runtime = self.runner.runtime_snapshot(module_id, cfg)
        output = self.outputs.get(module_id)
        lines = output.lines(_paper(config)["columns"]) if output else []
        return {
            **self._descriptions[module_id],
            "enabled": cfg["enabled"],
            "interval_seconds": cfg["interval_seconds"],
            "limits": dict(cfg["limits"]),
            "options": dict(cfg["options"]),
            "runtime": runtime,
            "output": {
                "lines": lines,
                "text": "\n".join(lines) + "\n" if lines else "",
                "generated_at": output.generated_at if output else None,
            },
        }

    def _printer_snapshot(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        if config is None:
            config = self.config.snapshot()["printer"]
        return {
            **config,
            "width_px": PAPERS[config["paper"]]["width_px"],
            **self.printer.status(),
        }


def _paper(config: dict[str, Any]) -> dict[str, int]:
    return PAPERS[config["printer"]["paper"]]
