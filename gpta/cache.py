"""The download cache modules share: ``ctx.cache``.

Files under ``data/cache``, bounded by size and age. Anything in it may be
evicted, so it is for data that can be fetched again; state a module must
keep goes in ``ctx.store`` (:mod:`gpta.storage`).
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path


class Cache:
    """Bounded on-disk cache shared by modules.

    Entries are addressed by a relative key such as ``"gutenberg/11.txt"``.
    Writes are atomic; reads touch the file so the size-based eviction pass
    approximates LRU. Expired entries and oversized caches are pruned on every
    write. One instance is shared by all runs so its lock serialises eviction.
    """

    DEFAULT_MAX_BYTES = 50 * 1024 * 1024
    DEFAULT_MAX_AGE_SECONDS = 30 * 24 * 3600
    # Temp files older than this are leftovers from a crash, not live writes.
    STALE_TMP_SECONDS = 3600

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    ):
        self.root = Path(root)
        self.max_bytes = max(0, int(max_bytes))
        self.max_age_seconds = max(0, int(max_age_seconds))
        self._lock = threading.Lock()

    def path(self, key: str) -> Path:
        if not isinstance(key, str) or not key:
            raise ValueError("cache key must be a non-empty string")
        relative = Path(key)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError(f"unsafe cache key: {key!r}")
        return self.root / relative

    def read_text(self, key: str, encoding: str = "utf-8") -> str | None:
        path = self.path(key)
        try:
            text = path.read_text(encoding=encoding)
        except (OSError, UnicodeDecodeError):
            return None
        try:
            os.utime(path)
        except OSError:
            pass
        return text

    def write_text(self, key: str, text: str, encoding: str = "utf-8") -> None:
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # A unique temp name, so concurrent writes of one key cannot collide.
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding=encoding) as handle:
                handle.write(text)
            tmp.replace(path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self.evict()

    def evict(self) -> None:
        """Drop expired entries, then oldest entries until the size cap holds."""
        with self._lock:
            entries: list[tuple[float, int, Path]] = []
            total = 0
            now = time.time()
            if not self.root.is_dir():
                return
            for path in self.root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if path.name.endswith(".tmp"):
                    if now - stat.st_mtime > self.STALE_TMP_SECONDS:
                        try:
                            path.unlink()
                        except OSError:
                            pass
                    continue
                if self.max_age_seconds and now - stat.st_mtime > self.max_age_seconds:
                    try:
                        path.unlink()
                        continue
                    except OSError:
                        pass
                entries.append((stat.st_mtime, stat.st_size, path))
                total += stat.st_size
            if total <= self.max_bytes:
                return
            for _mtime, size, path in sorted(entries):
                if total <= self.max_bytes:
                    break
                try:
                    path.unlink()
                    total -= size
                except OSError:
                    continue
