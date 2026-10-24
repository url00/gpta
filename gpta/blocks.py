"""Receipt block kinds: the data a module returns from ``fetch()``.

Every kind is a small frozen dataclass that converts to and from plain JSON
(:func:`to_dict` / :func:`from_dict`), so receipts can be saved in
``state.json`` and, later, sent over IPC. New kinds (QR codes, images) are
added by defining a dataclass with a ``kind`` and registering it in
:data:`KINDS`; the renderer then needs a layout function for it.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar

ALIGNS = ("left", "center", "right")
SIZES = ("small", "body", "large")


def _str(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string, not {type(value).__name__}")
    return value


def _choice(value: Any, name: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(choices)}")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


@dataclass(frozen=True)
class Block:
    """A titled section: an optional heading over lines of body text."""

    kind: ClassVar[str] = "block"

    title: str
    lines: tuple[str, ...]
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _str(self.title, "Block.title")
        if isinstance(self.lines, str):
            raise TypeError("Block.lines must be a sequence of strings, not a string")
        object.__setattr__(self, "lines", tuple(self.lines))
        for line in self.lines:
            _str(line, "Block.lines[]")


@dataclass(frozen=True)
class Heading:
    """A large bold heading."""

    kind: ClassVar[str] = "heading"

    text: str

    def __post_init__(self) -> None:
        _str(self.text, "Heading.text")


@dataclass(frozen=True)
class Text:
    """A wrapped paragraph. ``size`` is small, body, or large."""

    kind: ClassVar[str] = "text"

    text: str
    size: str = "body"
    bold: bool = False
    align: str = "left"

    def __post_init__(self) -> None:
        _str(self.text, "Text.text")
        _choice(self.size, "Text.size", SIZES)
        _choice(self.align, "Text.align", ALIGNS)
        if not isinstance(self.bold, bool):
            raise TypeError("Text.bold must be true or false")


@dataclass(frozen=True)
class Rule:
    """A horizontal line across the paper; ``heavy`` makes it thicker."""

    kind: ClassVar[str] = "rule"

    heavy: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.heavy, bool):
            raise TypeError("Rule.heavy must be true or false")


@dataclass(frozen=True)
class KeyValue:
    """One row: ``label`` on the left, ``value`` right-aligned."""

    kind: ClassVar[str] = "key_value"

    label: str
    value: str

    def __post_init__(self) -> None:
        _str(self.label, "KeyValue.label")
        _str(self.value, "KeyValue.value")


@dataclass(frozen=True)
class Checkbox:
    """A to-do row: a drawn box, ``text`` beside it, an optional right-aligned
    ``value`` (e.g. a due date), and an optional small ``note`` underneath."""

    kind: ClassVar[str] = "checkbox"

    text: str
    checked: bool = False
    value: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        _str(self.text, "Checkbox.text")
        _str(self.value, "Checkbox.value")
        _str(self.note, "Checkbox.note")
        if not isinstance(self.checked, bool):
            raise TypeError("Checkbox.checked must be true or false")


@dataclass(frozen=True)
class Bar:
    """One bar of a :class:`BarChart`. ``display`` overrides the value text."""

    label: str
    value: float
    display: str = ""

    def __post_init__(self) -> None:
        _str(self.label, "Bar.label")
        _number(self.value, "Bar.value")
        _str(self.display, "Bar.display")


@dataclass(frozen=True)
class BarChart:
    """Horizontal bars, longest = ``max_value`` (default: the largest value)."""

    kind: ClassVar[str] = "bar_chart"

    bars: tuple[Bar, ...]
    title: str = ""
    max_value: float | None = None

    def __post_init__(self) -> None:
        bars = tuple(
            bar if isinstance(bar, Bar) else Bar(*bar) for bar in self.bars or ()
        )
        object.__setattr__(self, "bars", bars)
        _str(self.title, "BarChart.title")
        if self.max_value is not None:
            _number(self.max_value, "BarChart.max_value")


#: Everything a module may return from ``fetch()``.
Element = Block | Heading | Text | Rule | KeyValue | Checkbox | BarChart

KINDS: dict[str, type] = {
    cls.kind: cls for cls in (Block, Heading, Text, Rule, KeyValue, Checkbox, BarChart)
}


def text_of(element: Element) -> str:
    """The text an element prints."""
    if isinstance(element, Block):
        return element.title + "".join(element.lines)
    if isinstance(element, (Heading, Text)):
        return element.text
    if isinstance(element, KeyValue):
        return element.label + element.value
    if isinstance(element, Checkbox):
        return element.text + element.value + element.note
    if isinstance(element, BarChart):
        return element.title + "".join(bar.label for bar in element.bars)
    return ""


def size_of(element: Element) -> int:
    """What ``max_chars`` counts: the text, plus one per rule and per bar."""
    extra = 1 if isinstance(element, Rule) else 0
    if isinstance(element, BarChart):
        extra = len(element.bars)
    return len(text_of(element)) + extra


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return {}


def to_dict(element: Element) -> dict[str, Any]:
    """Plain JSON for ``element``, tagged with its ``kind``."""
    if type(element) not in KINDS.values():
        raise TypeError(f"not a receipt block: {type(element).__name__}")
    data: dict[str, Any] = {"kind": element.kind}
    for item in fields(element):
        value = getattr(element, item.name)
        if item.name == "lines":
            value = list(value)
        elif item.name == "bars":
            value = [
                {"label": bar.label, "value": bar.value, "display": bar.display}
                for bar in value
            ]
        elif item.name == "meta":
            value = _jsonable(value)
        data[item.name] = value
    return data


def from_dict(data: Any) -> Element:
    """Rebuild an element from :func:`to_dict` output; raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError("block must be an object")
    cls = KINDS.get(data.get("kind"))
    if cls is None:
        raise ValueError(f"unknown block kind: {data.get('kind')!r}")
    names = {item.name for item in fields(cls)}
    kwargs = {key: value for key, value in data.items() if key in names}
    try:
        if cls is Block:
            kwargs["lines"] = tuple(kwargs.get("lines") or ())
            if not isinstance(kwargs.get("meta", {}), dict):
                kwargs["meta"] = {}
        elif cls is BarChart:
            raw = kwargs.get("bars")
            if not isinstance(raw, list) or not all(isinstance(b, dict) for b in raw):
                raise ValueError("bar_chart.bars must be a list of objects")
            kwargs["bars"] = tuple(
                Bar(b.get("label"), b.get("value"), b.get("display", "")) for b in raw
            )
        return cls(**kwargs)
    except TypeError as exc:
        raise ValueError(str(exc)) from exc
