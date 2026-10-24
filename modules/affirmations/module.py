"""Daily affirmation module backed by a local curated list."""

from __future__ import annotations

import hashlib
from pathlib import Path

from gpta.module_api import Element, FetchError, Heading, Module, ModuleContext, Text


class AffirmationsModule(Module):
    id = "affirmations"
    name = "Daily Affirmation"
    description = "Prints one curated affirmation, stable for the current date."

    defaults = {
        "enabled": False,
        "interval_seconds": 86400,
        "limits": {"max_chars": 500},
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

    def fetch(self, ctx: ModuleContext) -> list[Element]:
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
        ctx.emit(f"selected affirmation {index + 1}/{len(lines)}")
        blocks: list[Element] = [
            Heading("Today's affirmation"),
            Text(lines[index], size="large", align="center"),
        ]
        if ctx.options.get("include_date", True):
            blocks.append(Text(f"For {day}", size="small", align="right"))
        return blocks
