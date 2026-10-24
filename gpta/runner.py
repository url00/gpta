"""Module execution: one run = fetch with a timeout, render, store, auto-print."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from PIL import Image

from .config import PAPERS, ConfigStore
from .errors import AlreadyRunning, ModuleStuck, ModuleTimeout, NotFound
from .module_api import Block, Cache, Module, ModuleContext
from .outputs import OutputStore
from .printing import (
    AutoPrintOff,
    Printer,
    PrinterDisabled,
    PrinterError,
    PrinterRateLimited,
)
from .renderer import Receipt, build_receipt, render_image
from .state import StateStore

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
    # Wall-clock time of the last run, saved across restarts.
    last_run_ts: float | None = None
    # The same moment on the monotonic clock, which scheduling uses.
    last_run_mono: float | None = None
    last_ok: bool | None = None
    last_error: str | None = None
    last_duration_ms: int | None = None
    run_count: int = 0
    running: bool = False
    print_error: str | None = None
    # Why the last successful run was not printed, when the printer is on.
    print_skipped: str | None = None
    consecutive_failures: int = 0


@dataclass(frozen=True)
class RunTicket:
    """A claimed run: the module plus the config it will run with."""

    module: Module
    cfg: dict[str, Any]
    width_px: int


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
        clock=time.time,
        monotonic=time.monotonic,
    ):
        self.config = config
        self.state = state
        self.registry = registry
        self.outputs = outputs
        self.printer = printer
        self.http = http
        self.fetch_timeout = float(fetch_timeout)
        self.clock = clock
        self.monotonic = monotonic
        self.cache = Cache(config.data_dir / "cache")
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
            PAPERS[settings["printer"]["paper"]]["width_px"],
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
                cache=self.cache,
                log=self._make_emitter(module_id),
            )
            blocks = self._fetch_with_timeout(module, ctx)
            if not blocks:
                blocks = [Block("No content", ("No items available.",))]
            receipt = build_receipt(
                module.name,
                blocks,
                max_chars=int(cfg["limits"]["max_chars"]),
                now=now,
            )
            # Render now, so a layout error fails the run and the preview and
            # the print share one image.
            image = render_image(receipt, ticket.width_px)
            self.outputs.set(module_id, receipt, self._iso_now(), image=image)
            with self._lock:
                runtime = self._runtime[module_id]
                runtime.last_ok = True
                runtime.last_error = None
                runtime.print_error = None
                runtime.print_skipped = None
                runtime.consecutive_failures = 0
            module_log(f"fetched {len(blocks)} block(s)", "info", module_id)
            printed = self._auto_print(module_id, image, manual=trigger == "manual")
        except Exception as exc:
            error = str(exc)
            module_log(f"run failed: {error}", "error", module_id)
            with self._lock:
                runtime = self._runtime[module_id]
                runtime.last_ok = False
                runtime.last_error = error
                runtime.print_error = None
                runtime.print_skipped = None
                runtime.consecutive_failures += 1
        finally:
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

        self._persist()
        result: dict[str, Any] = {"ok": error is None, "printed": printed}
        with self._lock:
            result["print_skipped"] = self._runtime[module_id].print_skipped
        if error is not None:
            result["error"] = error
        result["runtime"] = self.runtime_snapshot(module_id)
        return result

    def _auto_print(self, module_id: str, image: Image.Image, *, manual: bool) -> bool:
        """Print a run's receipt if printing is on; record why not otherwise."""
        try:
            self.printer.submit(image, module_id, auto=True, manual=manual)
        except AutoPrintOff as exc:
            self._set_print_state(module_id, skipped=str(exc))
            return False
        except PrinterDisabled:
            return False  # simulator-only mode; nothing to report
        except PrinterRateLimited as exc:
            module_log(f"print skipped: {exc}", "info", module_id)
            self._set_print_state(module_id, skipped=str(exc))
            return False
        except PrinterError as exc:
            self._set_print_state(module_id, error=str(exc))
            return False
        self._set_print_state(module_id)
        return True

    def _set_print_state(
        self, module_id: str, *, error: str | None = None, skipped: str | None = None
    ) -> None:
        with self._lock:
            runtime = self._runtime[module_id]
            runtime.print_error = error
            runtime.print_skipped = skipped

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

    def _persist(self) -> None:
        if self.state is None:
            return
        with self._persist_lock:
            modules: dict[str, dict[str, Any]] = {}
            with self._lock:
                for module_id, runtime in self._runtime.items():
                    if runtime.last_run_ts is None:
                        continue
                    modules[module_id] = {
                        "last_run_ts": runtime.last_run_ts,
                        "last_run_at": runtime.last_run_at,
                        "last_ok": runtime.last_ok,
                        "last_error": runtime.last_error,
                        "last_duration_ms": runtime.last_duration_ms,
                        "run_count": runtime.run_count,
                        "consecutive_failures": runtime.consecutive_failures,
                    }
            for module_id, entry in modules.items():
                output = self.outputs.get(module_id)
                if output is not None:
                    entry["output"] = {
                        "receipt": output.receipt.to_dict(),
                        "generated_at": output.generated_at,
                    }
            self.state.save_modules(modules)

    # -- helpers -------------------------------------------------------
    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    def _iso_now(self) -> str:
        return self._now().isoformat(timespec="seconds")
