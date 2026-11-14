"""The print queue: every physical print waits here, and one thread prints.

:meth:`Printer.submit` adds a job and returns at once. A single dispatcher
thread takes the job at the head of the queue and prints it, unless
something holds the queue (see :meth:`Printer._plan`):

- printing is paused (the panic switch; it survives a restart),
- the printer is disabled, by the operator or after repeated failures,
- the head job is longer than ``printer.warn_length_mm`` and has not been
  approved,
- the hourly cap is reached, or an automatic print must wait out the
  minimum spacing.

Jobs live in the database (:mod:`.jobs`), so the queue survives a restart.
Device access itself lives in :mod:`.transport`.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

from PIL import Image

from ..config import ConfigError, ConfigStore
from ..storage import Database
from . import jobs
from .jobs import JobStore, QueueFull
from .transport import PrinterError, PrinterWriter, PrintStopped

log = logging.getLogger("gpta.printing")

# Long prints get extra time on top of ``write_timeout_seconds``: one second
# per this many dots of image height (8 dots per mm, so a slow 25 mm/s).
TIMEOUT_DOTS_PER_SECOND = 200
# Longest the dispatcher sleeps when nothing wakes it; bounds how late an
# expiry or a freed hourly slot is noticed.
IDLE_WAIT_SECONDS = 30.0


class PrinterDisabled(Exception):
    """Printing (or auto-printing) is switched off in the config."""


class AutoPrintOff(PrinterDisabled):
    """The printer is on, but ``printer.auto_print`` is off."""


class PrinterRateLimited(PrinterError):
    """A print was refused: the queue is full."""


def _length(mm: int) -> str:
    return f"{mm / 1000:.1f} m" if mm >= 1000 else f"{max(1, round(mm / 10))} cm"


class Printer:
    def __init__(
        self,
        config: ConfigStore,
        db: Database | None = None,
        *,
        writer: PrinterWriter | None = None,
        clock=time.time,
        monotonic=time.monotonic,
    ):
        self._config = config
        self.jobs = JobStore(db if db is not None else Database(None))
        self._writer = writer or PrinterWriter()
        self._clock = clock
        self._monotonic = monotonic
        #: Called as ``on_result(job, error)`` when a job ends on the device;
        #: ``error`` is None for a completed print.
        self.on_result = None
        self._lock = threading.Lock()
        self._last_print_mono: float | None = None
        self._last_print_at: str | None = None
        self._last_error: str | None = None
        self._last_job: dict[str, Any] | None = None
        self._disabled_reason: str | None = None
        self._consecutive_failures = 0
        # The job on the device, and the event that stops it.
        self._current: tuple[int, threading.Event] | None = None
        # Dispatcher thread state, guarded by the condition.
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._woken = False
        self._stopping = False
        self._settled = threading.Event()
        self._settled.set()
        recovered = self.jobs.recover(self._clock())
        if recovered:
            log.warning("a print was interrupted by a restart; it is not reprinted")

    # -- submitting ----------------------------------------------------
    def submit(
        self,
        image: Image.Image,
        source: str,
        *,
        auto: bool = False,
        manual: bool = False,
        approved: bool = False,
    ) -> dict[str, Any]:
        """Queue a rendered receipt ``image`` and return its job.

        ``source`` is a module id, ``"instant"``, ``"reader"`` or ``"test"``.
        ``auto`` marks a module's own print: it needs ``printer.auto_print``.
        ``manual`` marks a print the operator asked for (Run now, Instant,
        Test print): it does not wait for the minimum spacing. A scheduled
        run's job replaces the module's earlier job if that is still
        waiting. ``approved`` says the operator has already confirmed the
        length, so the job is not held at the warning limit.

        Raises :class:`PrinterDisabled` (or its :class:`AutoPrintOff`), or
        :class:`PrinterRateLimited` when the queue is full.
        """
        cfg = self._config.snapshot()["printer"]
        if not cfg["enabled"]:
            raise PrinterDisabled("printer is disabled")
        if auto and not cfg["auto_print"]:
            raise AutoPrintOff("auto print is off")
        try:
            job = self.jobs.add(
                image,
                source,
                "manual" if manual else "auto",
                approved=approved,
                now=self._clock(),
                replace=auto and not manual,
            )
        except QueueFull as exc:
            raise PrinterRateLimited(str(exc)) from exc
        self.wake()
        return self._public(job, cfg)

    # -- operator controls ---------------------------------------------
    def pause(self) -> None:
        """Start no more jobs until :meth:`resume` (kept across restarts)."""
        self.jobs.set_paused(True)
        log.warning("printing paused")
        self.wake()

    def resume(self) -> None:
        self.jobs.set_paused(False)
        log.info("printing resumed")
        self.wake()

    def approve(self, job_id: int) -> bool:
        """Let a job longer than the warning limit print."""
        done = self.jobs.approve(job_id)
        self.wake()
        return done

    def cancel(self, job_id: int) -> bool:
        """Cancel a waiting job, or stop the one that is printing (as soon
        as the device allows; what is already on paper stays)."""
        if self.jobs.cancel(job_id, self._clock()):
            self.wake()
            return True
        with self._lock:
            current = self._current
        if current is not None and current[0] == job_id:
            current[1].set()
            return True
        return False

    def clear(self) -> int:
        """Cancel every waiting job; returns how many."""
        count = self.jobs.cancel_all(self._clock())
        self.wake()
        return count

    def move(self, job_id: int, to: str) -> bool:
        done = self.jobs.move(job_id, to)
        self.wake()
        return done

    def retry(self, job_id: int) -> bool:
        """Queue a failed job again."""
        try:
            done = self.jobs.retry(job_id, self._clock())
        except QueueFull as exc:
            raise PrinterRateLimited(str(exc)) from exc
        self.wake()
        return done

    def reset_failures(self) -> None:
        """Clear the failure count, e.g. after the operator re-enables printing."""
        with self._lock:
            self._consecutive_failures = 0
            self._disabled_reason = None

    # -- status --------------------------------------------------------
    def status(self) -> dict[str, Any]:
        cfg = self._config.snapshot()["printer"]
        pending = self.jobs.pending()
        _job, reason, _wait = self._plan(cfg, pending)
        with self._lock:
            return {
                "last_print_at": self._last_print_at,
                "last_error": self._last_error,
                "last_job": self._last_job,
                "circuit_open": self._disabled_reason is not None,
                "disabled_reason": self._disabled_reason,
                "queue": {
                    "pending": len(pending),
                    "printing": self._current is not None,
                    "paused": self.jobs.paused(),
                    "blocked": reason,
                },
            }

    def queue(self, history: int = 50) -> dict[str, Any]:
        """The queue as the console shows it."""
        cfg = self._config.snapshot()["printer"]
        pending = self.jobs.pending()
        _job, reason, _wait = self._plan(cfg, pending)
        current = self.jobs.printing()
        return {
            "paused": self.jobs.paused(),
            "blocked": reason,
            "warn_length_mm": cfg["warn_length_mm"],
            "current": self._public(current, cfg) if current else None,
            "pending": [self._public(job, cfg) for job in pending],
            "history": [self._public(job, cfg) for job in self.jobs.history(history)],
        }

    def job_png(self, job_id: int) -> bytes | None:
        return self.jobs.png(job_id)

    @staticmethod
    def _public(job: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in job.items() if k not in ("position", "created_ts")}
        out["needs_approval"] = job["state"] == jobs.PENDING and _held(job, cfg)
        out["can_retry"] = job["state"] == jobs.FAILED
        return out

    # -- dispatcher ----------------------------------------------------
    def start(self) -> None:
        """Start the dispatcher (also started by the first submit)."""
        with self._cond:
            if self._stopping or (self._thread and self._thread.is_alive()):
                return
            self._thread = threading.Thread(
                target=self._loop, name="gpta-print-queue", daemon=True
            )
            self._thread.start()

    def wake(self) -> None:
        """Have the dispatcher look at the queue again (after any change)."""
        with self._cond:
            self._settled.clear()
            self._woken = True
            self._cond.notify_all()
        self.start()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Wait until the dispatcher has nothing it can print right now."""
        return self._settled.wait(timeout)

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
            thread = self._thread
        with self._lock:
            current = self._current
        if current is not None:
            current[1].set()
        if thread is not None:
            thread.join(timeout=1.0)
        self._settled.set()
        self._writer.stop()

    def _loop(self) -> None:
        while True:
            with self._cond:
                if self._stopping:
                    return
                self._woken = False
            try:
                wait = self._step()
            except Exception:  # pragma: no cover - defensive
                if self._stopping:
                    return
                log.exception("print queue error")
                wait = IDLE_WAIT_SECONDS
            if wait == 0:
                continue
            with self._cond:
                if self._stopping:
                    return
                if not self._woken:
                    self._settled.set()
                    self._cond.wait(wait)

    def _step(self) -> float:
        """Print the next job if one may print; returns how long to sleep
        (0 to look again at once)."""
        cfg = self._config.snapshot()["printer"]
        now = self._clock()
        expired = self.jobs.expire(now - cfg["job_expiry_hours"] * 3600, now)
        if expired:
            log.warning("%d print job(s) expired in the queue", expired)
        job, _reason, wait = self._plan(cfg, self.jobs.pending())
        if job is None:
            return wait
        self._print(job, cfg)
        return 0

    def _plan(
        self, cfg: dict[str, Any], pending: list[dict[str, Any]]
    ) -> tuple[dict[str, Any] | None, str | None, float]:
        """What the dispatcher should do: ``(job, None, 0)`` to print the
        head job, or ``(None, reason, seconds)`` to wait. ``reason`` is None
        when the queue is simply empty."""
        if self.jobs.paused():
            return None, "printing is paused", IDLE_WAIT_SECONDS
        if not pending:
            return None, None, IDLE_WAIT_SECONDS
        if not cfg["enabled"]:
            with self._lock:
                why = self._disabled_reason
            return None, why or "the printer is disabled", IDLE_WAIT_SECONDS
        job = pending[0]
        if _held(job, cfg):
            return (
                None,
                f"job {job['id']} ({job['source']}) is {_length(job['length_mm'])}"
                f" long, over the {_length(cfg['warn_length_mm'])} limit;"
                " approve or cancel it",
                IDLE_WAIT_SECONDS,
            )
        limit = int(cfg["max_prints_per_hour"])
        now = self._clock()
        recent = self.jobs.started_since(now - 3600)
        if len(recent) >= limit:
            wait = max(1.0, recent[len(recent) - limit] + 3600 - now)
            return (
                None,
                f"hourly limit of {limit} prints reached",
                min(wait, IDLE_WAIT_SECONDS),
            )
        spacing = int(cfg["min_print_spacing_seconds"])
        with self._lock:
            last = self._last_print_mono
        if job["kind"] == "auto" and spacing > 0 and last is not None:
            remaining = spacing - (self._monotonic() - last)
            if remaining > 0:
                return (
                    None,
                    f"waiting {spacing}s between automatic prints",
                    min(remaining, IDLE_WAIT_SECONDS),
                )
        return job, None, 0

    def _print(self, job: dict[str, Any], cfg: dict[str, Any]) -> None:
        job_id = job["id"]
        image = self.jobs.image(job_id)
        if image is None or not self.jobs.start(job_id, self._clock()):
            return  # cancelled between the plan and now
        stop = threading.Event()
        with self._lock:
            self._current = (job_id, stop)
            self._last_print_mono = self._monotonic()
        error: str | None = None
        state = jobs.DONE
        try:
            timeout = (
                cfg["write_timeout_seconds"] + image.height / TIMEOUT_DOTS_PER_SECOND
            )
            # The transports read their deadline and the stop flag from here.
            job_cfg = {**cfg, "write_timeout_seconds": timeout, "stop": stop}
            self._writer.print(image, job_cfg, timeout=timeout)
        except PrintStopped:
            state, error = jobs.CANCELLED, "stopped while printing"
        except PrinterError as exc:
            if stop.is_set():
                state, error = jobs.CANCELLED, "stopped while printing"
            else:
                state, error = jobs.FAILED, str(exc)
        finally:
            with self._lock:
                self._current = None
        self.jobs.finish(job_id, state, self._clock(), error)
        if state == jobs.FAILED:
            self._record_failure(
                cfg, error or "", job["source"] if job["kind"] == "auto" else None
            )
        elif state == jobs.DONE:
            self._record_success(job["source"], image)
        else:
            log.warning("print job %d stopped", job_id)
        callback = self.on_result
        if callback is not None and state != jobs.CANCELLED:
            try:
                callback(job, error)
            except Exception:  # pragma: no cover - defensive
                log.exception("print result callback failed")

    # -- internals -----------------------------------------------------
    def _record_success(self, source: str, image: Image.Image) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._disabled_reason = None
            self._last_print_at = self._iso_now()
            self._last_error = None
            self._last_job = {
                "module": source,
                "width_px": image.width,
                "height_px": image.height,
            }

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
        return datetime.fromtimestamp(self._clock(), UTC).isoformat(timespec="seconds")


def _held(job: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """Whether ``job`` is over the warning limit and not yet approved."""
    return not job["approved"] and job["length_mm"] > cfg["warn_length_mm"]
