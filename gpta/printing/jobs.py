"""The print queue's records: the ``print_jobs`` table.

A job is one rendered receipt image waiting to print, printing, or finished.
This file only stores and orders jobs; deciding when one may print is
:class:`gpta.printing.printer.Printer`'s job.

States: ``pending`` -> ``printing`` -> ``done`` or ``failed``; a pending job
can also end ``cancelled`` or ``expired``, and a printing one ``cancelled``
when it is stopped. Finished jobs are kept, without their image (except
failed ones, so they can be retried), as the print history.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Any

from PIL import Image

from ..storage import Database

#: Printer dots per mm (203 dpi).
DOTS_PER_MM = 8
#: Most jobs that may wait at once; more are refused.
MAX_PENDING = 50
#: Finished jobs kept as history.
HISTORY_KEPT = 200

PENDING, PRINTING, DONE = "pending", "printing", "done"
FAILED, CANCELLED, EXPIRED = "failed", "cancelled", "expired"
MOVES = ("top", "up", "down", "bottom")

_COLUMNS = (
    "id, source, kind, state, position, approved, width_px, height_px,"
    " error, created_at, started_at, finished_at"
)


class QueueFull(Exception):
    """There are already :data:`MAX_PENDING` jobs waiting."""


def length_mm(height_px: int) -> int:
    """Paper a job ``height_px`` dots tall uses, in mm."""
    return max(1, round(height_px / DOTS_PER_MM))


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def _job(row: tuple) -> dict[str, Any]:
    (
        job_id,
        source,
        kind,
        state,
        position,
        approved,
        width,
        height,
        error,
        created,
        started,
        finished,
    ) = row
    return {
        "id": job_id,
        "source": source,
        "kind": kind,
        "state": state,
        "position": position,
        "approved": bool(approved),
        "width_px": width,
        "height_px": height,
        "length_mm": length_mm(height),
        "error": error,
        "created_ts": created,
        "created_at": _iso(created),
        "started_at": _iso(started),
        "finished_at": _iso(finished),
    }


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class JobStore:
    def __init__(self, db: Database):
        self.db = db

    # -- reading -------------------------------------------------------
    def _select(self, where: str, args: tuple = (), tail: str = "") -> list[dict]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM print_jobs WHERE {where} {tail}", args
            ).fetchall()
        return [_job(row) for row in rows]

    def get(self, job_id: int) -> dict[str, Any] | None:
        jobs = self._select("id=?", (job_id,))
        return jobs[0] if jobs else None

    def pending(self) -> list[dict[str, Any]]:
        """Waiting jobs, next to print first."""
        return self._select("state=?", (PENDING,), "ORDER BY position, id")

    def printing(self) -> dict[str, Any] | None:
        jobs = self._select("state=?", (PRINTING,), "ORDER BY id LIMIT 1")
        return jobs[0] if jobs else None

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Finished jobs, newest first."""
        return self._select(
            "state NOT IN (?, ?)",
            (PENDING, PRINTING),
            f"ORDER BY finished_at DESC, id DESC LIMIT {int(limit)}",
        )

    def png(self, job_id: int) -> bytes | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT image FROM print_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return bytes(row[0]) if row and row[0] is not None else None

    def image(self, job_id: int) -> Image.Image | None:
        data = self.png(job_id)
        if data is None:
            return None
        image = Image.open(io.BytesIO(data))
        image.load()
        return image

    def started_since(self, ts: float) -> list[float]:
        """Start times of the prints begun at or after ``ts``, oldest first."""
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT started_at FROM print_jobs WHERE started_at>=?"
                " ORDER BY started_at",
                (ts,),
            ).fetchall()
        return [row[0] for row in rows]

    # -- adding --------------------------------------------------------
    def add(
        self,
        image: Image.Image,
        source: str,
        kind: str,
        *,
        approved: bool,
        now: float,
        replace: bool = False,
    ) -> dict[str, Any]:
        """Queue ``image`` last. With ``replace``, waiting automatic jobs
        from the same ``source`` are cancelled: only the newest receipt of a
        module is worth printing. Raises :class:`QueueFull`."""
        data = _png(image)
        with self.db.transaction() as conn:
            if replace:
                conn.execute(
                    "UPDATE print_jobs SET state=?, error=?, finished_at=?, image=NULL"
                    " WHERE state=? AND source=? AND kind='auto'",
                    (CANCELLED, "replaced by a newer receipt", now, PENDING, source),
                )
            count, last = conn.execute(
                "SELECT count(*), max(position) FROM print_jobs WHERE state=?",
                (PENDING,),
            ).fetchone()
            if count >= MAX_PENDING:
                raise QueueFull(f"the print queue is full ({MAX_PENDING} jobs)")
            cursor = conn.execute(
                "INSERT INTO print_jobs (source, kind, state, position, approved,"
                " width_px, height_px, image, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source,
                    kind,
                    PENDING,
                    (last or 0.0) + 1.0,
                    int(approved),
                    image.width,
                    image.height,
                    data,
                    now,
                ),
            )
            job_id = cursor.lastrowid
        job = self.get(job_id)
        assert job is not None
        return job

    # -- changing ------------------------------------------------------
    def start(self, job_id: int, now: float) -> bool:
        """Mark a pending job as printing; False if it is no longer pending."""
        with self.db.transaction() as conn:
            done = conn.execute(
                "UPDATE print_jobs SET state=?, started_at=? WHERE id=? AND state=?",
                (PRINTING, now, job_id, PENDING),
            ).rowcount
        return bool(done)

    def finish(
        self, job_id: int, state: str, now: float, error: str | None = None
    ) -> None:
        """End a printing job. A failed job keeps its image for a retry."""
        keep = "image" if state == FAILED else "NULL"
        with self.db.transaction() as conn:
            conn.execute(
                f"UPDATE print_jobs SET state=?, error=?, finished_at=?, image={keep}"
                " WHERE id=? AND state=?",
                (state, error, now, job_id, PRINTING),
            )
            conn.execute(
                "DELETE FROM print_jobs WHERE state NOT IN (?, ?) AND id NOT IN ("
                " SELECT id FROM print_jobs WHERE state NOT IN (?, ?)"
                " ORDER BY finished_at DESC, id DESC LIMIT ?)",
                (PENDING, PRINTING, PENDING, PRINTING, HISTORY_KEPT),
            )

    def cancel(self, job_id: int, now: float, error: str = "cancelled") -> bool:
        """Cancel a pending job; False if it is not pending."""
        with self.db.transaction() as conn:
            done = conn.execute(
                "UPDATE print_jobs SET state=?, error=?, finished_at=?, image=NULL"
                " WHERE id=? AND state=?",
                (CANCELLED, error, now, job_id, PENDING),
            ).rowcount
        return bool(done)

    def cancel_all(self, now: float) -> int:
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE print_jobs SET state=?, error='cancelled', finished_at=?,"
                " image=NULL WHERE state=?",
                (CANCELLED, now, PENDING),
            ).rowcount

    def expire(self, cutoff: float, now: float) -> int:
        """End pending jobs queued before ``cutoff``."""
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE print_jobs SET state=?, error=?, finished_at=?, image=NULL"
                " WHERE state=? AND created_at<?",
                (EXPIRED, "waited too long in the queue", now, PENDING, cutoff),
            ).rowcount

    def recover(self, now: float) -> int:
        """After a restart: a job left ``printing`` may be half on paper, so
        it is failed rather than printed again."""
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE print_jobs SET state=?, error=?, finished_at=? WHERE state=?",
                (FAILED, "interrupted by a restart", now, PRINTING),
            ).rowcount

    def approve(self, job_id: int) -> bool:
        with self.db.transaction() as conn:
            done = conn.execute(
                "UPDATE print_jobs SET approved=1 WHERE id=? AND state=?",
                (job_id, PENDING),
            ).rowcount
        return bool(done)

    def retry(self, job_id: int, now: float) -> bool:
        """Queue a failed job again, last; False if it cannot be retried."""
        with self.db.transaction() as conn:
            count, last = conn.execute(
                "SELECT count(*), max(position) FROM print_jobs WHERE state=?",
                (PENDING,),
            ).fetchone()
            if count >= MAX_PENDING:
                raise QueueFull(f"the print queue is full ({MAX_PENDING} jobs)")
            done = conn.execute(
                "UPDATE print_jobs SET state=?, error=NULL, position=?, created_at=?,"
                " started_at=NULL, finished_at=NULL"
                " WHERE id=? AND state=? AND image IS NOT NULL",
                (PENDING, (last or 0.0) + 1.0, now, job_id, FAILED),
            ).rowcount
        return bool(done)

    def move(self, job_id: int, to: str) -> bool:
        """Reorder a pending job: ``top``, ``up``, ``down`` or ``bottom``."""
        if to not in MOVES:
            raise ValueError(f"move must be one of {', '.join(MOVES)}")
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT id, position FROM print_jobs WHERE state=?"
                " ORDER BY position, id",
                (PENDING,),
            ).fetchall()
            ids = [row[0] for row in rows]
            if job_id not in ids:
                return False
            index = ids.index(job_id)
            ids.pop(index)
            target = {
                "top": 0,
                "up": max(0, index - 1),
                "down": min(len(ids), index + 1),
                "bottom": len(ids),
            }[to]
            ids.insert(target, job_id)
            for position, each in enumerate(ids, start=1):
                conn.execute(
                    "UPDATE print_jobs SET position=? WHERE id=?",
                    (float(position), each),
                )
        return True

    # -- settings ------------------------------------------------------
    def _flag(self, key: str) -> bool:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT value FROM host_settings WHERE key=?", (key,)
            ).fetchone()
        return bool(row) and row[0] == "1"

    def _set_flag(self, key: str, on: bool) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO host_settings VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value=excluded.value",
                (key, "1" if on else "0"),
            )

    def paused(self) -> bool:
        return self._flag("print_paused")

    def set_paused(self, paused: bool) -> None:
        self._set_flag("print_paused", paused)

    def double_spaced(self) -> bool:
        return self._flag("double_spaced")

    def set_double_spaced(self, on: bool) -> None:
        self._set_flag("double_spaced", on)
