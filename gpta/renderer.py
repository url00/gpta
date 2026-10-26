"""Receipt layout shared by the simulator and the physical printer.

A run's blocks become a :class:`Receipt` (truncated to ``max_chars``), which
renders two ways: a 1-bit image the width of the paper, which is both the
console preview and what the printer prints, and plain text for copying and
debugging.
"""

from __future__ import annotations

import io
import math
import struct
import textwrap
import threading
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .blocks import (
    KINDS,
    BarChart,
    Block,
    Checkbox,
    Element,
    Heading,
    KeyValue,
    Rule,
    Text,
    from_dict,
    size_of,
    to_dict,
)

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT_FILES = {False: "DejaVuSansMono.ttf", True: "DejaVuSansMono-Bold.ttf"}
TRUNCATED = "[truncated]"
EMPTY_TOTAL = "No output from enabled modules."

# FreeType font objects are shared; rendering is quick, so serialise it.
_RENDER_LOCK = threading.Lock()


# -- text --------------------------------------------------------------
def _cmap(path: Path) -> frozenset[int]:
    """Code points a TrueType font has glyphs for (cmap format 4 or 12)."""
    data = path.read_bytes()
    (num_tables,) = struct.unpack_from(">H", data, 4)
    tables = {}
    for index in range(num_tables):
        tag, _sum, offset, _len = struct.unpack_from(">4sIII", data, 12 + 16 * index)
        tables[tag] = offset
    base = tables[b"cmap"]
    _version, count = struct.unpack_from(">HH", data, base)
    subtables = {}
    for index in range(count):
        platform, encoding, offset = struct.unpack_from(
            ">HHI", data, base + 4 + 8 * index
        )
        subtables[(platform, encoding)] = base + offset
    points: set[int] = set()
    if (3, 10) in subtables:
        start = subtables[(3, 10)]
        (groups,) = struct.unpack_from(">I", data, start + 12)
        for index in range(groups):
            first, last, _glyph = struct.unpack_from(
                ">III", data, start + 16 + 12 * index
            )
            points.update(range(first, last + 1))
        return frozenset(points)
    start = subtables.get((3, 1), subtables.get((0, 3)))
    if start is None:
        raise ValueError(f"{path.name} has no Unicode cmap")
    seg_count = struct.unpack_from(">H", data, start + 6)[0] // 2
    ends = struct.unpack_from(f">{seg_count}H", data, start + 14)
    starts_at = start + 16 + 2 * seg_count
    starts = struct.unpack_from(f">{seg_count}H", data, starts_at)
    deltas = struct.unpack_from(f">{seg_count}h", data, starts_at + 2 * seg_count)
    ranges_at = starts_at + 4 * seg_count
    ranges = struct.unpack_from(f">{seg_count}H", data, ranges_at)
    for seg in range(seg_count):
        for code in range(starts[seg], ends[seg] + 1):
            if code == 0xFFFF:
                continue
            if ranges[seg] == 0:
                glyph = (code + deltas[seg]) & 0xFFFF
            else:
                at = ranges_at + 2 * seg + ranges[seg] + 2 * (code - starts[seg])
                glyph = struct.unpack_from(">H", data, at)[0]
            if glyph:
                points.add(code)
    return frozenset(points)


@lru_cache(maxsize=1)
def _coverage() -> frozenset[int]:
    return _cmap(FONT_DIR / FONT_FILES[False]) & _cmap(FONT_DIR / FONT_FILES[True])


def clean_text(text: object) -> str:
    """Normalise text for printing.

    Keeps newlines; other control characters become spaces, and characters
    the receipt font has no glyph for become U+FFFD.
    """
    if text is None:
        return ""
    value = unicodedata.normalize("NFC", str(text))
    covered = _coverage()
    replacement = "�" if 0xFFFD in covered else "?"
    out = []
    for ch in value:
        if ch == "\n":
            out.append(ch)
        elif unicodedata.category(ch) in ("Cc", "Zl", "Zp"):
            out.append(" ")
        elif unicodedata.category(ch) in ("Cf", "Cs", "Co", "Cn"):
            continue
        elif ord(ch) in covered:
            out.append(ch)
        else:
            out.append(replacement)
    return "".join(out)


