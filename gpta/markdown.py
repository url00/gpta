"""Markdown to receipt blocks, for Instant prints. Standard library only.

Covers what people paste from notes and chat: ``#`` and underlined
headings, paragraphs (a trailing double space or backslash breaks the line),
bullet, numbered and task lists (nested by indentation), quotes, fenced and
indented code, tables and horizontal rules. Inline, ``**bold**`` prints
bold; italics, strikethrough and code marks are dropped (the receipt font
has no italic); links print as their text with the address in brackets.
"""

from __future__ import annotations

import re

from .blocks import Checkbox, Element, Heading, Rule, Text

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_HR = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_QUOTE = re.compile(r"^ {0,3}> ?(.*)$")
_ITEM = re.compile(r"^( *)([-*+]|\d{1,9}[.)])(?:[ \t]+(.*)|$)")
_TASK = re.compile(r"^\[([ xX])\][ \t]+(.*)$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_INDENT_CODE = "    "

#: Deepest list nesting shown; deeper items stay at this indent.
MAX_LIST_DEPTH = 4


def to_blocks(text: str) -> list[Element]:
    """The receipt blocks for Markdown ``text``."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).split("\n")
    out: list[Element] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append(_text(_join_lines(para)))
            para.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            flush()
            i += 1
            continue
        if para and _SETEXT.match(line):
            level = 1 if line.strip()[0] == "=" else 2
            content = _join_lines(para)
            para.clear()
            out.append(_heading(content, level))
            i += 1
            continue
        fence = _FENCE.match(line)
        if fence:
            flush()
            mark = fence.group(1)
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith(mark):
                code.append(lines[i])
                i += 1
            i += 1  # the closing fence
            _code(out, code)
            continue
        if not para and line.startswith(_INDENT_CODE) and not _ITEM.match(line):
            code = []
            while i < len(lines) and (
                lines[i].startswith(_INDENT_CODE) or not lines[i].strip()
            ):
                code.append(lines[i][len(_INDENT_CODE) :])
                i += 1
            _code(out, code)
            continue
        atx = _ATX.match(line)
        if atx:
            flush()
            out.append(_heading(atx.group(2) or "", len(atx.group(1))))
            i += 1
            continue
        if _HR.match(line):
            flush()
            out.append(Rule())
            i += 1
            continue
        if _QUOTE.match(line):
            flush()
            quoted: list[str] = []
            while i < len(lines) and (match := _QUOTE.match(lines[i])):
                quoted.append(match.group(1))
                i += 1
            for paragraph in "\n".join(quoted).split("\n\n"):
                if paragraph.strip():
                    plain, spans = inline(_join_lines(paragraph.split("\n")))
                    shifted = tuple((a + 1, b + 1) for a, b in spans)
                    out.append(Text(f"“{plain}”", bold_spans=shifted))
            continue
        if "|" in line and i + 1 < len(lines) and _TABLE_RULE.match(lines[i + 1]):
            flush()
            out.append(_row(line, header=True))
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                out.append(_row(lines[i]))
                i += 1
            continue
        item = _ITEM.match(line)
        if item and (not para or item.group(3)):
            flush()
            i = _list_item(lines, i, item, out)
            continue
        para.append(line)
        i += 1
    flush()
    return out


def _list_item(lines: list[str], i: int, item: re.Match, out: list[Element]) -> int:
    """Emit the item at ``lines[i]`` with its continuation lines; returns
    the index after it."""
    depth = min(len(item.group(1)) // 2, MAX_LIST_DEPTH)
    body = [item.group(3) or ""]
    i += 1
    while i < len(lines):
        line = lines[i]
        if not line.strip() or _ITEM.match(line) or _starts_block(line):
            break
        body.append(line.strip())
        i += 1
    content = _join_lines(body)
    pad = "  " * depth  # leading spaces print, so nesting shows
    task = _TASK.match(content)
    if task:
        plain, _spans = inline(task.group(2))
        out.append(Checkbox(plain, checked=task.group(1) != " "))
        return i
    marker = item.group(2)
    mark = "•" if marker in "-*+" else marker
    prefix = f"{pad}{mark} "
    plain, spans = inline(content)
    shifted = tuple((a + len(prefix), b + len(prefix)) for a, b in spans)
    out.append(Text(prefix + plain, bold_spans=shifted))
    return i


def _starts_block(line: str) -> bool:
    return bool(
        _FENCE.match(line) or _ATX.match(line) or _HR.match(line) or _QUOTE.match(line)
    )


def _join_lines(lines: list[str]) -> str:
    """Paragraph lines joined by spaces, or by a newline after a hard break
    (two trailing spaces or a backslash)."""
    out = ""
    for index, line in enumerate(lines):
        hard = line.endswith("  ") or line.endswith("\\")
        text = line.strip()
        if text.endswith("\\") and not text.endswith("\\\\"):
            text = text[:-1].rstrip()
        out += text
        if index < len(lines) - 1:
            out += "\n" if hard else " "
    return out


def _text(content: str, **kwargs) -> Text:
    plain, spans = inline(content)
    return Text(plain, bold_spans=spans, **kwargs)


def _heading(content: str, level: int) -> Element:
    plain, _spans = inline(content)
    if level <= 2:
        return Heading(plain)
    return Text(plain, bold=True)


def _code(out: list[Element], lines: list[str]) -> None:
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines:
        out.append(Text("\n".join(line.rstrip() for line in lines), size="small"))


def _row(line: str, *, header: bool = False) -> Text:
    cells = line.strip().strip("|").split("|")
    plain, spans = inline(" | ".join(cell.strip() for cell in cells))
    return Text(plain, size="small", bold=header, bold_spans=() if header else spans)


_INLINE = re.compile(
    r"""
    (?P<esc>\\[!-/:-@\[-`{-~])
    | (?P<tick>`+)(?P<code>.+?)(?P=tick)
    | !\[(?P<alt>[^\]]*)\]\([^)]*\)
    | \[(?P<label>[^\]]+)\]\((?P<url>[^)\s]*)(?:\s+"[^"]*")?\)
    | <(?P<auto>(?:https?|mailto):[^>\s]+)>
    | \*\*(?P<strong>.+?)\*\* | (?<!\w)__(?P<strong2>.+?)__(?!\w)
    | ~~(?P<strike>.+?)~~
    | \*(?P<em>[^*\s](?:.*?(?:[^*\s]|\*\*))?)\*(?!\*)
    | (?<!\w)_(?P<em2>[^_\s](?:[^_]*?[^_\s])?)_(?!\w)
    """,
    re.VERBOSE,
)


def inline(text: str) -> tuple[str, tuple[tuple[int, int], ...]]:
    """Markdown inline ``text`` as plain text and its bold ranges."""
    pieces: list[str] = []
    spans: list[tuple[int, int]] = []
    length = 0

    def add(piece: str, bold: bool) -> None:
        nonlocal length
        if not piece:
            return
        if bold:
            if spans and spans[-1][1] == length:
                spans[-1] = (spans[-1][0], length + len(piece))
            else:
                spans.append((length, length + len(piece)))
        pieces.append(piece)
        length += len(piece)

    def walk(source: str, bold: bool) -> None:
        pos = 0
        for match in _INLINE.finditer(source):
            add(source[pos : match.start()], bold)
            pos = match.end()
            group = match.lastgroup
            value = match.group(group)
            if group == "esc":
                add(value[1], bold)
            elif group == "code":
                add(value.strip() or value, bold)
            elif group == "alt":
                add(value, bold)
            elif group in ("label", "url"):
                label, url = match.group("label"), match.group("url")
                walk(label, bold)
                if url.startswith(("http://", "https://")) and url != label:
                    add(f" ({url})", bold)
            elif group == "auto":
                add(value.removeprefix("mailto:"), bold)
            elif group in ("strong", "strong2"):
                walk(value, True)
            else:  # italic or strikethrough: no such face, keep the words
                walk(value, bold)
        add(source[pos:], bold)

    walk(text, False)
    return "".join(pieces), tuple(spans)
