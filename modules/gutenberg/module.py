"""Project Gutenberg book excerpt printer."""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET

from gpta.module_api import Block, FetchError, Module, ModuleContext

BASE = "https://www.gutenberg.org"
RANDOM_OPDS = BASE + "/ebooks/search.opds/"
BOOK_OPDS = BASE + "/ebooks/{}.opds"
TEXT_URL = BASE + "/cache/epub/{0}/pg{0}.txt"
START_MARKER = re.compile(r"\*\*\* START OF [^\n]*\*\*\*", re.IGNORECASE)
END_MARKER = re.compile(r"\*\*\* END OF [^\n]*\*\*\*", re.IGNORECASE)
OPDS_NS = {"a": "http://www.w3.org/2005/Atom"}
METADATA_LINE = re.compile(
    r"^(Title|Author|Release Date|Language|Credits|Character set encoding):",
    re.IGNORECASE,
)


def _parse_opds(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FetchError(f"invalid Gutenberg OPDS response: {exc}") from exc
    books = []
    for entry in root.findall("a:entry", OPDS_NS):
        identifier = entry.findtext("a:id", default="", namespaces=OPDS_NS)
        match = re.search(r"/ebooks/(\d+)", identifier) or re.search(
            r"urn:gutenberg:(\d+):", identifier
        )
        title = entry.findtext("a:title", default="", namespaces=OPDS_NS)
        author = entry.findtext("a:content", default="", namespaces=OPDS_NS)
        if match and title:
            books.append(
                {
                    "id": int(match.group(1)),
                    "title": title,
                    "author": author.strip(),
                }
            )
    return books


def _book_candidates(ctx: ModuleContext) -> list[dict]:
    book_id = ctx.options.get("book_id")
    if book_id is not None:  # validated as an int by options_schema
        books = _parse_opds(
            ctx.http.get_text(BOOK_OPDS.format(book_id), max_bytes=2_000_000)
        )
        for book in books:
            if book["id"] == book_id:
                return [book]
        raise FetchError(f"unknown Gutenberg book {book_id}")

    query = urllib.parse.urlencode(
        {
            "sort_order": "random",
            "filetype": "txt",
            "lang": str(ctx.options.get("language") or "en"),
        }
    )
    books = _parse_opds(
        ctx.http.get_text(f"{RANDOM_OPDS}?{query}", max_bytes=2_000_000)
    )
    if not books:
        raise FetchError("Gutenberg random feed returned no books")
    return books


def _cached_text(ctx: ModuleContext, book: dict) -> str:
    book_id = int(book["id"])
    url = TEXT_URL.format(book_id)
    key = f"gutenberg/{book_id}.txt"
    use_cache = bool(ctx.options.get("cache", True))
    if use_cache:
        cached = ctx.cache.read_text(key)
        if cached is not None:
            ctx.emit(f"using cached text for book {book_id}")
            return cached
    text = ctx.http.get_text(url, max_bytes=1_000_000)
    if use_cache:
        try:
            ctx.cache.write_text(key, text)
        except OSError as exc:
            ctx.emit(f"cache write failed: {exc}", "warning")
    return text


def _strip_boilerplate(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    start = START_MARKER.search(text)
    end = END_MARKER.search(text)
    begin = start.end() if start else 0
    finish = end.start() if end and end.start() > begin else len(text)
    body = text[begin:finish]
    paragraphs = []
    for raw in re.split(r"\n\s*\n", body):
        lines = [
            line for line in raw.splitlines() if not METADATA_LINE.match(line.strip())
        ]
        paragraph = " ".join(" ".join(lines).split())
        if not paragraph:
            continue
        if set(paragraph) <= {"_", "-", " "}:
            continue
        lowered = paragraph.lower()
        if len(paragraph) < 100 and (
            paragraph.isupper()
            or lowered.startswith(
                (
                    "produced by",
                    "transcriber",
                    "updated editions",
                    "character set encoding",
                )
            )
        ):
            continue
        paragraphs.append(paragraph)
    return "\n\n".join(paragraphs)


class GutenbergModule(Module):
    id = "gutenberg"
    name = "Project Gutenberg"
    description = "Prints an excerpt from a random public-domain book."

    defaults = {
        "enabled": False,
        "interval_seconds": 86400,
        "limits": {"max_chars": 1800},
        "options": {"book_id": None, "language": "en", "cache": True},
    }
    options_schema = {
        "book_id": {
            "type": "int",
            "label": "Book ID (blank = random)",
            "min": 1,
            "max": 100000,
            "optional": True,
        },
        "language": {
            "type": "str",
            "label": "Language code",
            "max_length": 8,
        },
        "cache": {
            "type": "bool",
            "label": "Cache downloaded text",
        },
    }

    def fetch(self, ctx: ModuleContext) -> list[Block]:
        candidates = _book_candidates(ctx)
        max_chars = int(ctx.limits["max_chars"])
        selected = None
        paragraphs: list[str] = []
        for book in candidates[:8]:
            try:
                text = _cached_text(ctx, book)
            except FetchError as exc:
                ctx.emit(f"book {book['id']} unavailable: {exc}", "warning")
                continue
            if not book.get("author"):
                author_match = re.search(r"^Author:\s*(.+)$", text, re.MULTILINE)
                if author_match:
                    book = dict(book, author=author_match.group(1).strip())
            body = _strip_boilerplate(text)
            candidate_paragraphs: list[str] = []
            used = 0
            for paragraph in body.split("\n\n"):
                if not paragraph:
                    continue
                if candidate_paragraphs and used + len(paragraph) > max_chars:
                    break
                candidate_paragraphs.append(paragraph)
                used += len(paragraph)
                if used >= max_chars:
                    break
            if candidate_paragraphs:
                selected = book
                paragraphs = candidate_paragraphs
                break
        if selected is None:
            raise FetchError("could not load a printable Gutenberg book")

        title = selected.get("title") or "(untitled)"
        author = selected.get("author") or ""
        header = [title]
        if author:
            header.append("by " + author)
        header.append("")
        ctx.emit(f"selected book {selected['id']}: {title}")
        return [Block("FROM PROJECT GUTENBERG", tuple(header + paragraphs))]
