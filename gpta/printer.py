"""ESC/POS printer transport over a Linux device node."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from .renderer import ascii_safe, to_escpos


class PrinterError(Exception):
    pass


def send_to_device(lines: Sequence[str], config: dict[str, Any]) -> None:
    device = config.get("device", "")
    if not device:
        raise PrinterError("no printer device configured")
    data = to_escpos(
        lines,
        encoding=config.get("encoding", "cp437"),
        feed_lines=int(config.get("feed_lines", 3)),
        cut=bool(config.get("cut", True)),
    )
    try:
        with open(device, "wb", buffering=0) as handle:
            handle.write(data)
            handle.flush()
    except OSError as exc:
        raise PrinterError(f"failed to write to {device}: {exc}") from exc


def test_receipt(width: int = 32) -> list[str]:
    width = max(1, int(width))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return [
        ascii_safe("GPTA PRINTER TEST").center(width),
        "-" * width,
        "",
        "If you can read this,",
        "the printer transport works.",
        "",
        stamp.center(width),
        "-" * width,
    ]