def wrap_text(text: object, width: int) -> list[str]:
    """Wrap to ``width`` characters, keeping blank lines between paragraphs."""
    width = max(1, int(width))
    lines: list[str] = []
    for paragraph in clean_text(text).split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        wrapped = textwrap.wrap(
            paragraph, width=width, break_long_words=True, break_on_hyphens=False
        )
        lines.extend(wrapped or [""])
    return lines


# -- receipts ----------------------------------------------------------
@dataclass(frozen=True)
class Ruler:
    """Tick marks across the full paper width (test page only; not saved)."""


@dataclass(frozen=True)
class Receipt:
    """A module's receipt: header title, content, and footer timestamp.

    An empty ``title`` or ``stamp`` omits the header or footer.
    """

    title: str
    elements: tuple[Any, ...]
    stamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "stamp": self.stamp,
            "blocks": [to_dict(element) for element in self.elements],
        }

    @classmethod
    def from_dict(cls, data: Any) -> Receipt:
        if not isinstance(data, dict):
            raise ValueError("receipt must be an object")
        title, stamp, blocks = data.get("title"), data.get("stamp"), data.get("blocks")
        if not isinstance(title, str) or not isinstance(stamp, str):
            raise ValueError("receipt title and stamp must be strings")
        if not isinstance(blocks, list):
            raise ValueError("receipt blocks must be a list")
        return cls(title, tuple(from_dict(block) for block in blocks), stamp)

    @classmethod
    def from_lines(cls, lines: Iterable[str]) -> Receipt:
        """A receipt from pre-rendered text lines (state files before images)."""
        return cls("", (Block("", tuple(lines)),))


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _cut(element: Element, budget: int) -> Element | None:
    """``element`` shortened to ``budget`` characters, or None to drop it."""
    if isinstance(element, Block):
        title = element.title[:budget]
        budget -= len(title)
        lines = []
        for line in element.lines:
            if budget <= 0:
                break
            lines.append(line[:budget])
            budget -= len(lines[-1])
        if not title and not lines:
            return None
        return Block(title, tuple(lines), element.meta)
    if isinstance(element, Heading):
        return Heading(element.text[:budget]) if budget else None
    if isinstance(element, Text):
        if not budget:
            return None
        return Text(element.text[:budget], element.size, element.bold, element.align)
    return None


def truncate(elements: Sequence[Element], max_chars: int | None) -> list[Element]:
    """Keep the first ``max_chars`` printed characters of ``elements``."""
    if max_chars is None:
        return list(elements)
    budget = max(0, int(max_chars))
    out: list[Element] = []
    for element in elements:
        size = size_of(element)
        if size <= budget:
            out.append(element)
            budget -= size
            continue
        part = _cut(element, budget)
        if part is not None:
            out.append(part)
        out.append(Text(TRUNCATED, size="small"))
        return out
    return out


def check_elements(elements: Any) -> list[Element]:
    """Validate what ``fetch()`` returned; raises TypeError on anything else."""
    if not isinstance(elements, (list, tuple)):
        raise TypeError(f"fetch() must return a list, not {type(elements).__name__}")
    known = tuple(KINDS.values())
    for element in elements:
        if not isinstance(element, known):
            raise TypeError(f"fetch() returned a {type(element).__name__}")
    return list(elements)


def build_receipt(
    module_name: str,
    elements: Sequence[Element],
    max_chars: int | None = None,
    now: datetime | None = None,
) -> Receipt:
    return Receipt(
        title=module_name,
        elements=tuple(truncate(check_elements(elements), max_chars)),
        stamp=_utc(now).strftime("%Y-%m-%d %H:%M UTC"),
    )


