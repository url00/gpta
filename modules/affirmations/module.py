"""Daily affirmation module backed by a local curated list.

Runs every few minutes. With "Auto print before work" on, the first run at
or after the global before work time prints the day's affirmation; every
other scheduled run raises ``Skip``. A manual run always prints.
"""

from __future__ import annotations

import hashlib
from datetime import tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from gpta.module_api import (
    Element,
    FetchError,
    Heading,
    Module,
    ModuleContext,
    Skip,
    Text,
)


def _zone(ctx: ModuleContext) -> tzinfo | None:
    name = str(ctx.options.get("timezone") or "").strip()
    if not name:
        return None  # the server's local time zone
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError) as exc:
        raise FetchError(f"unknown time zone {name!r}") from exc


class AffirmationsModule(Module):
    id = "affirmations"
    name = "Daily Affirmation"
    description = (
        "Prints one curated affirmation before work, stable for the current date."
    )

    defaults = {
        "enabled": False,
        "interval_seconds": 300,
        "limits": {"max_chars": 500},
        "options": {"before_work": True, "timezone": "", "include_date": True},
    }
    options_schema = {
        "before_work": {"type": "bool", "label": "Auto print before work"},
        "timezone": {
            "type": "str",
            "label": "Time zone, e.g. America/Chicago (blank = server's)",
            "max_length": 60,
        },
        "include_date": {
            "type": "bool",
            "label": "Include the date",
        },
    }

    def __init__(self) -> None:
        self._data_file = Path(__file__).resolve().parent / "data" / "affirmations.txt"

    def fetch(self, ctx: ModuleContext) -> list[Element]:
        local = ctx.now.astimezone(_zone(ctx))
        day = local.date().isoformat()
        manual = ctx.trigger == "manual"
        if not manual:
            if not ctx.options.get("before_work", True):
                raise Skip("auto print before work is off")
            if ctx.store.get("printed_day") == day:
                raise Skip("already printed today")
            if local.time() < ctx.before_work:
                raise Skip(f"waiting for {ctx.before_work:%H:%M}")
        try:
            raw = self._data_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise FetchError(f"cannot read affirmation list: {exc}") from exc
        lines = [
            line.strip()
            for line in raw.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not lines:
            raise FetchError("affirmation list is empty")
        index = int(hashlib.sha256(day.encode("utf-8")).hexdigest(), 16) % len(lines)
        ctx.emit(f"selected affirmation {index + 1}/{len(lines)}")
        blocks: list[Element] = [
            Heading("Today's affirmation"),
            Text(lines[index], size="large", align="center"),
        ]
        if ctx.options.get("include_date", True):
            blocks.append(Text(f"For {day}", size="small", align="right"))
        if not manual:  # a manual run does not stand in for the daily print
            ctx.store.set("printed_day", day)
        return blocks
