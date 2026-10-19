"""Daily affirmation module backed by a local curated list."""

from __future__ import annotations

import hashlib
from pathlib import Path

from gpta.module_api import Block, FetchError, Module, ModuleContext


class AffirmationsModule(Module):
    id = "affirmations"
    name = "Daily Affirmation"
    description = "Prints one curated affirmation, stable for the current date."

    defaults = {
        "enabled": False,
        "interval_seconds": 86400,
        "limits": {"max_items": 1, "max_chars": 500},
        "options": {"include_date": True},
    }
    options_schema = {
        "include_date": {
            "type": "bool",
            "label": "Include the date",
        }
    }

    def __init__(self) -> None:
        self._data_file = Path(__file__).resolve().parent / "data" / "affirmations.txt"

    def fetch(self, ctx: ModuleContext) -> list[Block]:
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
        day = ctx.now.date().isoformat()
        index = int(hashlib.sha256(day.encode("utf-8")).hexdigest(), 16) % len(lines)
        body = [lines[index]]
        if ctx.options.get("include_date", True):
            body.append(f"For {day}")
        ctx.emit(f"selected affirmation {index + 1}/{len(lines)}")
        return [Block("TODAY'S AFFIRMATION", tuple(body))]
