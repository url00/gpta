"""Background scheduler: dispatches due module runs on their own threads.

Due-ness uses a monotonic clock, so wall-clock jumps cannot cause run storms.
Runs are dispatched asynchronously, so a slow module cannot delay others.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .config import ConfigStore
from .errors import AlreadyRunning, ModuleStuck
from .runner import ModuleRunner

log = logging.getLogger("gpta.scheduler")

TICK_SECONDS = 1.0
STARTUP_DELAY_SECONDS = 2.0
STALE_SECONDS = 10


class Scheduler:
    def __init__(
        self,
        config: ConfigStore,
        runner: ModuleRunner,
        *,
        clock=time.time,
        monotonic=time.monotonic,
    ):
        self.config = config
        self.runner = runner
        self.clock = clock
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_threads: list[threading.Thread] = []
        self._last_tick_mono: float | None = None
        self._last_tick_at: str | None = None

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

    def tick(self) -> None:
        """Dispatch every enabled module whose interval has elapsed."""
        now = self.monotonic()
        with self._lock:
            self._last_tick_mono = now
            self._last_tick_at = datetime.fromtimestamp(
                self.clock(), timezone.utc
            ).isoformat(timespec="seconds")
        for module_id in sorted(self.runner.registry):
            cfg = self.config.module_config(module_id)
            if not self.runner.is_due(module_id, cfg, now):
                continue
            try:
                ticket = self.runner.begin(module_id)
            except (AlreadyRunning, ModuleStuck):
                continue
            thread = threading.Thread(
                target=self.runner.execute,
                args=(ticket, "schedule"),
                name=f"gpta-run-{module_id}",
                daemon=True,
            )
            with self._lock:
                self._run_threads = [t for t in self._run_threads if t.is_alive()]
                self._run_threads.append(thread)
            thread.start()

    def wait_for_idle(self, timeout: float = 5.0) -> bool:
        """Wait for runs dispatched by :meth:`tick` to finish."""
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

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
            age = (
                None
                if self._last_tick_mono is None
                else max(0, int(self.monotonic() - self._last_tick_mono))
            )
            return {
                "scheduler_running": running,
                "last_tick_at": self._last_tick_at,
                "last_tick_age_seconds": age,
                "scheduler_stale": bool(
                    running and age is not None and age > STALE_SECONDS
                ),
            }

    def _loop(self) -> None:
        if self._stop_event.wait(STARTUP_DELAY_SECONDS):
            return
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:  # pragma: no cover - tick already isolates modules
                log.exception("scheduler tick failed")
            if self._stop_event.wait(TICK_SECONDS):
                return
