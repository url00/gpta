"""Application facade used by the HTTP layer.

Wires together the module runner, scheduler, output store, and printer, and
builds the ``/api/state`` snapshot.

Lock order: runner → config. The printer and output store take their own
locks only around their own state and never call out while holding them.
"""

from __future__ import annotations

import base64
import dataclasses
import logging
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__, markdown, reader, selections
from .blocks import (
    LAYOUTS,
    PRINT_SIZES,
    QR_SIDES,
    QR_SIZES,
    Heading,
    KeyValue,
    Rule,
    Text,
    size_of,
)
from .config import PAPERS, ConfigStore
from .errors import AlreadyRunning, InvalidRequest, ModuleTimeout, NotFound
from .http import HttpClient
from .logbus import LogBus
from .module_api import Module
from .outputs import OutputStore
from .printing import (
    Printer,
    PrinterDisabled,
    PrinterRateLimited,
    PrinterWriter,
)
from .printing.jobs import MOVES
from .registry import describe
from .renderer import (
    Receipt,
    build_receipt,
    render_image,
    render_text,
    set_double_spaced,
    test_receipt,
    to_png,
    truncate,
)
from .runner import ModuleRunner, module_log
from .scheduler import Scheduler
from .state import StateStore
from .storage import Database

log = logging.getLogger("gpta.service")

#: Longest text the Instant tab may print in one job.
#: Longest Instant text. Like web pages, long text prints whole; this only
#: guards against runaway pastes (about 15 m of paper at normal size).
INSTANT_MAX_CHARS = 150_000


def print_style(body: dict) -> dict[str, str]:
    """The ``layout`` and ``size`` a request asks for, as :class:`Receipt`
    fields. Raises InvalidRequest for a value that is not offered."""
    layout = body.get("layout", "single")
    if layout not in LAYOUTS:
        raise InvalidRequest(f"layout must be one of {', '.join(LAYOUTS)}")
    size = body.get("size", "body")
    if size not in PRINT_SIZES:
        raise InvalidRequest(f"size must be one of {', '.join(PRINT_SIZES)}")
    return {"layout": layout, "size": size}


#: How Instant text is read: as is, or as Markdown.
INSTANT_FORMATS = ("plain", "markdown")


def instant_receipt(body: Any) -> Receipt:
    """A one-off receipt of pasted ``text``: no header or footer.

    ``format`` is ``plain`` (the default) or ``markdown``.
    """
    if not isinstance(body, dict):
        raise InvalidRequest("body must be an object")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise InvalidRequest("text must be a non-empty string")
    if len(text) > INSTANT_MAX_CHARS:
        raise InvalidRequest(f"text is longer than {INSTANT_MAX_CHARS:,} characters")
    form = body.get("format", "plain")
    if form not in INSTANT_FORMATS:
        raise InvalidRequest(f"format must be one of {', '.join(INSTANT_FORMATS)}")
    style = print_style(body)
    if form == "plain":
        return Receipt("", (Text(text.strip("\n")),), **style)
    elements = markdown.to_blocks(text)
    if not elements:
        raise InvalidRequest("the Markdown has nothing to print")
    return Receipt("", tuple(elements), **style)


#: Longest article text the reader keeps; the rest is cut with a marker.
#: Whole pages print, so this only guards against runaway pages (about
#: 15 m of paper at normal size).
READER_MAX_CHARS = 150_000
#: Fetched pages kept for printing, newest last.
READER_CACHE_SIZE = 8
#: Printer dots per mm (203 dpi), for paper length estimates.
DOTS_PER_MM = 8


#: The ``module`` of codes made by the Settings tab's selection test; the
#: host handles their scans itself. Not a valid module id (ids are folders).
TEST_SELECTIONS = "_test"
TEST_MAX_CHOICES = 10


@dataclass(frozen=True)
class ScanResult:
    """What a scanned code led to: the HTTP status and the page to show."""

    status: int
    state: str
    heading: str
    detail: str = ""


