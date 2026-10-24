"""Latest rendered receipt per module, and the combined "total" receipt."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass

from .renderer import render_total


@dataclass(frozen=True)
class Output:
    lines: tuple[str, ...]
    generated_at: str

    @property
    def text(self) -> str:
        return "\n".join(self.lines) + "\n" if self.lines else ""


class OutputStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, Output] = {}

    def set(self, module_id: str, lines: Iterable[str], generated_at: str) -> None:
        with self._lock:
            self._latest[module_id] = Output(tuple(lines), generated_at)

    def get(self, module_id: str) -> Output | None:
        with self._lock:
            return self._latest.get(module_id)

    def total(self, module_ids: Iterable[str], width: int) -> dict:
        """Combine the latest output of ``module_ids`` (in order) into one receipt."""
        with self._lock:
            sections = [
                (module_id, self._latest[module_id])
                for module_id in module_ids
                if module_id in self._latest and self._latest[module_id].lines
            ]
        lines = render_total(
            [(module_id, output.lines) for module_id, output in sections], width
        )
        generated = [output.generated_at for _module_id, output in sections]
        return {
            "lines": lines,
            "text": "\n".join(lines) + "\n",
            "generated_at": max(generated) if generated else None,
            "module_ids": [module_id for module_id, _output in sections],
        }
