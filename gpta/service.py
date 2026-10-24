"""Application facade used by the HTTP layer.

Wires together the module runner, scheduler, output store, and printer, and
builds the ``/api/state`` snapshot.

Lock order: runner → config. The printer and output store take their own
locks only around their own state and never call out while holding them.
"""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__, reader
from .blocks import Rule, Text, from_dict, to_dict
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
READER_MAX_CHARS = 8000
READER_MAX_BLOCKS = 600


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
        """Fetch a web page and return part of it as reader-mode blocks.

        ``offset`` (0-based) picks where in the article text the part starts;
        each part holds at most :data:`READER_MAX_CHARS` characters, and
        ``next_offset`` says where the following part starts (None when this
        part reaches the end). The blocks go back to the console, which sends
        them to :meth:`reader_print`, so what prints is what was previewed.
        """
        if not isinstance(body, dict):
            raise InvalidRequest("body must be an object")
        print_style(body)  # validate before fetching
        offset = body.get("offset", 0)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise InvalidRequest("offset must be a whole number of 0 or more")
        try:
            page = self.reader_fetcher(body.get("url"))
            article = reader.extract(page.html, page.url)
        except reader.ReaderError as exc:
            raise InvalidRequest(str(exc)) from exc
        part = reader.window(article.body, offset, READER_MAX_CHARS)
        if not part.body:
            raise InvalidRequest(
                f"the article has only {part.total:,} characters; "
                "nothing is left after that offset"
            )
        receipt = self._reader_receipt(article.elements(part))
        module_log(f"reader fetched {page.url} from character {part.start}")
        style = print_style(body, receipt.elements)
        image = render_image(receipt, self._width_px(), **style)
        return {
            "title": article.title,
            "url": page.url,
            "blocks": [to_dict(element) for element in receipt.elements],
            "start": part.start,
            "end": part.end,
            "total": part.total,
            "next_offset": part.end if part.end < part.total else None,
            "png": _png_url(image),
            "height_px": image.height,
        }

    def reader_print(self, body: Any) -> dict[str, Any]:
        raw = body.get("blocks") if isinstance(body, dict) else None
        if not isinstance(raw, list) or not raw:
            raise InvalidRequest("nothing to print; fetch a page first")
        if len(raw) > READER_MAX_BLOCKS:
            raise InvalidRequest(f"at most {READER_MAX_BLOCKS} blocks can be printed")
        print_style(body)  # validate before parsing blocks
        try:
            elements = [from_dict(item) for item in raw]
        except ValueError as exc:
            raise InvalidRequest(f"invalid block: {exc}") from exc
        # A fetched part holds READER_MAX_CHARS of article text; the slack
        # covers its header and footer. Anything longer did not come from a
        # fetch and is cut.
        receipt = self._reader_receipt(elements, READER_MAX_CHARS + 2000)
        style = print_style(body, receipt.elements)
        image = render_image(receipt, self._width_px(), **style)
        self.printer.submit(image, "reader", manual=True)
        module_log(f"reader print: {len(elements)} blocks")
        return {"ok": True}

    def _reader_receipt(self, elements: list, max_chars: int | None = None) -> Receipt:
        now = datetime.fromtimestamp(self.clock(), UTC)
        return build_receipt("", elements, max_chars, now)

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
