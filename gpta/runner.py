"""Module execution: one run = fetch with a timeout, render, store, auto-print."""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from PIL import Image

from . import reader
from .blocks import Block, Document, Element
from .cache import Cache
from .config import PAPERS, ConfigStore
from .errors import AlreadyRunning, ModuleStuck, ModuleTimeout, NotFound
from .module_api import FetchError, Module, ModuleContext, Reading, Skip
from .outputs import OutputStore
from .printing import (
    AutoPrintOff,
    Printer,
    PrinterDisabled,
    PrinterRateLimited,
)
from .renderer import Receipt, build_receipt, render_image, truncate
from .selections import Selections, SelectionStore
from .state import StateStore
from .storage import Database, ModuleStore

log = logging.getLogger("gpta.runner")

MAX_EMITS_PER_RUN = 100
#: Longest receipt a scanned selection may print: like web pages, a chosen
#: article prints whole, so this only guards against runaway output.
SELECT_MAX_CHARS = 150_000
#: Selection handlers running at once, counting timed-out ones still alive.
MAX_SELECT_WORKERS = 4
#: Separate prints one selection may ask for.
MAX_SELECT_PARTS = 5
#: Printer dots per mm (203 dpi), for paper length estimates.
DOTS_PER_MM = 8
MAX_BACKOFF_INTERVAL = 604800
LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def module_log(message: str, level: str = "info", module: str | None = None) -> None:
    """Log ``message``, tagged with ``module`` for the console log list."""
    log.log(
        LOG_LEVELS.get(str(level).lower(), logging.INFO),
        "%s",
        message,
        extra={"gpta_module": module},
    )


class PageReader:
    """``ctx.reader``: reader mode for modules, with the same guards as the
    console's web page tab (public http/https sites only, 3 MB, HTML)."""

    def __init__(self, runner: ModuleRunner):
        self._runner = runner

    def read(self, url: str) -> Reading:
        """Download ``url`` and return the article with what it is made of."""
        try:
            page = self._runner.page_fetcher(url)
        except reader.ReaderError as exc:
            raise FetchError(str(exc)) from exc
        return self.read_html(page.html, page.url)

    def read_html(self, html: str, url: str, title: str = "") -> Reading:
        """The same for ``html`` (a page or a fragment). ``url`` is where it
        came from; ``title`` replaces the page's own."""
        try:
            article = reader.extract(html, url)
        except reader.ReaderError as exc:
            raise FetchError(str(exc)) from exc
        if title:
            article = dataclasses.replace(article, title=title)
        return Reading(
            tuple(article.elements(truncate(article.body, SELECT_MAX_CHARS))),
            article.words,
            article.code_words,
            article.table_rows,
            article.media,
        )

    def article(self, url: str) -> list[Element]:
        """Just the blocks of :meth:`read`."""
        return list(self.read(url).blocks)

    def html(self, html: str, url: str, title: str = "") -> list[Element]:
        """Just the blocks of :meth:`read_html`."""
        return list(self.read_html(html, url, title).blocks)


class _Abandoned(ModuleTimeout):
    """A timed-out call, with the worker thread that is still running it."""

    def __init__(self, message: str, worker: threading.Thread):
        super().__init__(message)
        self.worker = worker


@dataclass
class ModuleRuntime:
    last_run_at: str | None = None
    # Wall-clock time of the last run, saved across restarts.
    last_run_ts: float | None = None
    # The same moment on the monotonic clock, which scheduling uses.
    last_run_mono: float | None = None
    last_ok: bool | None = None
    last_error: str | None = None
    last_duration_ms: int | None = None
    run_count: int = 0
    running: bool = False
    # Why the module's last print job failed on the device, if it did.
    print_error: str | None = None
    # Why the last successful run was not queued, when the printer is on.
    print_skipped: str | None = None
    consecutive_failures: int = 0


@dataclass(frozen=True)
class RunTicket:
    """A claimed run: the module plus the config it will run with."""

    module: Module
    cfg: dict[str, Any]
    width_px: int
    printer: dict[str, Any]
    settings: dict[str, Any]


def _styled(content: Any, style: dict[str, str], printer: dict[str, Any]) -> Any:
    """``content`` with its layout and size settled: the module's setting
    wins, then what the module asked for, then the printer's default.
    Anything that is not blocks is passed on for ``build_receipt`` to
    reject."""
    document = content if isinstance(content, Document) else None
    if document is None and not isinstance(content, (list, tuple)):
        return content
    elements = document.elements if document else content

    def pick(name: str) -> str:
        if style[name] != "default":
            return style[name]
        asked = getattr(document, name) if document else None
        return asked or printer[f"module_{name}"]

    return Document(
        elements, pick("layout"), pick("size"), document.lead if document else None
    )