# -- plain text --------------------------------------------------------
def _number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _bar_text(bar) -> str:
    return bar.display or _number(bar.value)


def _bar_fraction(value: float, top: float) -> float:
    return 0.0 if top <= 0 else min(1.0, max(0.0, value / top))


def _chart_top(chart: BarChart) -> float:
    if chart.max_value is not None:
        return float(chart.max_value)
    return max((float(bar.value) for bar in chart.bars), default=0.0)


def _align(line: str, width: int, align: str) -> str:
    if align == "center":
        return line.center(width).rstrip()
    if align == "right":
        return line.rjust(width)
    return line


def _element_text(element: Any, width: int) -> list[str]:
    if isinstance(element, Block):
        lines = []
        if element.title:
            lines.extend(wrap_text(element.title.upper(), width) + [""])
        for item in element.lines or ("",):
            lines.extend(wrap_text(item, width))
        return lines + [""]
    if isinstance(element, Heading):
        return wrap_text(element.text.upper(), width)
    if isinstance(element, Text):
        wrapped = wrap_text(element.text, width)
        return [_align(line, width, element.align) for line in wrapped] + [""]
    if isinstance(element, Rule):
        return [("=" if element.heavy else "-") * width]
    if isinstance(element, KeyValue):
        label, value = clean_text(element.label), clean_text(element.value)
        if len(label) + len(value) + 1 <= width:
            return [label + value.rjust(width - len(label))]
        return wrap_text(label, width) + [
            line.rjust(width) for line in wrap_text(value, width)
        ]
    if isinstance(element, Checkbox):
        box = "[x] " if element.checked else "[ ] "
        value = clean_text(element.value)
        room = width - len(box) - (len(value) + 1 if value else 0)
        wrapped = wrap_text(element.text, max(1, room)) or [""]
        lines = [box + line for line in wrapped]
        if value:
            lines[0] = lines[0].ljust(width - len(value)) + value
        lines += ["    " + line for line in wrap_text(element.note, width - 4)]
        return [line.rstrip() for line in lines if line.strip()]
    if isinstance(element, BarChart):
        lines = wrap_text(element.title, width) if element.title else []
        top = _chart_top(element)
        values = [clean_text(_bar_text(bar)) for bar in element.bars]
        value_w = max((len(v) for v in values), default=0)
        label_w = min(
            max((len(clean_text(b.label)) for b in element.bars), default=0), width // 3
        )
        bar_w = max(1, width - label_w - value_w - 2)
        for bar, value in zip(element.bars, values, strict=True):
            label = clean_text(bar.label)[:label_w].ljust(label_w)
            fill = "#" * round(bar_w * _bar_fraction(bar.value, top))
            lines.append(f"{label} {fill.ljust(bar_w)} {value.rjust(value_w)}".rstrip())
        return lines + [""]
    if isinstance(element, Ruler):
        return ["".join("|" if i % 10 == 0 else "." for i in range(width))]
    raise TypeError(f"cannot render {type(element).__name__}")


def render_text(receipt: Receipt, width: int) -> list[str]:
    """``receipt`` as plain text lines, ``width`` characters wide."""
    width = max(1, int(width))
    lines: list[str] = []
    if receipt.title:
        lines.extend(
            line.center(width).rstrip()
            for line in wrap_text(receipt.title.upper(), width)
        )
        lines.extend(["-" * width, ""])
    for element in receipt.elements:
        lines.extend(_element_text(element, width))
    if receipt.stamp:
        lines.extend([receipt.stamp.center(width).rstrip(), "-" * width])
    return lines


def render_total(sections: Iterable[Sequence[str]], width: int) -> list[str]:
    """Join plain-text receipts with a separator line."""
    width = max(1, int(width))
    parts: list[str] = []
    for index, lines in enumerate(sections):
        if index:
            parts.extend(["", "=" * width, ""])
        parts.extend(str(line) for line in lines)
    return parts or [EMPTY_TOTAL]


