"""Receipt layout shared by the simulator and the physical printer."""

from __future__ import annotations

import textwrap
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from .module_api import Block


def ascii_safe(text: object) -> str:
    """Convert arbitrary text to printable ASCII while keeping newlines."""
    if text is None:
        return ""
    value = str(text)
    value = (
        value.replace("—", "-")
        .replace("–", "-")
        .replace("‘", "'")
        .replace("’", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("…", "...")
    )
    value = "".join(
        ch
        for ch in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(ch)
    )
    value = value.encode("ascii", "replace").decode("ascii")
    # Keep newlines; any other control character (tabs included) becomes a space.
    return "".join(ch if ch == "\n" or 32 <= ord(ch) <= 126 else " " for ch in value)


def wrap_text(text: object, width: int) -> list[str]:
    width = max(1, int(width))
    safe = ascii_safe(text)
    lines: list[str] = []
    for paragraph in safe.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        wrapped = textwrap.wrap(
            paragraph,
            width=width,
            break_long_words=True,
            break_on_hyphens=False,
            replace_whitespace=True,
            drop_whitespace=True,
        )
        lines.extend(wrapped or [""])
    return lines


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _truncate(lines: list[str], max_chars: int | None) -> list[str]:
    if max_chars is None:
        return lines
    max_chars = max(0, int(max_chars))
    out: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) <= max_chars:
            out.append(line)
            used += len(line)
            continue
        remaining = max_chars - used
        if remaining > 0:
            out.append(line[:remaining])
        out.append("[truncated]")
        return out
    return out


def render_receipt(
    module_name: str,
    blocks: Sequence[Block],
    width: int,
    max_chars: int | None = None,
    now: datetime | None = None,
) -> list[str]:
    width = max(1, int(width))
    lines: list[str] = []
    header = ascii_safe(module_name).upper()
    lines.append(header[:width].center(width))
    lines.append("-" * width)
    lines.append("")
    for block in blocks:
        if block.title:
            lines.extend(wrap_text(block.title.upper(), width))
            lines.append("")
        body = list(block.lines)
        if not body:
            body = [""]
        for item in body:
            lines.extend(wrap_text(item, width))
        lines.append("")
    stamp = _utc(now).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(stamp.center(width))
    lines.append("-" * width)
    return _truncate(lines, max_chars)


def render_total(
    sections: Iterable[tuple[str, Sequence[str]]], width: int
) -> list[str]:
    width = max(1, int(width))
    parts: list[str] = []
    for index, (_name, lines) in enumerate(sections):
        if index:
            parts.extend(["", "=" * width, ""])
        parts.extend(str(line) for line in lines)
    if not parts:
        return ["No output from enabled modules."]
    return parts


def to_escpos(
    lines: Sequence[str],
    feed_lines: int = 3,
    cut: bool = True,
) -> bytes:
    safe = [ascii_safe(line) for line in lines]
    body = "\n".join(safe)
    # ascii_safe() already reduced the text to printable ASCII, which every
    # ESC/POS code page renders identically.
    data = b"\x1b@" + body.encode("ascii", errors="replace")
    data += b"\n" * max(0, int(feed_lines))
    if cut:
        data += b"\x1dV\x00"
    return data
