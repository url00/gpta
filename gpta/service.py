"""Application facade used by the HTTP layer.

Wires together the module runner, scheduler, output store, and printer, and
builds the ``/api/state`` snapshot.

Lock order: runner → config. The printer and output store take their own
locks only around their own state and never call out while holding them.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigStore
from .errors import NotFound
from .http import HttpClient
from .logbus import LogBus
from .module_api import Module
from .outputs import OutputStore
from .printing import Printer, PrinterWriter, test_receipt
from .registry import describe
from .runner import ModuleRunner, module_log
from .scheduler import Scheduler


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
        width = self.config.snapshot()["printer"]["width"]
        lines = test_receipt(width, datetime.fromtimestamp(self.clock(), UTC))
        self.printer.submit(lines, "test", manual=True)
        return {"ok": True, "lines": lines}

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
        if key == "total":
            return self._total(self.config.snapshot())["text"]
        if key not in self.registry:
            raise NotFound(f"unknown output: {key}")
        output = self.outputs.get(key)
        return output.text if output else ""

    def _total(self, config: dict[str, Any]) -> dict[str, Any]:
        enabled = [
            module_id
            for module_id in sorted(self.registry)
            if config["modules"][module_id]["enabled"]
        ]
        return self.outputs.total(enabled, int(config["printer"]["width"]))

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
        return {
            **self._descriptions[module_id],
            "enabled": cfg["enabled"],
            "interval_seconds": cfg["interval_seconds"],
            "limits": dict(cfg["limits"]),
            "options": dict(cfg["options"]),
            "runtime": runtime,
            "output": {
                "lines": list(output.lines) if output else [],
                "text": output.text if output else "",
                "generated_at": output.generated_at if output else None,
            },
        }

    def _printer_snapshot(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        if config is None:
            config = self.config.snapshot()["printer"]
        return {**config, **self.printer.status()}