# -- images ------------------------------------------------------------
@dataclass(frozen=True)
class Theme:
    """Font sizes (px) and spacing for one paper width."""

    small: int
    body: int
    large: int
    title: int
    margin: int


# 203 dpi: 24px body text is about 3 mm tall, 38 columns on 80 mm paper.
THEMES = {
    576: Theme(small=18, body=24, large=30, title=36, margin=8),
    384: Theme(small=16, body=20, large=24, title=28, margin=6),
}


#: Text sizes offered for one-off prints (Instant, web pages): the body size
#: in px on 80 mm paper. Everything else on the receipt scales with it.
PRINT_SIZES = {
    "tiny": 12,
    "small": 16,
    "compact": 20,
    "body": 24,
    "large": 30,
    "huge": 36,
}
# Smallest font drawn after scaling; below this, glyphs stop being legible.
MIN_FONT_PX = 11


def print_scale(size: str) -> float:
    """The scale factor for a :data:`PRINT_SIZES` name."""
    return PRINT_SIZES[size] / THEMES[576].body


def theme_for(width_px: int, scale: float = 1.0) -> Theme:
    theme = _base_theme(width_px)
    if scale == 1.0:
        return theme

    def px(value: int) -> int:
        return max(MIN_FONT_PX, round(value * scale))

    return Theme(
        small=px(theme.small),
        body=px(theme.body),
        large=px(theme.large),
        title=px(theme.title),
        margin=theme.margin,
    )


def _base_theme(width_px: int) -> Theme:
    fitting = [width for width in THEMES if width <= width_px]
    return THEMES[max(fitting) if fitting else min(THEMES)]


@lru_cache(maxsize=32)
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(
        str(FONT_DIR / FONT_FILES[bold]), size, layout_engine=ImageFont.Layout.BASIC
    )