def _test_options(body: Any) -> dict[str, Any]:
    """The selection test's settings from a request, validated."""
    if not isinstance(body, dict):
        raise InvalidRequest("body must be an object")
    count = body.get("count", 3)
    if type(count) is not int or not 1 <= count <= TEST_MAX_CHOICES:
        raise InvalidRequest(f"count must be between 1 and {TEST_MAX_CHOICES}")
    hours = body.get("expires_hours", 1)
    if type(hours) is not int or not 1 <= hours <= selections.MAX_EXPIRES_HOURS:
        raise InvalidRequest(
            f"expires_hours must be between 1 and {selections.MAX_EXPIRES_HOURS}"
        )
    side, qr = body.get("side", "right"), body.get("qr", "medium")
    if side not in QR_SIDES:
        raise InvalidRequest(f"side must be one of {', '.join(QR_SIDES)}")
    if qr not in QR_SIZES:
        raise InvalidRequest(f"qr must be one of {', '.join(QR_SIZES)}")
    flags = {name: body.get(name, True) for name in ("numbered", "rules")}
    if not all(isinstance(value, bool) for value in flags.values()):
        raise InvalidRequest("numbered and rules must be true or false")
    return {"count": count, "expires_hours": hours, "side": side, "qr": qr, **flags}


def _confirmed(body: Any) -> bool:
    """Whether the request says the operator already agreed to the length,
    so the job is not held again at the warning limit."""
    return isinstance(body, dict) and body.get("confirmed") is True


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
        self.db = Database(config.data_dir / "gpta.db")
        self.printer = Printer(
            config, self.db, writer=printer_writer, clock=clock, monotonic=monotonic
        )
        self.runner = ModuleRunner(
            config,
            registry,
            self.outputs,
            self.printer,
            http or HttpClient(),
            fetch_timeout=fetch_timeout,
            state=StateStore(self.db, config.data_dir / "state.json"),
            db=self.db,
            clock=clock,
            monotonic=monotonic,
        )
        self.scheduler = Scheduler(
            config, self.runner, clock=clock, monotonic=monotonic
        )
        self.printer.on_result = self.runner.note_print
        set_double_spaced(self.printer.jobs.double_spaced())
        self._descriptions = {
            module_id: describe(module) for module_id, module in registry.items()
        }
        self._started_mono = monotonic()

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        self.printer.start()  # jobs left waiting by the last run
        self.scheduler.start()

    def stop(self) -> None:
        self.scheduler.stop()
        self.printer.stop()
        self.db.close()

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
        self.printer.wake()  # the change may release, or hold, the queue
        module_log("printer configuration updated")
        return self._printer_snapshot()

    def test_printer(self) -> dict[str, Any]:
        paper = PAPERS[self.config.snapshot()["printer"]["paper"]]
        receipt = test_receipt(datetime.fromtimestamp(self.clock(), UTC))
        job = self.printer.submit(
            render_image(receipt, paper["width_px"]), "test", manual=True
        )
        return {
            "ok": True,
            "job": job,
            "lines": render_text(receipt, paper["columns"]),
        }

    def instant_preview(self, body: Any) -> dict[str, Any]:
        """The image an instant print would produce, as a PNG data URL."""
        image = render_image(instant_receipt(body), self._width_px())
        return {
            "png": _png_url(image),
            "height_px": image.height,
            "paper_mm": round(image.height / DOTS_PER_MM),
            "chars": len(body["text"]),
        }

    def instant_print(self, body: Any) -> dict[str, Any]:
        image = render_image(instant_receipt(body), self._width_px())
        job = self.printer.submit(
            image, "instant", manual=True, approved=_confirmed(body)
        )
        module_log(f"instant print queued: {len(body['text'])} characters")
        return {"ok": True, "job": job}

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
        receipt = self._reader_receipt(elements, body)
        module_log(f"reader fetched {page.url}: {total} characters")
        image = render_image(receipt, self._width_px())
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
        image = render_image(self._reader_receipt(elements, body), self._width_px())
        job = self.printer.submit(
            image, "reader", manual=True, approved=_confirmed(body)
        )
        module_log(f"reader print queued: {image.height // DOTS_PER_MM} mm")
        return {"ok": True, "job": job}

    # -- QR selections -------------------------------------------------
    def set_server(self, patch: Any) -> dict[str, Any]:
        """Change the live server settings (``public_url``)."""
        server = self.config.update_server(patch)
        module_log("server configuration updated")
        return {"public_url": server["public_url"]}

    def scan(self, token: str) -> ScanResult:
        """Act on a scanned selection code and say what happened."""
        outcome, selection = self.runner.selections.claim(token)
        if outcome == selections.UNKNOWN:
            return ScanResult(404, outcome, "Unknown code", "This code is not known.")
        title = selection["title"]
        if outcome == selections.EXPIRED:
            return ScanResult(410, outcome, "Code expired", title)
        if outcome == selections.REPEAT:
            return ScanResult(
                200, outcome, "Already sent", f"{title}: scanned a moment ago."
            )
        result = self._run_selection(selection)
        self.runner.selections.finish(
            token,
            f"{result.state}: {result.heading}",
            ok=result.status < 400,
        )
        return dataclasses.replace(
            result, detail=f"{title}: {result.detail}" if result.detail else title
        )

    def _run_selection(self, selection: dict[str, Any]) -> ScanResult:
        module_id = selection["module"]
        try:
            if module_id == TEST_SELECTIONS:
                jobs = [self._print_test_selection(selection)]
            else:
                jobs = self.runner.select(module_id, selection["payload"])
        except PrinterDisabled:
            return ScanResult(
                409, "printer_off", "Printer is off", "received, but not printed"
            )
        except (PrinterRateLimited, AlreadyRunning) as exc:
            return ScanResult(429, "busy", "Busy, try again", str(exc))
        except ModuleTimeout as exc:
            return ScanResult(504, "timeout", "Timed out", str(exc))
        except NotFound:
            return ScanResult(404, "no_module", "Module not installed", module_id)
        except Exception as exc:
            if module_id == TEST_SELECTIONS:  # ours: a bug, not a module's
                log.exception("selection test failed")
            return ScanResult(502, "failed", "Could not print", str(exc)[:200])
        if not jobs:
            return ScanResult(200, "skipped", "Nothing to print")
        numbers = ", ".join(str(job["id"]) for job in jobs)
        what = f"print job {numbers}" if len(jobs) == 1 else f"print jobs {numbers}"
        if any(job.get("needs_approval") for job in jobs):
            return ScanResult(
                200, "held", "Waiting for approval", f"{what}: long; see the Queue tab"
            )
        return ScanResult(200, "queued", "Printing", what)

    def _print_test_selection(self, selection: dict[str, Any]) -> dict[str, Any]:
        now = datetime.fromtimestamp(self.clock(), UTC)
        receipt = build_receipt(
            "Selection test",
            [
                Heading("Selection received"),
                Text(selection["title"], bold=True),
                KeyValue("Scans of this code", str(selection["use_count"])),
            ],
            now=now,
        )
        module_log(f"selection test scanned: {selection['title']}")
        return self.printer.submit(
            render_image(receipt, self._width_px()), "test", manual=True
        )

    def selection_test(self, body: Any) -> dict[str, Any]:
        """Make a sample selection list, as a module would, and return its
        preview; with ``"print": true``, print it too. Scanning one of its
        codes prints a short confirmation."""
        options = _test_options(body)
        count = options.pop("count")
        offer = selections.Selections(
            self.runner.selections,
            TEST_SELECTIONS,
            self.config.snapshot()["server"]["public_url"],
        )
        try:
            items = offer.offer(
                [
                    selections.Choice(
                        f"Test item {number}",
                        {"item": number},
                        "Scan to print a confirmation",
                    )
                    for number in range(1, count + 1)
                ],
                **options,
            )
        except selections.SelectionError as exc:
            raise InvalidRequest(str(exc)) from exc
        now = datetime.fromtimestamp(self.clock(), UTC)
        receipt = build_receipt(
            "Selection test",
            [Text("Scan a code to choose that item.", size="small"), Rule(), *items],
            now=now,
        )
        image = render_image(receipt, self._width_px())
        result: dict[str, Any] = {
            "png": _png_url(image),
            "paper_mm": round(image.height / DOTS_PER_MM),
            "lines": render_text(receipt, self._columns()),
        }
        if isinstance(body, dict) and body.get("print") is True:
            result["job"] = self.printer.submit(image, "test", manual=True)
            module_log(f"selection test list queued: {count} item(s)")
        return {**result, **self.selections_list()}

    def selections_list(self) -> dict[str, Any]:
        """The newest codes and how they have been used, for diagnostics.
        Tokens are credentials; like the rest of the API this is only for
        the operator."""
        public_url = self.config.snapshot()["server"]["public_url"]
        now = self.clock()

        def iso(ts: float | None) -> str | None:
            if ts is None:
                return None
            return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")

        return {
            "public_url": public_url,
            "debounce_seconds": selections.DEBOUNCE_SECONDS,
            "selections": [
                {
                    "token": item["token"],
                    "path": selections.SCAN_PATH + item["token"],
                    "module": item["module"],
                    "title": item["title"],
                    "created_at": iso(item["created_at"]),
                    "expires_at": iso(item["expires_at"]),
                    "expired": item["expires_at"] <= now,
                    "use_count": item["use_count"],
                    "last_used_at": iso(item["last_used_at"]),
                    "last_result": item["last_result"],
                }
                for item in self.runner.selections.recent()
            ],
        }

    # -- print queue ---------------------------------------------------
    def queue(self) -> dict[str, Any]:
        return self.printer.queue()

    def queue_png(self, job_id: str) -> bytes:
        data = self.printer.job_png(int(job_id))
        if data is None:
            raise NotFound(f"no image for print job {job_id}")
        return data

    def queue_pause(self, paused: bool) -> dict[str, Any]:
        if paused:
            self.printer.pause()
        else:
            self.printer.resume()
        return self.printer.queue()

    def queue_spacing(self, double: bool) -> dict[str, Any]:
        """Double space everything rendered from now on (kept across
        restarts). Jobs already queued keep the image they were given."""
        self.printer.jobs.set_double_spaced(double)
        set_double_spaced(double)
        module_log(f"line spacing: {'double' if double else 'single'}")
        return self.printer.queue()

    def queue_clear(self) -> dict[str, Any]:
        count = self.printer.clear()
        module_log(f"print queue cleared: {count} job(s) cancelled")
        return self.printer.queue()

    def queue_action(self, job_id: str, action: str, body: Any) -> dict[str, Any]:
        """Approve, cancel, retry or move one job."""
        number = int(job_id)
        if action == "approve":
            done = self.printer.approve(number)
        elif action == "cancel":
            done = self.printer.cancel(number)
        elif action == "retry":
            done = self.printer.retry(number)
        else:
            to = body.get("to") if isinstance(body, dict) else None
            if to not in MOVES:
                raise InvalidRequest(f"to must be one of {', '.join(MOVES)}")
            done = self.printer.move(number, to)
        if not done:
            raise NotFound(f"print job {job_id} cannot be changed that way now")
        module_log(f"print job {job_id}: {action}")
        return self.printer.queue()

    def _reader_receipt(self, elements: list, body: dict) -> Receipt:
        """A web page's receipt; its header (up to the heavy rule) stays
        full width in columns."""
        now = datetime.fromtimestamp(self.clock(), UTC)
        receipt = build_receipt("", elements, now=now)
        return dataclasses.replace(receipt, **print_style(body))

    def _width_px(self) -> int:
        return _paper(self.config.snapshot())["width_px"]

    def _columns(self) -> int:
        return _paper(self.config.snapshot())["columns"]

    # -- snapshots -----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        config = self.config.snapshot()
        return {
            "server": {
                "version": __version__,
                "host": self.bound_host or config["server"]["host"],
                "port": self.bound_port or config["server"]["port"],
                "public_url": config["server"]["public_url"],
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
            "style": dict(cfg["style"]),
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
