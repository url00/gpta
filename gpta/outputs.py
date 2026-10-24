"""Latest receipt per module, and the combined "total" receipt.

Receipts are kept as blocks and rendered on demand, so a paper size change
applies to existing output too. Images are cached per module and width.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass

from PIL import Image

from .renderer import (
    Receipt,
    render_image,
    render_text,
    render_total,
    render_total_image,
)


@dataclass(frozen=True)
class Output:
    receipt: Receipt
    generated_at: str

    def lines(self, columns: int) -> list[str]:
        return render_text(self.receipt, columns)

    def text(self, columns: int) -> str:
        lines = self.lines(columns)
        return "\n".join(lines) + "\n" if lines else ""


class OutputStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, Output] = {}
        self._images: dict[str, tuple[Output, int, Image.Image]] = {}

    def set(
        self,
        module_id: str,
        receipt: Receipt,
        generated_at: str,
        *,
        image: Image.Image | None = None,
    ) -> None:
        """Store a receipt, optionally with its already rendered image."""
        output = Output(receipt, generated_at)
        with self._lock:
            self._latest[module_id] = output
            if image is None:
                self._images.pop(module_id, None)
            else:
                self._images[module_id] = (output, image.width, image)

    def get(self, module_id: str) -> Output | None:
        with self._lock:
            return self._latest.get(module_id)

    def image(self, module_id: str, width_px: int) -> Image.Image | None:
        """The latest receipt of ``module_id`` rendered ``width_px`` dots wide."""
        with self._lock:
            output = self._latest.get(module_id)
            cached = self._images.get(module_id)
        if output is None:
            return None
        if cached and cached[0] is output and cached[1] == width_px:
            return cached[2]
        image = render_image(output.receipt, width_px)
        with self._lock:
            if self._latest.get(module_id) is output:
                self._images[module_id] = (output, width_px, image)
        return image

    def _sections(self, module_ids: Iterable[str]) -> list[tuple[str, Output]]:
        with self._lock:
            return [
                (module_id, self._latest[module_id])
                for module_id in module_ids
                if module_id in self._latest
            ]

    def total(self, module_ids: Iterable[str], columns: int) -> dict:
        """Combine the latest output of ``module_ids`` (in order) as text."""
        sections = self._sections(module_ids)
        lines = render_total(
            [output.lines(columns) for _id, output in sections], columns
        )
        generated = [output.generated_at for _id, output in sections]
        return {
            "lines": lines,
            "text": "\n".join(lines) + "\n",
            "generated_at": max(generated) if generated else None,
            "module_ids": [module_id for module_id, _output in sections],
        }

    def total_image(
        self, module_ids: Iterable[str], width_px: int
    ) -> Image.Image | None:
        """The receipts of ``module_ids`` stacked into one image, or None."""
        images = [
            self.image(module_id, width_px)
            for module_id, _ in self._sections(module_ids)
        ]
        return render_total_image([image for image in images if image is not None])
