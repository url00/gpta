"""Scheduler, module execution, output store, and printer orchestration."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ConfigError, ConfigStore
from .http import HttpClient
from .logbus import LogBus
from .module_api import Block, Module, ModuleContext
from .printer import PrinterError, send_to_device, test_receipt
from .registry import describe
from .renderer import render_receipt, render_total


class NotFound(Exception):
    pass


class AlreadyRunning(Exception):
    pass


class ValidationError(Exception):
    pass


class PrinterDisabled(Exception):
    pass


@dataclass
class ModuleRuntime:
    last_run_at: str | None = None
    last_run_epoch: float | None = None
    last_ok: bool | None = None
    last_error: str | None = None
    last_duration_ms: int | None = None
    run_count: int = 0
    items: int = 0
    running: bool = False
    print_error: str | None = None
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
        modules_dir: Path | None = None,
    ):
        self.config = config
        self.registry = registry
        self.logbus = logbus
        self.http = http or HttpClient()
        self.clock = clock
        self.modules_dir = Path(modules_dir) if modules_dir else None
        self.bound_host: str | None = None
        self.bound_port: int | None = None

        self._lock = threading.RLock()
        self._runtime: dict[str, ModuleRuntime] = {
            module_id: ModuleRuntime() for module_id in registry
        }
        self._started_at = self.clock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._printer_last_print_at: str | None = None
        self._printer_last_error: str | None = None
        self._printer_last_job: dict[str, Any] | None = None

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
        for module_id in sorted(self.registry):
            with self._lock:
                cfg = self.config.module_config(module_id)
                runtime = self._runtime[module_id]
                due = (
                    cfg["enabled"]
                    and not runtime.running
                    and (
                        runtime.last_run_epoch is None
                        or self.clock() - runtime.last_run_epoch
                        >= cfg["interval_seconds"]
                    )
                )
            if due:
                try:
                    self.run_module(module_id, trigger="schedule")
                except AlreadyRunning:
                    continue

    # -- execution -----------------------------------------------------
    def run_module(self, module_id: str, trigger: str = "manual") -> dict[str, Any]:
        with self._lock:
            if module_id not in self.registry:
                raise NotFound(f"unknown module: {module_id}")
            runtime = self._runtime[module_id]
            if runtime.running:
                raise AlreadyRunning(f"module {module_id} is already running")
            runtime.running = True
            cfg = self.config.module_config(module_id)
            printer_cfg = self.config.snapshot()["printer"]

        module = self.registry[module_id]
        started = time.monotonic()
        error: str | None = None
        printed = False
        print_error: str | None = None
        self._log(f"run started ({trigger})", "info", module_id)

        try:
            ctx = ModuleContext(
                module_id=module_id,
                limits=dict(cfg["limits"]),
                options=dict(cfg["options"]),
                data_dir=self.config.data_dir,
                now=self._now(),
                http=self.http,
                log=lambda message, level="info", _id=module_id: self._log(
                    message, level, _id
                ),
            )
            blocks = module.fetch(ctx)
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
            duration = int(round((time.monotonic() - started) * 1000))
            with self._lock:
                runtime.last_ok = True
                runtime.last_error = None
                runtime.items = len(blocks)
                runtime.output_lines = list(lines)
                runtime.output_text = text
                runtime.output_at = self._iso_now()
                runtime.last_duration_ms = duration
                runtime.print_error = None
            self._log(f"fetched {len(blocks)} block(s)", "info", module_id)

            if printer_cfg["enabled"] and printer_cfg["auto_print"]:
                try:
                    send_to_device(lines, printer_cfg)
                except PrinterError as exc:
                    print_error = str(exc)
                    self._log(f"print failed: {print_error}", "error", module_id)
                    with self._lock:
                        runtime.print_error = print_error
                        self._printer_last_error = print_error
                else:
                    printed = True
                    with self._lock:
                        self._printer_last_print_at = self._iso_now()
                        self._printer_last_error = None
                        self._printer_last_job = {
                            "module": module_id,
                            "lines": list(lines),
                        }
        except Exception as exc:
            error = str(exc)
            self._log(f"run failed: {error}", "error", module_id)
            with self._lock:
                runtime.last_ok = False
                runtime.last_error = error
                runtime.print_error = None
        finally:
            duration = int(round((time.monotonic() - started) * 1000))
            with self._lock:
                runtime.running = False
                runtime.last_run_epoch = self.clock()
                runtime.last_run_at = self._iso_now()
                runtime.run_count += 1
                runtime.last_duration_ms = duration

        result: dict[str, Any] = {"ok": error is None, "printed": printed}
        if error is not None:
            result["error"] = error
        result["module"] = self._module_snapshot(module_id)
        return result

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
        self._log("printer configuration updated", "info", None)
        return self._printer_snapshot()

    def test_printer(self) -> dict[str, Any]:
        with self._lock:
            cfg = self.config.snapshot()["printer"]
            if not cfg["enabled"]:
                raise PrinterDisabled("printer is disabled")
            lines = test_receipt(cfg["width"])
        try:
            send_to_device(lines, cfg)
        except PrinterError as exc:
            with self._lock:
                self._printer_last_error = str(exc)
            raise
        with self._lock:
            self._printer_last_print_at = self._iso_now()
            self._printer_last_error = None
            self._printer_last_job = {"module": "test", "lines": list(lines)}
        return {"ok": True, "lines": lines}

    # -- snapshots -----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            config = self.config.snapshot()
            modules = [self._module_snapshot(mid) for mid in sorted(self.registry)]
            total, generated_at, module_ids = self._total_snapshot()
            server_cfg = config["server"]
            return {
                "server": {
                    "version": self._version(),
                    "host": self.bound_host or server_cfg["host"],
                    "port": self.bound_port or server_cfg["port"],
                    "uptime_seconds": max(0, int(self.clock() - self._started_at)),
                    "scheduler_running": bool(
                        self._thread and self._thread.is_alive()
                    ),
                    "config_path": str(self.config.path),
                    "modules_dir": str(self.modules_dir) if self.modules_dir else "",
                },
                "printer": self._printer_snapshot_locked(config["printer"]),
                "modules": modules,
                "total": {
                    "lines": total,
                    "text": "\n".join(total) + "\n" if total else "",
                    "generated_at": generated_at,
                    "module_ids": module_ids,
                },
                "logs": self.logbus.recent(200),
            }

    def output_text(self, key: str) -> str:
        with self._lock:
            if key == "total":
                lines, _generated, _ids = self._total_snapshot()
                return "\n".join(lines) + "\n" if lines else ""
            if key not in self.registry:
                raise NotFound(f"unknown output: {key}")
            return self._runtime[key].output_text

    def _module_snapshot(self, module_id: str) -> dict[str, Any]:
        module = self.registry[module_id]
        cfg = self.config.module_config(module_id)
        runtime = self._runtime[module_id]
        if cfg["enabled"]:
            if runtime.last_run_epoch is None:
                next_due: int | None = 0
            else:
                next_due = max(
                    0,
                    int(
                        round(
                            cfg["interval_seconds"]
                            - (self.clock() - runtime.last_run_epoch)
                        )
                    ),
                )
        else:
            next_due = None
        info = describe(module)
        return {
            **info,
            "enabled": cfg["enabled"],
            "interval_seconds": cfg["interval_seconds"],
            "limits": dict(cfg["limits"]),
            "options": dict(cfg["options"]),
            "runtime": {
                "last_run_at": runtime.last_run_at,
                "last_ok": runtime.last_ok,
                "last_error": runtime.last_error,
                "last_duration_ms": runtime.last_duration_ms,
                "run_count": runtime.run_count,
                "items": runtime.items,
                "running": runtime.running,
                "print_error": runtime.print_error,
                "next_due_seconds": next_due,
            },
            "output": {
                "lines": list(runtime.output_lines),
                "text": runtime.output_text,
                "generated_at": runtime.output_at,
            },
        }

    def _total_snapshot(self) -> tuple[list[str], str | None, list[str]]:
        width = self.config.snapshot()["printer"]["width"]
        sections: list[tuple[str, list[str]]] = []
        generated: list[str] = []
        ids: list[str] = []
        for module_id in sorted(self.registry):
            cfg = self.config.module_config(module_id)
            runtime = self._runtime[module_id]
            if cfg["enabled"] and runtime.output_lines:
                sections.append((module_id, list(runtime.output_lines)))
                ids.append(module_id)
                if runtime.output_at:
                    generated.append(runtime.output_at)
        return render_total(sections, width), (max(generated) if generated else None), ids

    def _printer_snapshot_locked(self, config: dict[str, Any]) -> dict[str, Any]:
        return {
            **config,
            "last_print_at": self._printer_last_print_at,
            "last_error": self._printer_last_error,
            "last_job": self._printer_last_job,
        }

    def _printer_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._printer_snapshot_locked(
                self.config.snapshot()["printer"]
            )

    # -- helpers -------------------------------------------------------
    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), timezone.utc)

    def _iso_now(self) -> str:
        return self._now().isoformat(timespec="seconds")

    def _log(self, message: str, level: str = "info", module: str | None = None) -> None:
        logging.getLogger("gpta.service").log(
            {"error": logging.ERROR, "warning": logging.WARNING}.get(
                level, logging.INFO
            ),
            "%s%s",
            f"[{module}] " if module else "",
            message,
            extra={"gpta_module": module},
        )

    @staticmethod
    def _version() -> str:
        from . import __version__

        return __version__
