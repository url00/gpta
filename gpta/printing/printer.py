"""Print policy: enabled checks, rate limits, failure cut-off, and status.

Every physical print goes through :meth:`Printer.submit`, whether it comes
from a module's auto-print or the test page, so the same guards apply to all
of them. Device access itself lives in :mod:`.transport`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Sequence

from ..config import ConfigError, ConfigStore
from ..renderer import ascii_safe
from .transport import PrinterError, PrinterWriter

log = logging.getLogger("gpta.printing")


class PrinterDisabled(Exception):
    """Printing (or auto-printing) is switched off in the config."""


class PrinterRateLimited(PrinterError):
    """A physical print was suppressed by the rate limiter."""


class Printer:
    def __init__(
        self,
        config: ConfigStore,
        *,
        writer: PrinterWriter | None = None,
        clock=time.time,
        monotonic=time.monotonic,
    ):
        self._config = config
        self._writer = writer or PrinterWriter()
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._print_times: deque[float] = deque()
        self._last_print_mono: float | None = None
        self._last_print_at: str | None = None
        self._last_error: str | None = None
        self._last_job: dict[str, Any] | None = None
        self._disabled_reason: str | None = None
        self._consecutive_failures = 0

    def submit(self, lines: Sequence[str], source: str, *, auto: bool = False) -> None:
        """Print ``lines`` now, or raise why not.

        ``source`` is a module id or ``"test"``. ``auto`` marks a module's
        automatic print, which also needs ``printer.auto_print``.

        Raises :class:`PrinterDisabled`, :class:`PrinterRateLimited`, or
        :class:`PrinterError` for a device failure.
        """
        cfg = self._config.snapshot()["printer"]
        if not cfg["enabled"]:
            raise PrinterDisabled("printer is disabled")
        if auto and not cfg["auto_print"]:
            raise PrinterDisabled("auto print is off")
        reason = self._reserve(cfg)
        if reason is not None:
            raise PrinterRateLimited(f"print rate limited: {reason}")
        try:
            self._writer.print(lines, cfg, timeout=float(cfg["write_timeout_seconds"]))
        except PrinterError as exc:
            self._record_failure(cfg, str(exc), source if auto else None)
            raise
        self._record_success(source, lines)

    def reset_failures(self) -> None:
        """Clear the failure count, e.g. after the operator re-enables printing."""
        with self._lock:
            self._consecutive_failures = 0
            self._disabled_reason = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "last_print_at": self._last_print_at,
                "last_error": self._last_error,
                "last_job": self._last_job,
                "circuit_open": self._disabled_reason is not None,
                "disabled_reason": self._disabled_reason,
            }

    def stop(self) -> None:
        self._writer.stop()

    # -- internals -----------------------------------------------------
    def _reserve(self, cfg: dict[str, Any]) -> str | None:
        """Reserve a print slot, or return the reason it is blocked."""
        now = self._monotonic()
        with self._lock:
            cutoff = now - 3600
            while self._print_times and self._print_times[0] < cutoff:
                self._print_times.popleft()
            spacing = int(cfg["min_print_spacing_seconds"])
            if (
                spacing > 0
                and self._last_print_mono is not None
                and now - self._last_print_mono < spacing
            ):
                return f"minimum spacing is {spacing}s"
            limit = int(cfg["max_prints_per_hour"])
            if len(self._print_times) >= limit:
                return f"hourly limit of {limit} prints reached"
            self._print_times.append(now)
            self._last_print_mono = now
        return None

    def _record_success(self, source: str, lines: Sequence[str]) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._disabled_reason = None
            self._last_print_at = self._iso_now()
            self._last_error = None
            self._last_job = {"module": source, "lines": list(lines)}

    def _record_failure(
        self, cfg: dict[str, Any], message: str, module: str | None
    ) -> None:
        log.error("print failed: %s", message, extra={"gpta_module": module})
        with self._lock:
            self._consecutive_failures += 1
            failures = self._consecutive_failures
            self._last_error = message
            trip = failures >= int(cfg["failure_threshold"])
            if trip:
                self._disabled_reason = (
                    f"auto-disabled after {failures} consecutive device failures"
                )
        if not trip:
            return
        try:
            self._config.update_printer({"enabled": False})
        except ConfigError as exc:  # pragma: no cover - disk failure
            log.error("could not auto-disable printer: %s", exc)
        log.error(
            "printer auto-disabled after %d consecutive failures: %s",
            failures,
            message,
        )

    def _iso_now(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(
            timespec="seconds"
        )


def test_receipt(width: int = 32, now: datetime | None = None) -> list[str]:
    width = max(1, int(width))
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    return [
        ascii_safe("GPTA PRINTER TEST").center(width),
        "-" * width,
        "",
        "If you can read this,",
        "the printer transport works.",
        "",
        stamp.center(width),
        "-" * width,
    ]
