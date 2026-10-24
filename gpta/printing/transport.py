"""ESC/POS printer transport over a Linux device node.

All physical writes go through :class:`PrinterWriter`. A single daemon thread
owns the device, which serialises writes from the scheduler and HTTP threads.
Callers wait for completion with a deadline, so a jammed device degrades to a
``PrinterError`` instead of blocking the request or scheduler thread forever.
"""

from __future__ import annotations

import os
import queue
import stat
import threading
from typing import Any, Sequence

from ..renderer import to_escpos


class PrinterError(Exception):
    pass


def _escpos_bytes(lines: Sequence[str], config: dict[str, Any]) -> bytes:
    return to_escpos(
        lines,
        feed_lines=int(config.get("feed_lines", 3)),
        cut=bool(config.get("cut", True)),
    )


def send_to_device(lines: Sequence[str], config: dict[str, Any]) -> None:
    device = config.get("device", "")
    if not device:
        raise PrinterError("no printer device configured")
    if not device.startswith("/dev/"):
        raise PrinterError(f"refusing to write outside /dev: {device}")
    resolved = os.path.realpath(device)
    if not resolved.startswith("/dev/"):
        raise PrinterError(f"refusing to follow {device} outside /dev")
    data = _escpos_bytes(lines, config)
    try:
        fd = os.open(resolved, os.O_WRONLY | getattr(os, "O_NOCTTY", 0))
    except OSError as exc:
        raise PrinterError(f"failed to open {device}: {exc}") from exc
    try:
        try:
            info = os.fstat(fd)
        except OSError as exc:
            raise PrinterError(f"failed to stat {device}: {exc}") from exc
        if not stat.S_ISCHR(info.st_mode):
            raise PrinterError(f"{device} is not a character device")
        handle = os.fdopen(fd, "wb", buffering=0)
        fd = -1
        with handle:
            try:
                handle.write(data)
                handle.flush()
            except OSError as exc:
                raise PrinterError(f"failed to write to {device}: {exc}") from exc
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


class _PrintJob:
    __slots__ = ("lines", "config", "done", "error", "state", "lock")

    def __init__(self, lines: Sequence[str], config: dict[str, Any]):
        self.lines = tuple(lines)
        self.config = dict(config)
        self.done = threading.Event()
        self.error: PrinterError | None = None
        self.state = "queued"  # queued -> writing -> done, or queued -> cancelled
        self.lock = threading.Lock()


class PrinterWriter:
    """Serialize device writes on a single daemon thread."""

    def __init__(self) -> None:
        self._queue: queue.Queue[_PrintJob | None] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="gpta-printer", daemon=True
            )
            self._thread.start()

    def print(
        self,
        lines: Sequence[str],
        config: dict[str, Any],
        *,
        timeout: float,
    ) -> None:
        job = _PrintJob(lines, config)
        self._ensure_thread()
        self._queue.put(job)
        if not job.done.wait(timeout):
            with job.lock:
                if job.state == "queued":
                    # Never reached the device: drop it so it cannot print
                    # later, after the caller has already been told it failed.
                    job.state = "cancelled"
                    raise PrinterError(
                        f"printer busy; job cancelled after {timeout:g}s in queue"
                    )
            if not job.done.is_set():
                raise PrinterError(f"printer did not respond within {timeout:g}s")
        if job.error is not None:
            raise job.error

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            with job.lock:
                if job.state == "cancelled":
                    continue
                job.state = "writing"
            try:
                send_to_device(job.lines, job.config)
            except PrinterError as exc:
                job.error = exc
            except Exception as exc:  # pragma: no cover - defensive
                job.error = PrinterError(str(exc))
            finally:
                job.state = "done"
                job.done.set()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
        if thread is None or not thread.is_alive():
            return
        self._queue.put(None)
        thread.join(timeout=1.0)
