"""ESC/POS printer transport: a Linux device node or a raw CUPS queue.

All physical writes go through :class:`PrinterWriter`. A single daemon thread
owns the printer, which serialises writes from the scheduler and HTTP threads.
Callers wait for completion with a deadline, so a jammed printer degrades to a
``PrinterError`` instead of blocking the request or scheduler thread forever.

Receipts are printed as raster images (see :mod:`.escpos`). With
``printer.cups_queue`` set, the ESC/POS bytes go to that queue through
``lp -o raw`` (CUPS's client tools, so no Python dependency). This is the way
to print when CUPS owns the USB printer and there is no ``/dev/usb/lp*``.
"""

from __future__ import annotations

import os
import queue
import re
import stat
import subprocess
import threading
import time
from typing import Any

from PIL import Image

from .escpos import to_escpos


class PrinterError(Exception):
    pass


CUPS_COMMAND_TIMEOUT = 10
CUPS_POLL_SECONDS = 0.5


def _escpos_bytes(image: Image.Image, config: dict[str, Any]) -> bytes:
    return to_escpos(
        image,
        feed_lines=int(config.get("feed_lines", 0)),
        cut=bool(config.get("cut", True)),
    )


def send_to_device(image: Image.Image, config: dict[str, Any]) -> None:
    """Print ``image`` on the configured printer (CUPS queue or device)."""
    if config.get("cups_queue"):
        _send_to_cups(image, config)
    else:
        _send_to_device_node(image, config)


def _run(args: list[str], data: bytes | None = None) -> str:
    try:
        result = subprocess.run(
            args,
            input=data,
            capture_output=True,
            timeout=CUPS_COMMAND_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PrinterError(
            f"{args[0]} not found; install the CUPS client tools"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PrinterError(
            f"{args[0]} did not finish within {CUPS_COMMAND_TIMEOUT}s"
        ) from exc
    output = result.stdout.decode("utf-8", "replace")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip() or output.strip()
        raise PrinterError(f"{args[0]} failed: {detail}")
    return output


def _queue_stopped(queue_name: str) -> str | None:
    """The reason a CUPS queue is not printing, or None if it is accepting."""
    status = _run(["lpstat", "-p", queue_name])
    if re.search(r"\bdisabled\b", status):
        return " ".join(status.split())
    return None


def _send_to_cups(image: Image.Image, config: dict[str, Any]) -> None:
    queue_name = str(config["cups_queue"])
    # Finish (or cancel) before the writer's deadline so the caller gets
    # this function's error message rather than a generic timeout.
    deadline = time.monotonic() + max(
        1.0, float(config.get("write_timeout_seconds", 30)) - 1.0
    )
    stopped = _queue_stopped(queue_name)
    if stopped:
        raise PrinterError(f"CUPS queue {queue_name} is stopped: {stopped}")
    output = _run(
        ["lp", "-d", queue_name, "-o", "raw", "-t", "gpta", "-"],
        _escpos_bytes(image, config),
    )
    match = re.search(r"request id is (\S+)", output)
    if not match:
        raise PrinterError(f"unexpected lp output: {output.strip()!r}")
    job_id = match.group(1)
    while True:
        pending = _run(["lpstat", "-o", queue_name]).split()
        if job_id not in pending:
            break
        if time.monotonic() >= deadline:
            # Never let a job the caller was told failed print later.
            try:
                _run(["cancel", job_id])
            except PrinterError:
                pass
            raise PrinterError(f"CUPS job {job_id} not finished in time; cancelled it")
        time.sleep(CUPS_POLL_SECONDS)
    stopped = _queue_stopped(queue_name)
    if stopped:
        raise PrinterError(f"CUPS job {job_id} failed; queue stopped: {stopped}")


def _send_to_device_node(image: Image.Image, config: dict[str, Any]) -> None:
    device = config.get("device", "")
    if not device:
        raise PrinterError("no printer device configured")
    if not device.startswith("/dev/"):
        raise PrinterError(f"refusing to write outside /dev: {device}")
    resolved = os.path.realpath(device)
    if not resolved.startswith("/dev/"):
        raise PrinterError(f"refusing to follow {device} outside /dev")
    data = _escpos_bytes(image, config)
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
    __slots__ = ("image", "config", "done", "error", "state", "lock")

    def __init__(self, image: Image.Image, config: dict[str, Any]):
        self.image = image
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
        image: Image.Image,
        config: dict[str, Any],
        *,
        timeout: float,
    ) -> None:
        job = _PrintJob(image, config)
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
                send_to_device(job.image, job.config)
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
