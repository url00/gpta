"""Scheduler, module execution, output store, and printer orchestration.

Lock hierarchy: the service lock is always acquired before the config lock
(``self._lock`` then ``ConfigStore._lock``), never the reverse.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ConfigError, ConfigStore
from .http import HttpClient
from .logbus import LogBus
from .module_api import Block, Cache, Module, ModuleContext
from .printer import (
    PrinterError,
    PrinterRateLimited,
    PrinterWriter,
    test_receipt,
)
from .registry import describe
from .renderer import render_receipt, render_total

MAX_EMITS_PER_RUN = 100
MAX_BACKOFF_INTERVAL = 604800
SCHEDULER_STALE_SECONDS = 10
LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


class NotFound(Exception):
    pass


class AlreadyRunning(Exception):
    pass


class ValidationError(Exception):
    pass


class PrinterDisabled(Exception):
    pass


class ModuleTimeout(Exception):
    """A module fetch exceeded the configured run timeout."""


class ModuleStuck(Exception):
    """The abandoned worker for a timed-out run is still executing."""


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
    output_lines: list[str] = field(default_factory=list)
    output_text: str = ""
    output_at: str | None = None


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
        self.http = http or HttpClient()
        self.clock = clock
        self.monotonic = monotonic
        self.modules_dir = Path(modules_dir) if modules_dir else None
        self.bound_host: str | None = None
        self.bound_port: int | None = None
        self.registry_errors: dict[str, str] = dict(registry_errors or {})

        settings = self.config.snapshot()
        self.fetch_timeout = float(
            settings["server"]["run_timeout_seconds"]
            if fetch_timeout is None
            else fetch_timeout
        )

        self._lock = threading.RLock()
        self._runtime: dict[str, ModuleRuntime] = {
            module_id: ModuleRuntime() for module_id in registry
        }
        self._descriptions = {
            module_id: describe(module) for module_id, module in registry.items()
        }
        self._started_mono = self.monotonic()
        self._last_tick_mono: float | None = None
        self._last_tick_at: str | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_threads: list[threading.Thread] = []
        self._stuck: dict[str, threading.Thread] = {}

        self._printer_writer = printer_writer or PrinterWriter()
        self._printer_last_print_at: str | None = None
        self._printer_last_print_mono: float | None = None
        self._printer_last_error: str | None = None
        self._printer_last_job: dict[str, Any] | None = None
        self._printer_disabled_reason: str | None = None
        self._print_times: deque[float] = deque()
        self._consecutive_print_failures = 0

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop, name="gpta-scheduler", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread:
            thread.join(timeout=5)
        self._thread = None
        self._printer_writer.stop()

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        """Wait for scheduled runs dispatched by ``tick`` to finish."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                self._run_threads = [t for t in self._run_threads if t.is_alive()]
                threads = list(self._run_threads)
            if not threads:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            threads[0].join(remaining)

    def _loop(self) -> None:
        if self._stop_event.wait(2.0):
            return
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:  # pragma: no cover - tick already isolates modules
                logging.getLogger("gpta.service").exception("scheduler tick failed")
            if self._stop_event.wait(1.0):
                return

    # -- scheduler -----------------------------------------------------
    def tick(self) -> None:
        now = self.monotonic()
        with self._lock:
            self._last_tick_mono = now
            self._last_tick_at = self._iso_now()
        for module_id in sorted(self.registry):
            with self._lock:
                cfg = self.config.module_config(module_id)
                runtime = self._runtime[module_id]
                interval = self._effective_interval(cfg, runtime)
                due = (
                    cfg["enabled"]
                    and not runtime.running
                    and (
                        runtime.last_run_mono is None
                        or now - runtime.last_run_mono >= interval
                    )
                )
            if due:
                try:
                    self._dispatch(module_id)
                except (AlreadyRunning, ModuleStuck):
                    continue

    def _dispatch(self, module_id: str, trigger: str = "schedule") -> None:
        module, cfg, printer_cfg = self._begin_run(module_id)
        thread = threading.Thread(
            target=self._execute,
            args=(module, cfg, printer_cfg, trigger),
            name=f"gpta-run-{module_id}",
            daemon=True,
        )
        with self._lock:
            self._run_threads = [t for t in self._run_threads if t.is_alive()]
            self._run_threads.append(thread)
        thread.start()

    # -- execution -----------------------------------------------------
    def run_module(self, module_id: str, trigger: str = "manual") -> dict[str, Any]:
        try:
            module, cfg, printer_cfg = self._begin_run(module_id)
        except ModuleStuck as exc:
            # A timed-out worker is still alive. Report a failed run rather
            # than wedging the API in a permanent 409.
            return {
                "ok": False,
                "printed": False,
                "error": str(exc),
                "module": self._module_snapshot(module_id),
            }
        return self._execute(module, cfg, printer_cfg, trigger)

    def _begin_run(
        self, module_id: str
    ) -> tuple[Module, dict[str, Any], dict[str, Any]]:
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
            cfg = self.config.module_config(module_id)
            printer_cfg = self.config.snapshot()["printer"]
        return self.registry[module_id], cfg, printer_cfg

    def _execute(
        self,
        module: Module,
        cfg: dict[str, Any],
        printer_cfg: dict[str, Any],
        trigger: str,
    ) -> dict[str, Any]:
        module_id = module.id
        started = self.monotonic()
        error: str | None = None
        printed = False
        self._log(f"run started ({trigger})", "info", module_id)

        try:
            ctx = ModuleContext(
                module_id=module_id,
                limits=dict(cfg["limits"]),
                options=dict(cfg["options"]),
                data_dir=self.config.data_dir,
                now=self._now(),
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
                printer_cfg["width"],
                max_chars=int(cfg["limits"]["max_chars"]),
                now=self._now(),
            )
            text = "\n".join(lines) + "\n"
            duration = int(round((self.monotonic() - started) * 1000))
            with self._lock:
                runtime = self._runtime[module_id]
                runtime.last_ok = True
                runtime.last_error = None
                runtime.items = len(blocks)
                runtime.output_lines = list(lines)
                runtime.output_text = text
                runtime.output_at = self._iso_now()
                runtime.last_duration_ms = duration
                runtime.print_error = None
                runtime.consecutive_failures = 0
            self._log(f"fetched {len(blocks)} block(s)", "info", module_id)

            if printer_cfg["enabled"] and printer_cfg["auto_print"]:
                printed, print_error = self._auto_print(
                    module_id, lines, printer_cfg
                )
                if print_error is not None:
                    with self._lock:
                        self._runtime[module_id].print_error = print_error
        except ModuleTimeout as exc:
            error = str(exc)
            self._log(f"run failed: {error}", "error", module_id)
            with self._lock:
                runtime = self._runtime[module_id]
                runtime.last_ok = False
                runtime.last_error = error
                runtime.print_error = None
                runtime.consecutive_failures += 1
        except Exception as exc:
            error = str(exc)
            self._log(f"run failed: {error}", "error", module_id)
            with self._lock:
                runtime = self._runtime[module_id]
                runtime.last_ok = False
                runtime.last_error = error
                runtime.print_error = None
                runtime.consecutive_failures += 1
        finally:
            duration = int(round((self.monotonic() - started) * 1000))
            with self._lock:
                runtime = self._runtime[module_id]
                runtime.running = False
                runtime.last_run_mono = self.monotonic()
                runtime.last_run_at = self._iso_now()
                runtime.run_count += 1
                runtime.last_duration_ms = duration

        result: dict[str, Any] = {"ok": error is None, "printed": printed}
        if error is not None:
            result["error"] = error
        result["module"] = self._module_snapshot(module_id)
        return result

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
                self._log(
                    f"module exceeded {MAX_EMITS_PER_RUN} log emits; "
                    "further output suppressed",
                    "warning",
                    module_id,
                )
                return
            self._log(str(message), level, module_id)

        return emit

    @staticmethod
    def _effective_interval(cfg: dict[str, Any], runtime: ModuleRuntime) -> int:
        base = int(cfg["interval_seconds"])
        if runtime.consecutive_failures <= 0:
            return base
        factor = 2 ** min(runtime.consecutive_failures, 20)
        return min(base * factor, MAX_BACKOFF_INTERVAL)

    # -- printing ------------------------------------------------------
    def _reserve_print(self, printer_cfg: dict[str, Any]) -> str | None:
        """Reserve a physical print slot, or return the reason it is blocked."""
        now = self.monotonic()
        with self._lock:
            cutoff = now - 3600
            while self._print_times and self._print_times[0] < cutoff:
                self._print_times.popleft()
            spacing = int(printer_cfg["min_print_spacing_seconds"])
            if (
                spacing > 0
                and self._printer_last_print_mono is not None
                and now - self._printer_last_print_mono < spacing
            ):
                return f"minimum spacing is {spacing}s"
            limit = int(printer_cfg["max_prints_per_hour"])
            if len(self._print_times) >= limit:
                return f"hourly limit of {limit} prints reached"
            self._print_times.append(now)
            self._printer_last_print_mono = now
        return None

    def _record_print_success(self, module_id: str, lines: list[str]) -> None:
        with self._lock:
            self._consecutive_print_failures = 0
            self._printer_disabled_reason = None
            self._printer_last_print_at = self._iso_now()
            self._printer_last_error = None
            self._printer_last_job = {"module": module_id, "lines": list(lines)}
            if module_id in self._runtime:
                self._runtime[module_id].print_error = None

    def _record_print_failure(
        self, printer_cfg: dict[str, Any], message: str, module_id: str | None
    ) -> None:
        with self._lock:
            self._consecutive_print_failures += 1
            failures = self._consecutive_print_failures
            self._printer_last_error = message
            if module_id is not None and module_id in self._runtime:
                self._runtime[module_id].print_error = message
            trip = failures >= int(printer_cfg["failure_threshold"])
            if trip:
                self._printer_disabled_reason = (
                    f"auto-disabled after {failures} consecutive device failures"
                )
        if not trip:
            return
        try:
            self.config.update_printer({"enabled": False})
        except ConfigError as exc:  # pragma: no cover - disk failure
            self._log(f"could not auto-disable printer: {exc}", "error", None)
        self._log(
            f"printer auto-disabled after {failures} consecutive failures: {message}",
            "error",
            None,
        )

    def _auto_print(
        self, module_id: str, lines: list[str], printer_cfg: dict[str, Any]
    ) -> tuple[bool, str | None]:
        reason = self._reserve_print(printer_cfg)
        if reason is not None:
            self._log(f"print skipped: {reason}", "info", module_id)
            return False, None
        try:
            self._printer_writer.print(
                lines,
                printer_cfg,
                timeout=float(printer_cfg["write_timeout_seconds"]),
            )
        except PrinterError as exc:
            message = str(exc)
            self._log(f"print failed: {message}", "error", module_id)
            self._record_print_failure(printer_cfg, message, module_id)
            return False, message
        self._record_print_success(module_id, lines)
        return True, None

    # -- configuration -------------------------------------------------
    def update_module(self, module_id: str, patch: Any) -> dict[str, Any]:
        if module_id not in self.registry:
            raise NotFound(f"unknown module: {module_id}")
        try:
            self.config.update_module(module_id, patch)
        except KeyError as exc:
            raise NotFound(f"unknown module: {module_id}") from exc
        self._log("configuration updated", "info", module_id)
        return self._module_snapshot(module_id)

    def set_printer(self, patch: Any) -> dict[str, Any]:
        self.config.update_printer(patch)
        if isinstance(patch, dict) and patch.get("enabled") is True:
            with self._lock:
                self._consecutive_print_failures = 0
                self._printer_disabled_reason = None
        self._log("printer configuration updated", "info", None)
        return self._printer_snapshot()

    def test_printer(self) -> dict[str, Any]:
        with self._lock:
            cfg = self.config.snapshot()["printer"]
            if not cfg["enabled"]:
                raise PrinterDisabled("printer is disabled")
            lines = test_receipt(cfg["width"])
        reason = self._reserve_print(cfg)
        if reason is not None:
            raise PrinterRateLimited(f"print rate limited: {reason}")
        try:
            self._printer_writer.print(
                lines, cfg, timeout=float(cfg["write_timeout_seconds"])
            )
        except PrinterError as exc:
            self._record_print_failure(cfg, str(exc), None)
            raise
        self._record_print_success("test", lines)
        return {"ok": True, "lines": lines}

    # -- snapshots -----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        config = self.config.snapshot()
        with self._lock:
            modules = [
                self._module_snapshot(module_id, config)
                for module_id in sorted(self.registry)
            ]
            printer = self._printer_snapshot_locked(config["printer"])
            sections = self._total_inputs_locked(config)
            thread_alive = bool(self._thread and self._thread.is_alive())
            tick_age = (
                None
                if self._last_tick_mono is None
                else max(0, int(self.monotonic() - self._last_tick_mono))
            )
            server = {
                "version": self._version(),
                "host": self.bound_host or config["server"]["host"],
                "port": self.bound_port or config["server"]["port"],
                "uptime_seconds": max(
                    0, int(self.monotonic() - self._started_mono)
                ),
                "scheduler_running": thread_alive,
                "last_tick_at": self._last_tick_at,
                "last_tick_age_seconds": tick_age,
                "scheduler_stale": bool(
                    thread_alive
                    and tick_age is not None
                    and tick_age > SCHEDULER_STALE_SECONDS
                ),
                "config_path": str(self.config.path),
                "modules_dir": str(self.modules_dir) if self.modules_dir else "",
            }
            registry_errors = [
                {"module": name, "error": message}
                for name, message in sorted(self.registry_errors.items())
            ]

        width = int(config["printer"]["width"])
        total = render_total(
            [(module_id, lines) for module_id, lines, _at in sections], width
        )
        generated = [at for _mid, _lines, at in sections if at]
        return {
            "server": server,
            "printer": printer,
            "modules": modules,
            "total": {
                "lines": total,
                "text": "\n".join(total) + "\n" if total else "",
                "generated_at": max(generated) if generated else None,
                "module_ids": [mid for mid, _lines, _at in sections],
            },
            "registry_errors": registry_errors,
        }

    def output_text(self, key: str) -> str:
        if key == "total":
            config = self.config.snapshot()
            with self._lock:
                sections = self._total_inputs_locked(config)
            lines = render_total(
                [(module_id, lines) for module_id, lines, _at in sections],
                int(config["printer"]["width"]),
            )
            return "\n".join(lines) + "\n" if lines else ""
        with self._lock:
            if key not in self.registry:
                raise NotFound(f"unknown output: {key}")
            return self._runtime[key].output_text

    def _module_snapshot(
        self, module_id: str, config: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if config is not None:
            cfg = config["modules"][module_id]
        else:
            cfg = self.config.module_config(module_id)
        with self._lock:
            runtime = self._runtime[module_id]
            interval = self._effective_interval(cfg, runtime)
            if cfg["enabled"]:
                if runtime.last_run_mono is None:
                    next_due: int | None = 0
                else:
                    next_due = max(
                        0,
                        int(
                            round(
                                interval
                                - (self.monotonic() - runtime.last_run_mono)
                            )
                        ),
                    )
            else:
                next_due = None
            runtime_fields = {
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
            output = {
                "lines": list(runtime.output_lines),
                "text": runtime.output_text,
                "generated_at": runtime.output_at,
            }
        return {
            **self._descriptions[module_id],
            "enabled": cfg["enabled"],
            "interval_seconds": cfg["interval_seconds"],
            "limits": dict(cfg["limits"]),
            "options": dict(cfg["options"]),
            "runtime": runtime_fields,
            "output": output,
        }

    def _total_inputs_locked(
        self, config: dict[str, Any]
    ) -> list[tuple[str, list[str], str | None]]:
        sections: list[tuple[str, list[str], str | None]] = []
        for module_id in sorted(self.registry):
            cfg = config["modules"][module_id]
            runtime = self._runtime[module_id]
            if cfg["enabled"] and runtime.output_lines:
                sections.append(
                    (module_id, list(runtime.output_lines), runtime.output_at)
                )
        return sections

    def _printer_snapshot_locked(self, config: dict[str, Any]) -> dict[str, Any]:
        return {
            **config,
            "last_print_at": self._printer_last_print_at,
            "last_error": self._printer_last_error,
            "last_job": self._printer_last_job,
            "circuit_open": self._printer_disabled_reason is not None,
            "disabled_reason": self._printer_disabled_reason,
        }

    def _printer_snapshot(self) -> dict[str, Any]:
        config = self.config.snapshot()["printer"]
        with self._lock:
            return self._printer_snapshot_locked(config)

    # -- helpers -------------------------------------------------------
    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), timezone.utc)

    def _iso_now(self) -> str:
        return self._now().isoformat(timespec="seconds")

    def _log(self, message: str, level: str = "info", module: str | None = None) -> None:
        logging.getLogger("gpta.service").log(
            LOG_LEVELS.get(str(level).lower(), logging.INFO),
            "%s",
            message,
            extra={"gpta_module": module},
        )

    @staticmethod
    def _version() -> str:
        from . import __version__

        return __version__