def _parts(content: Any) -> list[Any]:
    """What ``on_select()`` returned, as the separate prints it asks for: a
    list made only of Documents is one print each; anything else is one."""
    if (
        isinstance(content, (list, tuple))
        and content
        and all(isinstance(part, Document) for part in content)
    ):
        parts = list(content)
    else:
        parts = [content]
    if len(parts) > MAX_SELECT_PARTS:
        raise ValueError(f"a selection may print at most {MAX_SELECT_PARTS} parts")
    for part in parts:
        if not part or (isinstance(part, Document) and not part.elements):
            raise ValueError("the selection produced nothing to print")
    return parts


def effective_interval(cfg: dict[str, Any], runtime: ModuleRuntime) -> int:
    """The configured interval, doubled per consecutive failure (capped)."""
    base = int(cfg["interval_seconds"])
    if runtime.consecutive_failures <= 0:
        return base
    factor = 2 ** min(runtime.consecutive_failures, 20)
    return min(base * factor, MAX_BACKOFF_INTERVAL)


class ModuleRunner:
    """Runs modules and tracks their runtime state.

    Lock order: the runner lock may be held while reading the config store,
    never the reverse.
    """

    def __init__(
        self,
        config: ConfigStore,
        registry: dict[str, Module],
        outputs: OutputStore,
        printer: Printer,
        http: Any,
        *,
        fetch_timeout: float,
        state: StateStore | None = None,
        db: Database | None = None,
        clock=time.time,
        monotonic=time.monotonic,
    ):
        self.config = config
        self.state = state
        # Module stores live here; without a database they last the process.
        self.db = db if db is not None else Database(None)
        self.registry = registry
        self.outputs = outputs
        self.printer = printer
        self.http = http
        self.fetch_timeout = float(fetch_timeout)
        self.clock = clock
        self.monotonic = monotonic
        self.cache = Cache(config.data_dir / "cache")
        self.selections = SelectionStore(self.db, clock)
        # Replaced in tests so ctx.reader needs no network.
        self.page_fetcher = reader.fetch_page
        self._select_workers: list[threading.Thread] = []
        self._lock = threading.RLock()
        self._runtime = {module_id: ModuleRuntime() for module_id in registry}
        self._stuck: dict[str, threading.Thread] = {}
        # Serialises saves so an older snapshot never overwrites a newer one.
        # Order: persist lock -> runner lock.
        self._persist_lock = threading.Lock()
        if state is not None:
            self._restore(state.load_modules())

    # -- running -------------------------------------------------------
    def run(self, module_id: str, trigger: str = "manual") -> dict[str, Any]:
        """Run a module synchronously and return the result."""
        try:
            ticket = self.begin(module_id)
        except ModuleStuck as exc:
            # A timed-out worker is still alive. Report a failed run rather
            # than wedging the API in a permanent 409.
            return {
                "ok": False,
                "queued": False,
                "error": str(exc),
                "runtime": self.runtime_snapshot(module_id),
            }
        return self.execute(ticket, trigger)

    def begin(self, module_id: str) -> RunTicket:
        """Claim a run for ``module_id`` (single-flight)."""
        with self._lock:
            if module_id not in self.registry:
                raise NotFound(f"unknown module: {module_id}")
            runtime = self._runtime[module_id]
            if runtime.running:
                raise AlreadyRunning(f"module {module_id} is already running")
            stuck = self._stuck.get(module_id)
            if stuck is not None:
                if stuck.is_alive():
                    raise ModuleStuck(
                        f"module {module_id} has a timed-out run still executing"
                    )
                self._stuck.pop(module_id, None)
            runtime.running = True
            settings = self.config.snapshot()
        return RunTicket(
            self.registry[module_id],
            settings["modules"][module_id],
            PAPERS[settings["printer"]["paper"]]["width_px"],
            settings["printer"],
            settings,
        )

    def _context(
        self,
        module: Module,
        settings: dict[str, Any],
        now: datetime,
        store: ModuleStore,
        trigger: str,
    ) -> ModuleContext:
        """The context for one call into ``module``; ``settings`` is the
        config snapshot the call runs with."""
        cfg = settings["modules"][module.id]
        printer = settings["printer"]
        width_px = PAPERS[printer["paper"]]["width_px"]

        def paper_mm(content: Any) -> int:
            # Measured the way select() prints: same style, same paper.
            receipt = build_receipt(
                module.name,
                _styled(content, cfg["style"], printer),
                max_chars=SELECT_MAX_CHARS,
                now=now,
            )
            return round(render_image(receipt, width_px).height / DOTS_PER_MM)

        return ModuleContext(
            module_id=module.id,
            limits=dict(cfg["limits"]),
            options=dict(cfg["options"]),
            data_dir=self.config.data_dir,
            now=now,
            http=self.http,
            cache=self.cache,
            log=self._make_emitter(module.id),
            store=store,
            trigger=trigger,
            selections=Selections(
                self.selections, module.id, settings["server"]["public_url"]
            ),
            reader=PageReader(self),
            paper_mm=paper_mm,
        )

    # -- selections ----------------------------------------------------
    def select(self, module_id: str, payload: Any) -> list[dict[str, Any]]:
        """Hand a scanned selection to its module's ``on_select()`` and
        queue what it returns for printing. Returns the print jobs, in
        order: one, or one per :class:`Document` when the handler returned a
        list of them; none when the module raised :class:`Skip`.

        Independent of the module's scheduled runs (it does not take the
        single-flight claim or count towards backoff), but bounded: at most
        :data:`MAX_SELECT_WORKERS` handlers at once. Raises whatever the
        handler or the printer raises; every part is laid out before any is
        queued, so a part that cannot be drawn prints nothing.
        """
        module = self.registry.get(module_id)
        if module is None:
            raise NotFound(f"unknown module: {module_id}")
        settings = self.config.snapshot()
        cfg = settings["modules"][module_id]
        module_log("selection started", "info", module_id)
        store = ModuleStore(self.db, module_id)
        try:
            now = self._now()
            ctx = self._context(module, settings, now, store, "select")
            try:
                content = self._select_with_timeout(module, ctx, payload)
            except Skip as exc:
                store.commit()
                module_log(
                    f"selection skipped: {str(exc) or 'nothing to print'}",
                    "info",
                    module_id,
                )
                return []
            width_px = PAPERS[settings["printer"]["paper"]]["width_px"]
            images = []
            for part in _parts(content):
                receipt = build_receipt(
                    module.name,
                    _styled(part, cfg["style"], settings["printer"]),
                    max_chars=SELECT_MAX_CHARS,
                    now=now,
                )
                images.append(render_image(receipt, width_px))
            # A scan is a person asking, like Run now: no minimum spacing.
            jobs = [
                self.printer.submit(image, module_id, manual=True) for image in images
            ]
            store.commit()
        except Exception as exc:
            module_log(f"selection failed: {exc}", "error", module_id)
            raise
        finally:
            store.discard()
        numbers = ", ".join(str(job["id"]) for job in jobs)
        module_log(f"selection queued as job {numbers}", "info", module_id)
        return jobs

    def _select_with_timeout(
        self, module: Module, ctx: ModuleContext, payload: Any
    ) -> Any:
        with self._lock:
            self._select_workers = [w for w in self._select_workers if w.is_alive()]
            if len(self._select_workers) >= MAX_SELECT_WORKERS:
                raise AlreadyRunning("too many selections are being handled")
        return self._call_with_timeout(
            lambda: module.on_select(ctx, payload),
            f"gpta-select-{module.id}",
            self._select_workers,
        )

    def execute(self, ticket: RunTicket, trigger: str) -> dict[str, Any]:
        """Execute a claimed run. Always releases the claim."""
        module, cfg = ticket.module, ticket.cfg
        module_id = module.id
        started = self.monotonic()
        error: str | None = None
        skipped = False
        job: dict[str, Any] | None = None
        module_log(f"run started ({trigger})", "info", module_id)
        store = ModuleStore(self.db, module_id)

        try:
            now = self._now()
            ctx = self._context(module, ticket.settings, now, store, trigger)
            content = self._fetch_with_timeout(module, ctx)
            if not content or (isinstance(content, Document) and not content.elements):
                content = [Block("No content", ("No items available.",))]
            receipt = build_receipt(
                module.name,
                _styled(content, cfg["style"], ticket.printer),
                max_chars=int(cfg["limits"]["max_chars"]),
                now=now,
            )
            # Render now, so a layout error fails the run and the preview and
            # the print share one image.
            image = render_image(receipt, ticket.width_px)
            # The run has its receipt: keep what the module saved. A failure
            # to save fails the run before anything is shown or printed.
            store.commit()
            self.outputs.set(module_id, receipt, self._iso_now(), image=image)
            with self._lock:
                runtime = self._runtime[module_id]
                runtime.last_ok = True
                runtime.last_error = None
                runtime.print_error = None
                runtime.print_skipped = None
                runtime.consecutive_failures = 0
            module_log(f"fetched {len(receipt.elements)} block(s)", "info", module_id)
            job = self._auto_print(module_id, image, manual=trigger == "manual")
        except Skip as exc:
            # Nothing new: a successful run that keeps the previous receipt.
            try:
                store.commit()
            except Exception as commit_exc:
                error = self._fail(module_id, commit_exc)
            else:
                skipped = True
                with self._lock:
                    runtime = self._runtime[module_id]
                    runtime.last_ok = True
                    runtime.last_error = None
                    runtime.print_skipped = None
                    runtime.consecutive_failures = 0
                module_log(f"skipped: {str(exc) or 'nothing new'}", "info", module_id)
        except Exception as exc:
            error = self._fail(module_id, exc)
        finally:
            store.discard()  # no-op after a commit; drops a failed run's writes
            with self._lock:
                runtime = self._runtime[module_id]
                runtime.running = False
                runtime.last_run_mono = self.monotonic()
                runtime.last_run_ts = self.clock()
                runtime.last_run_at = self._iso_now()
                runtime.run_count += 1
                runtime.last_duration_ms = int(
                    round((self.monotonic() - started) * 1000)
                )

        self._persist(module_id)
        result: dict[str, Any] = {
            "ok": error is None,
            "skipped": skipped,
            "queued": job is not None,
            "job": job,
        }
        with self._lock:
            result["print_skipped"] = self._runtime[module_id].print_skipped
        if error is not None:
            result["error"] = error
        result["runtime"] = self.runtime_snapshot(module_id)
        return result

    def _fail(self, module_id: str, exc: Exception) -> str:
        """Record a failed run and return its error message."""
        error = str(exc)
        module_log(f"run failed: {error}", "error", module_id)
        with self._lock:
            runtime = self._runtime[module_id]
            runtime.last_ok = False
            runtime.last_error = error
            runtime.print_error = None
            runtime.print_skipped = None
            runtime.consecutive_failures += 1
        return error

    def _auto_print(
        self, module_id: str, image: Image.Image, *, manual: bool
    ) -> dict[str, Any] | None:
        """Queue a run's receipt for printing if printing is on; record why
        not otherwise. Returns the print job, or None."""
        try:
            job = self.printer.submit(image, module_id, auto=True, manual=manual)
        except AutoPrintOff as exc:
            self._set_print_state(module_id, skipped=str(exc))
            return None
        except PrinterDisabled:
            return None  # simulator-only mode; nothing to report
        except PrinterRateLimited as exc:
            module_log(f"print skipped: {exc}", "warning", module_id)
            self._set_print_state(module_id, skipped=str(exc))
            return None
        self._set_print_state(module_id)
        return job

    def note_print(self, job: dict[str, Any], error: str | None) -> None:
        """Record how a module's print job ended (the printer calls this)."""
        if job["source"] in self._runtime:
            with self._lock:
                self._runtime[job["source"]].print_error = error

    def _set_print_state(
        self, module_id: str, *, error: str | None = None, skipped: str | None = None
    ) -> None:
        with self._lock:
            runtime = self._runtime[module_id]
            runtime.print_error = error
            runtime.print_skipped = skipped

    def _fetch_with_timeout(self, module: Module, ctx: ModuleContext) -> Any:
        try:
            return self._call_with_timeout(
                lambda: module.fetch(ctx), f"gpta-fetch-{module.id}"
            )
        except _Abandoned as exc:
            with self._lock:
                self._stuck[module.id] = exc.worker
            raise ModuleTimeout(str(exc)) from None

    def _call_with_timeout(
        self,
        call: Callable[[], Any],
        name: str,
        workers: list[threading.Thread] | None = None,
    ) -> Any:
        """Run ``call`` on a worker thread for at most the fetch timeout.
        The worker is added to ``workers`` (under the runner lock) while it
        runs. Raises :class:`_Abandoned` when it does not finish in time."""
        holder: dict[str, Any] = {}
        done = threading.Event()

        def target() -> None:
            try:
                holder["result"] = call()
            except Exception as exc:
                holder["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(target=target, name=name, daemon=True)
        if workers is not None:
            with self._lock:
                workers.append(worker)
        worker.start()
        if not done.wait(self.fetch_timeout):
            raise _Abandoned(f"timeout after {self.fetch_timeout:g}s", worker)
        if "error" in holder:
            raise holder["error"]
        return holder.get("result")

    def _make_emitter(self, module_id: str):
        count = 0
        truncated = False

        def emit(message: str, level: str = "info") -> None:
            nonlocal count, truncated
            if truncated:
                return
            count += 1
            if count > MAX_EMITS_PER_RUN:
                truncated = True
                module_log(
                    f"module exceeded {MAX_EMITS_PER_RUN} log emits; "
                    "further output suppressed",
                    "warning",
                    module_id,
                )
                return
            module_log(str(message), level, module_id)

        return emit

    # -- state ---------------------------------------------------------
    def is_due(self, module_id: str, cfg: dict[str, Any], now: float) -> bool:
        with self._lock:
            runtime = self._runtime[module_id]
            return (
                cfg["enabled"]
                and not runtime.running
                and (
                    runtime.last_run_mono is None
                    or now - runtime.last_run_mono >= effective_interval(cfg, runtime)
                )
            )

    def runtime_snapshot(
        self, module_id: str, cfg: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if cfg is None:
            cfg = self.config.module_config(module_id)
        with self._lock:
            runtime = self._runtime[module_id]
            interval = effective_interval(cfg, runtime)
            if not cfg["enabled"]:
                next_due: int | None = None
            elif runtime.last_run_mono is None:
                next_due = 0
            else:
                elapsed = self.monotonic() - runtime.last_run_mono
                next_due = max(0, int(round(interval - elapsed)))
            return {
                "last_run_at": runtime.last_run_at,
                "last_ok": runtime.last_ok,
                "last_error": runtime.last_error,
                "last_duration_ms": runtime.last_duration_ms,
                "run_count": runtime.run_count,
                "running": runtime.running,
                "print_error": runtime.print_error,
                "print_skipped": runtime.print_skipped,
                "next_due_seconds": next_due,
                "consecutive_failures": runtime.consecutive_failures,
                "effective_interval_seconds": interval,
            }

    # -- persistence ---------------------------------------------------
    def _restore(self, saved: dict[str, dict[str, Any]]) -> None:
        """Load saved state so a restart does not re-run every module."""
        now_ts, now_mono = self.clock(), self.monotonic()
        for module_id, entry in saved.items():
            runtime = self._runtime.get(module_id)
            if runtime is None:
                continue
            ts = entry.get("last_run_ts")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                # Map the saved wall-clock time onto this process's monotonic
                # clock. A run "in the future" (the clock went backwards)
                # counts as just now, so it cannot cause a burst of runs.
                elapsed = max(0.0, now_ts - float(ts))
                runtime.last_run_ts = float(ts)
                runtime.last_run_mono = now_mono - elapsed
            for name, kinds in (
                ("last_run_at", (str,)),
                ("last_ok", (bool,)),
                ("last_error", (str,)),
                ("last_duration_ms", (int,)),
            ):
                value = entry.get(name)
                if isinstance(value, kinds):
                    setattr(runtime, name, value)
            for name in ("run_count", "consecutive_failures"):
                value = entry.get(name)
                if type(value) is int and value >= 0:
                    setattr(runtime, name, value)
            self._restore_output(module_id, entry.get("output"))

    def _restore_output(self, module_id: str, output: Any) -> None:
        """Restore a saved receipt. Text lines from older versions still load."""
        if not isinstance(output, dict):
            return
        generated_at = output.get("generated_at")
        if not isinstance(generated_at, str):
            return
        lines = output.get("lines")
        try:
            if "receipt" in output:
                receipt = Receipt.from_dict(output["receipt"])
            elif isinstance(lines, list) and all(isinstance(x, str) for x in lines):
                receipt = Receipt.from_lines(lines)
            else:
                return
        except (TypeError, ValueError) as exc:
            log.warning("ignoring saved output of %s: %s", module_id, exc)
            return
        self.outputs.set(module_id, receipt, generated_at)

    def _persist(self, module_id: str) -> None:
        """Save one module's run state and latest receipt."""
        if self.state is None:
            return
        with self._persist_lock:
            with self._lock:
                runtime = self._runtime[module_id]
                if runtime.last_run_ts is None:
                    return
                entry: dict[str, Any] = {
                    "last_run_ts": runtime.last_run_ts,
                    "last_run_at": runtime.last_run_at,
                    "last_ok": runtime.last_ok,
                    "last_error": runtime.last_error,
                    "last_duration_ms": runtime.last_duration_ms,
                    "run_count": runtime.run_count,
                    "consecutive_failures": runtime.consecutive_failures,
                }
            output = self.outputs.get(module_id)
            if output is not None:
                entry["output"] = {
                    "receipt": output.receipt.to_dict(),
                    "generated_at": output.generated_at,
                }
            self.state.save_module(module_id, entry)

    # -- helpers -------------------------------------------------------
    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    def _iso_now(self) -> str:
        return self._now().isoformat(timespec="seconds")
