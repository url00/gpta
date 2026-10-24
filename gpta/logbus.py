"""Thread-safe in-memory log ring shared by the UI and modules."""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any


class LogBus:
    def __init__(self, maxlen: int = 1000):
        self._entries: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, level: str, message: str, module: str | None = None) -> int:
        with self._lock:
            self._seq += 1
            seq = self._seq
            entry = {
                "seq": seq,
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                "level": level,
                "module": module,
                "message": message,
            }
            self._entries.append(entry)
            return seq

    def since(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._entries if e["seq"] > seq]

    def recent(self, n: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            if n <= 0:
                return []
            return list(self._entries)[-n:]


class BusLogHandler(logging.Handler):
    """Mirror stdlib log records into a LogBus."""

    def __init__(self, bus: LogBus):
        super().__init__()
        self.bus = bus
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.bus.emit(
                record.levelname.lower(),
                self.format(record),
                getattr(record, "gpta_module", None),
            )
        except Exception:  # pragma: no cover - never break logging
            self.handleError(record)