def _line_height(face: ImageFont.FreeTypeFont) -> int:
    ascent, descent = face.getmetrics()
    return ascent + descent + max(2, face.size // 8)


def _wrap_px(text: str, face: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Wrap ``text`` (already cleaned) so each line fits ``width`` pixels."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        line = ""
        for word in words:
            candidate = f"{line} {word}" if line else word
            if face.getlength(candidate) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            line = ""
            while face.getlength(word) > width:
                cut = len(word)
                while cut > 1 and face.getlength(word[:cut]) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines


def _fit(text: str, face: ImageFont.FreeTypeFont, width: int) -> str:
    """``text`` cut with an ellipsis to fit ``width`` pixels."""
    if face.getlength(text) <= width:
        return text
    while text and face.getlength(text + "…") > width:
        text = text[:-1]
    return text + "…"


Paint = Callable[[ImageDraw.ImageDraw, int], None]


class _Layout:
    """Stacks rows top to bottom, then paints them onto one image."""

    def __init__(
        self,
        width: int,
        scale: float = 1.0,
        *,
        theme: Theme | None = None,
        pad: bool = True,
    ):
        """``theme`` overrides the paper's sizes (columns use the full-width
        ones); ``pad`` adds the top and bottom margin."""
        self.width = width
        self.theme = theme or theme_for(width, scale)
        self.left = self.theme.margin
        self.right = width - self.theme.margin
        self.inner = self.right - self.left
        self.gap = self.theme.body // 2
        self.pad = self.theme.margin if pad else 0
        self.y = self.pad
        self.rows: list[tuple[int, Paint]] = []

    def add(self, height: int, paint: Paint | None = None) -> None:
        if paint is not None:
            self.rows.append((self.y, paint))
        self.y += height

    def text(
        self, text: str, size: int, *, bold: bool = False, align: str = "left"
    ) -> None:
        face = font(size, bold)
        height = _line_height(face)
        for line in _wrap_px(clean_text(text), face, self.inner):
            if not line:
                self.add(height // 2)
                continue
            self.add(height, self._painter(line, face, align))

    def _painter(self, line: str, face, align: str) -> Paint:
        length = face.getlength(line)
        if align == "center":
            x = self.left + (self.inner - length) / 2
        elif align == "right":
            x = self.right - length
        else:
            x = self.left
        return lambda draw, y: draw.text((x, y), line, font=face, fill=0)

    def rule(self, thickness: int, *, full: bool = False) -> None:
        x0, x1 = (0, self.width - 1) if full else (self.left, self.right - 1)
        self.add(
            thickness,
            lambda draw, y: draw.rectangle([x0, y, x1, y + thickness - 1], fill=0),
        )

    def canvas(self) -> Image.Image:
        """The rows painted in grayscale ("L"), before thresholding."""
        canvas = Image.new("L", (self.width, max(1, self.y + self.pad)), 255)
        draw = ImageDraw.Draw(canvas)
        for y, paint in self.rows:
            paint(draw, y)
        return canvas

    def items(self) -> list[tuple[int, Paint]]:
        """Rows as ``(height, paint)``; a row's height runs to the next row,
        so blank space travels with the row above it."""
        out = []
        for index, (y, paint) in enumerate(self.rows):
            following = (
                self.rows[index + 1][0] if index + 1 < len(self.rows) else self.y
            )
            out.append((following - y, paint))
        return out

    def image(self) -> Image.Image:
        # A hard threshold keeps glyph edges crisp; dithering would speckle them.
        return self.canvas().convert("1", dither=Image.Dither.NONE)


def _layout_element(out: _Layout, element: Any) -> None:
    theme = out.theme
    if isinstance(element, Block):
        if element.title:
            out.text(element.title, theme.large, bold=True)
            out.add(out.gap // 2)
        for line in element.lines or ("",):
            out.text(line, theme.body)
    elif isinstance(element, Heading):
        out.text(element.text, theme.large, bold=True)
    elif isinstance(element, Text):
        size = {
            "small": theme.small,
            "body": theme.body,
            "large": theme.large,
            "huge": theme.title,
        }
        out.text(
            element.text, size[element.size], bold=element.bold, align=element.align
        )
    elif isinstance(element, Rule):
        out.rule(4 if element.heavy else 2)
    elif isinstance(element, KeyValue):
        _layout_key_value(out, element)
    elif isinstance(element, Checkbox):
        _layout_checkbox(out, element)
    elif isinstance(element, BarChart):
        _layout_bar_chart(out, element)
    elif isinstance(element, Ruler):
        _layout_ruler(out)
    else:
        raise TypeError(f"cannot render {type(element).__name__}")
    out.add(out.gap)


def _layout_key_value(out: _Layout, element: KeyValue) -> None:
    regular, bold = font(out.theme.body), font(out.theme.body, True)
    label, value = clean_text(element.label), clean_text(element.value)
    space = regular.getlength(" ")
    if regular.getlength(label) + space + bold.getlength(value) <= out.inner:
        height = _line_height(regular)
        x_value = out.right - bold.getlength(value)

        def paint(draw, y):
            draw.text((out.left, y), label, font=regular, fill=0)
            draw.text((x_value, y), value, font=bold, fill=0)

        out.add(height, paint)
        return
    out.text(label, out.theme.body)
    out.text(value, out.theme.body, bold=True, align="right")


def _layout_checkbox(out: _Layout, element: Checkbox) -> None:
    regular, bold = font(out.theme.body), font(out.theme.body, True)
    ascent, _descent = regular.getmetrics()
    box = ascent * 4 // 5
    indent = box + round(regular.getlength(" ")) * 3 // 2
    value = clean_text(element.value)
    value_w = bold.getlength(value) + regular.getlength(" ") if value else 0
    width = max(1, int(out.inner - indent - value_w))
    lines = _wrap_px(clean_text(element.text), regular, width) or [""]
    height = _line_height(regular)
    for index, line in enumerate(lines):
        first = index == 0

        def paint(draw, y, line=line, first=first):
            x = out.left + indent
            draw.text((x, y), line, font=regular, fill=0)
            if not first:
                return
            top = y + ascent - box
            draw.rectangle(
                [out.left, top, out.left + box - 1, top + box - 1], outline=0, width=2
            )
            if element.checked:
                pad = max(3, box // 4)
                draw.rectangle(
                    [
                        out.left + pad,
                        top + pad,
                        out.left + box - 1 - pad,
                        top + box - 1 - pad,
                    ],
                    fill=0,
                )
            if value:
                draw.text(
                    (out.right - bold.getlength(value), y), value, font=bold, fill=0
                )

        out.add(height, paint)
    if element.note:
        small = font(out.theme.small)
        for line in _wrap_px(clean_text(element.note), small, out.inner - indent):
            out.add(
                _line_height(small),
                lambda draw, y, line=line: draw.text(
                    (out.left + indent, y), line, font=small, fill=0
                ),
            )


def _layout_bar_chart(out: _Layout, chart: BarChart) -> None:
    face = font(out.theme.body)
    if chart.title:
        out.text(chart.title, out.theme.body, bold=True)
    if not chart.bars:
        return
    top = _chart_top(chart)
    labels = [clean_text(bar.label) for bar in chart.bars]
    values = [clean_text(_bar_text(bar)) for bar in chart.bars]
    space = face.getlength(" ")
    value_w = max(face.getlength(v) for v in values)
    label_w = min(max(face.getlength(label) for label in labels), out.inner * 0.4)
    bar_x0 = out.left + label_w + space if label_w else out.left
    bar_x1 = out.right - value_w - space
    height = _line_height(face)
    ascent, descent = face.getmetrics()
    thickness = max(4, (ascent + descent) * 3 // 5)
    for label, value, bar in zip(labels, values, chart.bars, strict=True):
        length = (bar_x1 - bar_x0) * _bar_fraction(bar.value, top)
        shown = _fit(label, face, label_w)

        def paint(draw, y, shown=shown, value=value, length=length):
            if shown:
                draw.text((out.left, y), shown, font=face, fill=0)
            top_y = y + (ascent + descent - thickness) // 2
            end = bar_x0 + max(1, round(length)) - 1
            draw.rectangle([bar_x0, top_y, end, top_y + thickness - 1], fill=0)
            draw.text((out.right - face.getlength(value), y), value, font=face, fill=0)

        out.add(height, paint)


def _layout_ruler(out: _Layout) -> None:
    # Ticks every 8 dots (1 mm), long ones every 80 dots (10 mm), from the
    # very first to the very last dot, so the printable edges show.
    def paint(draw, y):
        draw.rectangle([0, y, out.width - 1, y + 1], fill=0)
        for x in range(0, out.width, 8):
            length = 30 if x % 80 == 0 else 14 if x % 40 == 0 else 8
            draw.rectangle([x, y, x + 1, y + length], fill=0)
        draw.rectangle([out.width - 2, y, out.width - 1, y + 30], fill=0)

    out.add(32, paint)


LAYOUTS = ("single", "double", "triple")
#: Columns per layout; only two columns get a dividing rule.
LAYOUT_COLUMNS = {"single": 1, "double": 2, "triple": 3}
# Tallest a column may grow before the text continues in a new band below,
# so a reader never has to travel far back up the paper (about 15 cm).
COLUMN_BAND_MAX_PX = 1200
COLUMN_RULE_PX = 2


def render_image(
    receipt: Receipt,
    width_px: int,
    scale: float = 1.0,
    *,
    layout: str = "single",
    lead: int = 0,
) -> Image.Image:
    """``receipt`` as a 1-bit image ``width_px`` dots wide (0 = black).

    ``scale`` multiplies every font size (see :func:`print_scale`).
    ``layout="double"`` or ``"triple"`` sets body text in two or three
    columns; the first ``lead`` elements (a header), headings, rules and
    centred text stay full width.
    """
    columns = LAYOUT_COLUMNS[layout]
    if columns > 1:
        with _RENDER_LOCK:
            return _render_columns(receipt, int(width_px), scale, lead, columns)
    with _RENDER_LOCK:
        out = _Layout(int(width_px), scale)
        theme = out.theme
        if receipt.title:
            out.text(receipt.title.upper(), theme.title, bold=True, align="center")
            out.add(out.gap // 2)
            out.rule(4)
            out.add(out.gap)
        for element in receipt.elements:
            _layout_element(out, element)
        if receipt.stamp:
            out.rule(2)
            out.add(out.gap // 2)
            out.text(receipt.stamp, theme.small, align="center")
        return out.image()


def _full_width(element: Any) -> bool:
    if isinstance(element, (Heading, Rule, Ruler)):
        return True
    return isinstance(element, Text) and element.align != "left"


def _render_columns(
    receipt: Receipt, width: int, scale: float, lead: int, columns: int
) -> Image.Image:
    """Column layout: full-width pieces stacked with column bands."""
    theme = theme_for(width, scale)
    pieces: list[Image.Image] = []
    full = _Layout(width, theme=theme, pad=False)
    run: list[Element] = []

    def flush_full() -> None:
        nonlocal full
        if full.y:
            pieces.append(full.canvas())
        full = _Layout(width, theme=theme, pad=False)

    def flush_run() -> None:
        if run:
            flush_full()
            pieces.extend(_column_bands(run, width, theme, columns))
            run.clear()

    if receipt.title:
        full.text(receipt.title.upper(), theme.title, bold=True, align="center")
        full.add(full.gap // 2)
        full.rule(4)
        full.add(full.gap)
    for index, element in enumerate(receipt.elements):
        if index < lead or _full_width(element):
            flush_run()
            _layout_element(full, element)
        else:
            run.append(element)
    flush_run()
    if receipt.stamp:
        full.rule(2)
        full.add(full.gap // 2)
        full.text(receipt.stamp, theme.small, align="center")
    flush_full()

    margin = theme.margin
    height = margin * 2 + sum(piece.height for piece in pieces)
    canvas = Image.new("L", (width, max(1, height)), 255)
    y = margin
    for piece in pieces:
        canvas.paste(piece, (0, y))
        y += piece.height
    return canvas.convert("1", dither=Image.Dither.NONE)


def _column_bands(
    elements: list[Element], width: int, theme: Theme, columns: int
) -> list[Image.Image]:
    """Lay ``elements`` out in ``columns`` side-by-side columns, balanced.

    Text flows down each column and continues at the top of the next;
    splits fall between lines. Longer runs become several bands, each at
    most :data:`COLUMN_BAND_MAX_PX` tall, read one after the other.
    """
    gutter = theme.margin
    column_w = (width - gutter * (columns - 1)) // columns
    column = _Layout(column_w, theme=theme, pad=False)
    for element in elements:
        _layout_element(column, element)
    items = column.items()
    if not items:
        return []
    total = sum(height for height, _paint in items)
    bands = max(1, math.ceil(total / (columns * COLUMN_BAND_MAX_PX)))
    groups: list[list[tuple[int, Paint]]] = [[] for _ in range(bands)]
    used = 0
    for item in items:  # fill bands in order, each to an equal share
        index = min(bands - 1, int(used * bands / total))
        groups[index].append(item)
        used += item[0]
    out = []
    for number, group in enumerate(group for group in groups if group):
        if number:
            out.append(_band_break(width, theme))
        out.append(_band(group, width, column_w, columns))
    return out


def _split_columns(heights: list[int], columns: int) -> list[int]:
    """Cut points that split ``heights`` into ``columns`` runs in order.

    Each column, left to right, ends where the taller of it and the best
    split of what remains is shortest; ties go to the longer column, so
    earlier columns keep the extra lines when the runs cannot be equal.
    """
    cuts, start = [], 0
    for column in range(columns - 1):
        rest = columns - 1 - column
        best, best_cost, height = start, None, 0
        for end in range(start, len(heights) + 1):
            if end > start:
                height += heights[end - 1]
            cost = max(height, _tallest_column(heights[end:], rest))
            if best_cost is None or cost <= best_cost:
                best, best_cost = end, cost
            if height >= best_cost:
                break  # longer only gets taller
        cuts.append(best)
        start = best
    return cuts


def _tallest_column(heights: list[int], columns: int) -> int:
    """The shortest possible tallest column when ``heights`` is split in
    order into ``columns`` runs."""

    def fits(cap: int) -> bool:
        used, height = 1, 0
        for value in heights:
            if height + value > cap:
                used, height = used + 1, 0
            height += value
        return used <= columns

    low, high = max(heights, default=0), sum(heights)
    while low < high:
        mid = (low + high) // 2
        if fits(mid):
            high = mid
        else:
            low = mid + 1
    return low


def _band(
    items: list[tuple[int, Paint]], width: int, column_w: int, columns: int
) -> Image.Image:
    heights = [height for height, _paint in items]
    bounds = [0, *_split_columns(heights, columns), len(items)]
    groups = [items[a:b] for a, b in zip(bounds, bounds[1:], strict=False)]
    tallest = max(sum(height for height, _paint in group) for group in groups)
    band = Image.new("L", (width, max(1, tallest)), 255)
    step = (width - column_w) / (columns - 1)
    for number, group in enumerate(groups):
        column = Image.new("L", (column_w, band.height), 255)
        draw = ImageDraw.Draw(column)
        y = 0
        for height, paint in group:
            paint(draw, y)
            y += height
        band.paste(column, (round(number * step), 0))
    if columns == 2:
        mid = width // 2 - COLUMN_RULE_PX // 2
        ImageDraw.Draw(band).rectangle(
            [mid, 0, mid + COLUMN_RULE_PX - 1, band.height - 1], fill=0
        )
    return band


def _band_break(width: int, theme: Theme) -> Image.Image:
    """A dotted line between bands: read on below."""
    height = theme.body
    piece = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(piece)
    mid = height // 2
    for x in range(theme.margin, width - theme.margin, 8):
        draw.rectangle([x, mid - 1, x + 2, mid + 1], fill=0)
    return piece


SEPARATOR_HEIGHT = 48


def render_total_image(images: Sequence[Image.Image]) -> Image.Image | None:
    """Stack receipt images with a dashed cut line between them."""
    if not images:
        return None
    width = max(image.width for image in images)
    height = sum(image.height for image in images)
    height += SEPARATOR_HEIGHT * (len(images) - 1)
    total = Image.new("1", (width, height), 1)
    draw = ImageDraw.Draw(total)
    y = 0
    for index, image in enumerate(images):
        if index:
            mid = y + SEPARATOR_HEIGHT // 2
            for x in range(0, width, 24):
                draw.rectangle([x, mid - 1, x + 13, mid + 1], fill=0)
            y += SEPARATOR_HEIGHT
        total.paste(image, (0, y))
        y += image.height
    return total


def to_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", dpi=(203, 203))
    return buffer.getvalue()


def test_receipt(now: datetime | None = None) -> Receipt:
    """The printer test page: fonts, a rule, a full-width ruler, and bars."""
    return Receipt(
        "GPTA printer test",
        (
            Heading("Fonts"),
            Text("Small: the quick brown fox jumps over the lazy dog.", size="small"),
            Text("Body: the quick brown fox jumps over the lazy dog."),
            Text("Bold body text", bold=True),
            Text("Large text", size="large"),
            Text("Unicode: café, naïve, 25°C, €5, — “quotes”"),
            Rule(),
            Text("Ruler: a tick every 1 mm, long every 10 mm.", size="small"),
            Ruler(),
            BarChart(
                (("one", 1), ("two", 2), ("three", 3), ("full", 4)),
                title="Bars",
            ),
            Text("If you can read this, printing works.", align="center"),
        ),
        _utc(now).strftime("%Y-%m-%d %H:%M UTC"),
    )
