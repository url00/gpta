"""Module execution: one run = fetch with a timeout, render, store, auto-print."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .config import ConfigStore
from .errors import AlreadyRunning, ModuleStuck, ModuleTimeout, NotFound
from .module_api import Block, Cache, Module, ModuleContext
from .outputs import OutputStore
from .printing import Printer, PrinterDisabled, PrinterError, PrinterRateLimited
from .renderer import render_receipt

log = logging.getLogger("gpta.runner")

MAX_EMITS_PER_RUN = 100
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


@dataclass
class ModuleRuntime:
    last_run_at: str | None = None
    last_run_mono: float | None = None
    last_ok: bool | None = None
    last_error: str | None = None
    last_duration_ms: int | None = None
    run_count: int = 0
    items: int = 0
    running: bool = False
    print_error: str | None = None
    consecutive_failures: int = 0


@dataclass(frozen=True)
class RunTicket:
    """A claimed run: the module plus the config it will run with."""

    module: Module
    cfg: dict[str, Any]
    width: int


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
        clock=time.time,
        monotonic=time.monotonic,
    ):
        self.config = config
        self.registry = registry
        self.outputs = outputs
        self.printer = printer
        self.http = http
        self.fetch_timeout = float(fetch_timeout)
        self.clock = clock
        self.monotonic = monotonic
        self._lock = threading.RLock()
        self._runtime = {module_id: ModuleRuntime() for module_id in registry}
        self._stuck: dict[str, threading.Thread] = {}

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
                "printed": False,
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
            int(settings["printer"]["width"]),
        )

    def execute(self, ticket: RunTicket, trigger: str) -> dict[str, Any]:
        """Execute a claimed run. Always releases the claim."""
        module, cfg = ticket.module, ticket.cfg
        module_id = module.id
        started = self.monotonic()
        error: str | None = None
        printed = False
        module_log(f"run started ({trigger})", "info", module_id)

        try:
            now = self._now()
            ctx = ModuleContext(
                module_id=module_id,
                limits=dict(cfg["limits"]),
                options=dict(cfg["options"]),
                data_dir=self.config.data_dir,
                now=now,
                http=self.http,
                cache=Cache(self.config.data_dir / "cache"),
                log=self._make_emitter(module_id),
            )
            blocks = self._fetch_with_timeout(module, ctx)
            if not blocks:
                blocks = [Block("No content", ("No items available.",))]
            lines = render_receipt(
                module.name,
                blocks,
                ticket.width,
                max_chars=int(cfg["limits"]["max_chars"]),
                now=now,
            )
            self.outputs.set(module_id, lines, self._iso_now())
            with self._lock:
                runtime = self._runtime[module_id]
                runtime.last_ok = True
                runtime.last_error = None
                runtime.items = len(blocks)
                runtime.print_error = None
                runtime.consecutive_failures = 0
            module_log(f"fetched {len(blocks)} block(s)", "info", module_id)
            printed = self._auto_print(module_id, lines)
        except Exception as exc:
            error = str(exc)
            module_log(f"run failed: {error}", "error", module_id)
            with self._lock:
                runtime = self._runtime[module_id]
                runtime.last_ok = False
                runtime.last_error = error
                runtime.print_error = None
                runtime.consecutive_failures += 1
        finally:
            with self._lock:
                runtime = self._runtime[module_id]
                runtime.running = False
                runtime.last_run_mono = self.monotonic()
                runtime.last_run_at = self._iso_now()
                runtime.run_count += 1
                runtime.last_duration_ms = int(
                    round((self.monotonic() - started) * 1000)
                )

        result: dict[str, Any] = {"ok": error is None, "printed": printed}
        if error is not None:
            result["error"] = error
        result["runtime"] = self.runtime_snapshot(module_id)
        return result

    def _auto_print(self, module_id: str, lines: list[str]) -> bool:
        try:
            self.printer.submit(lines, module_id, auto=True)
        except PrinterDisabled:
            return False
        except PrinterRateLimited as exc:
            module_log(f"print skipped: {exc}", "info", module_id)
            return False
        except PrinterError as exc:
            with self._lock:
                self._runtime[module_id].print_error = str(exc)
            return False
        with self._lock:
            self._runtime[module_id].print_error = None
        return True

    def _fetch_with_timeout(self, module: Module, ctx: ModuleContext) -> Any:
        holder: dict[str, Any] = {}
        done = threading.Event()

        def target() -> None:
            try:
                holder["result"] = module.fetch(ctx)
            except Exception as exc:
                holder["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(
            target=target, name=f"gpta-fetch-{module.id}", daemon=True
        )
        worker.start()
        if not done.wait(self.fetch_timeout):
            with self._lock:
                self._stuck[module.id] = worker
            raise ModuleTimeout(f"timeout after {self.fetch_timeout:g}s")
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
                "items": runtime.items,
                "running": runtime.running,
                "print_error": runtime.print_error,
                "next_due_seconds": next_due,
                "consecutive_failures": runtime.consecutive_failures,
                "effective_interval_seconds": interval,
            }

    # -- helpers -------------------------------------------------------
    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), timezone.utc)

    def _iso_now(self) -> str:
        return self._now().isoformat(timespec="seconds")
