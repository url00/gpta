"""Hacker News top stories of the week, each with a code to print it.

The list shows every story beside a QR code. Scanning one prints the
article (reader mode of the linked page, or the post's own text), and each
row says roughly how much paper that would take.

By default only stories that read well on a receipt are listed: enough
prose, and little that a thermal printer cannot show (pictures, video,
code listings, tables). See :func:`unprintable`.

The story's comments print as a second job after the article: the first few
top-level threads with replies two levels deep, indented by depth. Long
replies are cut short, with a code to read the rest on a phone.
"""

from __future__ import annotations

import html
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

from gpta.module_api import (
    MAX_ITEMS_LIMIT,
    Choice,
    Document,
    Element,
    FetchError,
    Heading,
    Module,
    ModuleContext,
    QrCode,
    QrItem,
    Reading,
    Rule,
    Text,
)

TOP_STORIES = "https://hacker-news.firebaseio.com/v0/topstories.json"
ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"
DISCUSSION = "https://news.ycombinator.com/item?id={}"
#: How long a printed list's codes work.
CODE_HOURS = 14 * 24


# -- what reads well on a receipt ------------------------------------
#: One picture (a lead photo, say) is fine, plus one per this many words.
WORDS_PER_PICTURE = 2000
#: At most this share of the words may be code listings.
MAX_CODE_SHARE = 0.15
#: At most one table row per this many words.
WORDS_PER_TABLE_ROW = 100
#: Titles Hacker News marks as not being an article.
NOT_TEXT_TITLE = re.compile(r"\[(video|pdf|audio|slides|podcast)\]", re.I)
#: Stop reading candidate pages after this long and list what qualified.
READ_BUDGET_SECONDS = 60
READ_WORKERS = 8


def read(ctx: ModuleContext, story: dict) -> Reading:
    """The story's article: the linked page in reader mode, or the post's
    own text (Ask HN and the like). Raises FetchError."""
    url = story.get("url")
    if url:
        return ctx.reader.read(url)
    text = story.get("text")
    if not text:
        raise FetchError("this story has no article or text")
    return ctx.reader.read_html(
        text, DISCUSSION.format(story.get("id")), story.get("title") or ""
    )


def unprintable(story: dict, reading: Reading, min_words: int) -> str:
    """Why ``reading`` would not read well on a receipt, or "" if it would.

    A rough judgement from counts, not from understanding the page: enough
    prose, and not much that was left out or prints badly.
    """
    if NOT_TEXT_TITLE.search(story.get("title") or ""):
        return "not an article"
    prose = reading.words - reading.code_words
    if prose < min_words:
        return f"short ({prose} words)"
    if reading.media > 1 + prose // WORDS_PER_PICTURE:
        return f"relies on pictures ({reading.media})"
    if reading.code_words > reading.words * MAX_CODE_SHARE:
        return "mostly code"
    if reading.table_rows > prose // WORDS_PER_TABLE_ROW:
        return f"relies on tables ({reading.table_rows} rows)"
    return ""


# -- comments ----------------------------------------------------------
#: Replies are printed this many levels below a top-level comment.
REPLY_DEPTH = 2
#: Extra comments fetched per parent, to replace dead or deleted ones.
SPARE_COMMENTS = 2
COMMENT_WORKERS = 16
#: Stop measuring comment threads after this long; the rest say "unknown".
COMMENT_BUDGET_SECONDS = 30
CUT_NOTE = "Cut short. Scan to read the rest on Hacker News."

_PARAGRAPH = re.compile(r"<p>", re.I)
_LINK = re.compile(r"<a\s[^>]*href=\"([^\"]*)\"[^>]*>.*?</a>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")


def paragraphs(text: str) -> list[tuple[str, bool]]:
    """A comment's HTML as ``(text, is_code)`` paragraphs. Links print as
    their address, since Hacker News shortens the visible text."""
    out = []
    for part in _PARAGRAPH.split(text or ""):
        code = "<pre>" in part.lower()
        part = html.unescape(_TAG.sub("", _LINK.sub(r"\1", part)))
        part = part.strip("\n") if code else " ".join(part.split())
        if part.strip():
            out.append((part, code))
    return out


def shorten(parts: list[tuple[str, bool]], limit: int) -> tuple[list, bool]:
    """``parts`` cut to ``limit`` characters at a word, ending in an
    ellipsis; and whether anything was cut. ``limit`` 0 keeps everything."""
    if limit <= 0 or sum(len(text) for text, _code in parts) <= limit:
        return parts, False
    out, room = [], limit
    for text, code in parts:
        if len(text) <= room:
            out.append((text, code))
            room -= len(text)
            continue
        cut = text[:room].rsplit(None, 1)[0] if " " in text[:room] else text[:room]
        if cut.strip():
            out.append((cut.rstrip(" ,;:.") + "…", code))
        elif out:
            out[-1] = (out[-1][0] + " …", out[-1][1])
        break
    return out, True


def age(now: float, then: float) -> str:
    minutes = max(0, int(now - then)) // 60
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 48 * 60:
        return f"{minutes // 60}h"
    return f"{minutes // (24 * 60)}d"


def _live(item) -> bool:
    return (
        isinstance(item, dict)
        and item.get("type") == "comment"
        and not item.get("dead")
        and not item.get("deleted")
        and bool(item.get("text"))
    )


def load_threads(ctx: ModuleContext, story: dict, pool: ThreadPoolExecutor) -> list:
    """The story's first comment threads, in Hacker News's own order, as
    nested ``{"item", "replies", "more"}`` nodes: ``comment_threads``
    top-level comments, and up to ``comment_replies`` replies under each for
    :data:`REPLY_DEPTH` levels. ``more`` counts the replies not shown."""
    threads = int(ctx.options.get("comment_threads", 5))
    replies = int(ctx.options.get("comment_replies", 3))

    def get(comment_id):
        try:
            return ctx.http.get_json(ITEM.format(comment_id))
        except Exception as exc:
            ctx.emit(f"comment {comment_id} fetch failed: {exc}", "debug")
            return None

    def children(parents: list[dict], limit: int) -> list[dict]:
        """Fill in each parent's replies, one request per comment, all
        parents' at once. Returns the new nodes."""
        wanted = [
            [k for k in parent["item"].get("kids") or [] if isinstance(k, int)]
            for parent in parents
        ]
        take = limit + SPARE_COMMENTS if limit else 0
        ids = [kid for kids in wanted for kid in kids[:take]]
        items = dict(zip(ids, pool.map(get, ids), strict=True))
        made = []
        for parent, kids in zip(parents, wanted, strict=True):
            live = [items[kid] for kid in kids[:take] if _live(items[kid])][:limit]
            parent["replies"] = [{"item": i, "replies": [], "more": 0} for i in live]
            parent["more"] = max(0, len(kids) - len(live))
            made += parent["replies"]
        return made

    root = {"item": story}
    level = children([root], threads)
    for _depth in range(REPLY_DEPTH):
        level = children(level, replies)
    for node in level:  # the deepest level shown: its replies are all unseen
        node["more"] = len(node["item"].get("kids") or [])
    return root["replies"]


def comment_blocks(ctx: ModuleContext, story: dict, threads: list) -> list[Element]:
    """The comment threads laid out for the receipt: each comment under its
    author, replies indented by depth, a rule between threads."""
    now = ctx.now.timestamp()
    reply_chars = int(ctx.options.get("reply_chars", 600))
    link = DISCUSSION.format(story.get("id"))
    total = int(story.get("descendants", 0))
    blocks: list[Element] = [
        Heading(story.get("title") or "(untitled)"),
        Text(
            f"Comments: the first {len(threads)} threads of {total} comments, "
            f"replies {REPLY_DEPTH} deep. Scan for the whole discussion.",
            size="small",
        ),
        QrCode(link),
        Rule(heavy=True),
    ]

    def add(node: dict, depth: int) -> None:
        item = node["item"]
        parts, cut = shorten(paragraphs(item.get("text")), reply_chars if depth else 0)
        head = f"{item.get('by') or '?'} · {age(now, int(item.get('time', now)))}"
        if parts and not parts[0][1]:
            first = f"{head}\n{parts[0][0]}"
            blocks.append(Text(first, bold_spans=((0, len(head)),), indent=depth))
            parts = parts[1:]
        else:
            blocks.append(Text(head, bold=True, indent=depth))
        for text, code in parts:
            blocks.append(Text(text, size="small" if code else "body", indent=depth))
        if cut:
            blocks.append(
                QrItem(
                    DISCUSSION.format(item.get("id")),
                    "",
                    CUT_NOTE,
                    qr="small",
                    indent=depth,
                )
            )
        for reply in node["replies"]:
            add(reply, depth + 1)
        if node["more"]:
            more = node["more"]
            blocks.append(
                Text(
                    f"+ {more} more {'reply' if more == 1 else 'replies'}",
                    size="small",
                    indent=depth + 1,
                )
            )

    for number, thread in enumerate(threads):
        if number:
            blocks.append(Rule())
        add(thread, 0)
    return blocks


def paper_length(mm: int) -> str:
    """A rough paper length: "about 45 cm", "about 1.2 m"."""
    if mm >= 975:  # would round to 100 cm
        return f"about {mm / 1000:.1f} m"
    return f"about {max(5, round(mm / 50) * 5)} cm"


def site(story: dict) -> str:
    host = urlsplit(story.get("url") or "").hostname or "news.ycombinator.com"
    return host.removeprefix("www.")


class HackerNewsModule(Module):
    id = "hackernews"
    name = "Hacker News Weekly"
    description = (
        "Prints the highest-scoring Hacker News stories from the last seven "
        "days that read well in print, each with a QR code that prints the "
        "article when scanned."
    )

    defaults = {
        "enabled": False,
        "interval_seconds": 604800,
        "limits": {"max_items": 10, "max_chars": 3000},
        "options": {
            "window_days": 7,
            "min_score": 0,
            "scan_limit": 60,
            "text_only": True,
            "min_words": 400,
            "paper_estimates": True,
            "comments": True,
            "comment_threads": 5,
            "comment_replies": 3,
            "reply_chars": 600,
        },
    }
    limits_schema = {"max_items": MAX_ITEMS_LIMIT}
    options_schema = {
        "window_days": {
            "type": "int",
            "label": "Window (days)",
            "min": 1,
            "max": 30,
        },
        "min_score": {
            "type": "int",
            "label": "Minimum score",
            "min": 0,
            "max": 100000,
        },
        "scan_limit": {
            "type": "int",
            "label": "Stories to scan",
            "min": 1,
            "max": 100,
        },
        # Lists only stories that read well in print; see unprintable().
        # Downloads candidate pages, best score first, until enough qualify.
        "text_only": {"type": "bool", "label": "Only text articles"},
        "min_words": {
            "type": "int",
            "label": "Shortest article (words)",
            "min": 0,
            "max": 20000,
        },
        "paper_estimates": {"type": "bool", "label": "Show paper estimates"},
        # Comments print as a second job after the article.
        "comments": {"type": "bool", "label": "Print comments too"},
        "comment_threads": {
            "type": "int",
            "label": "Comment threads",
            "min": 1,
            "max": 20,
        },
        "comment_replies": {
            "type": "int",
            "label": "Replies per comment",
            "min": 0,
            "max": 10,
        },
        # Replies longer than this are cut, with a code to read the rest.
        "reply_chars": {
            "type": "int",
            "label": "Longest reply (characters, 0 = whole)",
            "min": 0,
            "max": 5000,
        },
    }

    def fetch(self, ctx: ModuleContext) -> list[Element]:
        top = ctx.http.get_json(TOP_STORIES)
        if not isinstance(top, list):
            raise FetchError("unexpected topstories response")
        scan_limit = int(ctx.options.get("scan_limit", 60))
        candidate_ids = [
            int(item) for item in top[:scan_limit] if isinstance(item, int)
        ]
        cutoff = ctx.now.timestamp() - int(ctx.options.get("window_days", 7)) * 86400
        min_score = int(ctx.options.get("min_score", 0))

        stories = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(ctx.http.get_json, ITEM.format(story_id)): story_id
                for story_id in candidate_ids
            }
            for future in as_completed(futures):
                story_id = futures[future]
                try:
                    item = future.result()
                except Exception as exc:
                    ctx.emit(f"story {story_id} fetch failed: {exc}", "warning")
                    continue
                if not isinstance(item, dict):
                    continue
                if (
                    item.get("type") != "story"
                    or item.get("dead")
                    or item.get("deleted")
                ):
                    continue
                if int(item.get("time", 0)) < cutoff:
                    continue
                if int(item.get("score", 0)) < min_score:
                    continue
                stories.append(item)

        stories.sort(
            key=lambda item: (-int(item.get("score", 0)), int(item.get("id", 0)))
        )
        picked = self._pick(ctx, stories)
        ctx.emit(f"listing {len(picked)} of {len(stories)} stories in the window")
        blocks: list[Element] = [Heading("Top stories this week")]
        if not picked:
            return blocks + [Text("No qualifying stories.")]

        estimates = bool(ctx.options.get("paper_estimates", True))
        comments = bool(ctx.options.get("comments", True))
        threads = self._comment_lengths(ctx, picked) if estimates and comments else {}
        choices: list[Choice] = []
        for story, reading in picked:
            note = [
                f"{int(story.get('score', 0))} points",
                f"{int(story.get('descendants', 0))} comments",
                site(story),
            ]
            if estimates and comments:
                note.append(f"article {self._length(ctx, story, reading)}")
                note.append(threads.get(story.get("id"), "comments length unknown"))
            elif estimates:
                note.append(self._length(ctx, story, reading))
            choices.append(
                Choice(
                    story.get("title") or "(untitled)",
                    {"id": story.get("id")},
                    " · ".join(note),
                )
            )
        what = "that article and its comments" if comments else "that article"
        blocks.append(Text(f"Scan a code to print {what}.", size="small"))
        blocks += ctx.selections.offer(choices, expires_hours=CODE_HOURS)
        return blocks

    def _pick(
        self, ctx: ModuleContext, stories: list[dict]
    ) -> list[tuple[dict, Reading | None]]:
        """The stories to list, best score first, each with its article if
        it was read (None if it could not be, or did not need to be)."""
        max_items = int(ctx.limits["max_items"])
        text_only = bool(ctx.options.get("text_only", True))
        if not text_only:
            stories = stories[:max_items]
            if not ctx.options.get("paper_estimates", True):
                return [(story, None) for story in stories]
        min_words = int(ctx.options.get("min_words", 400))
        deadline = time.monotonic() + READ_BUDGET_SECONDS
        picked: list[tuple[dict, Reading | None]] = []
        skipped: dict[str, int] = {}

        def attempt(story: dict) -> Reading | str:
            try:
                return read(ctx, story)
            except Exception as exc:
                return str(exc) or type(exc).__name__

        with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
            # A batch at a time, so pages are not downloaded past the last
            # story needed.
            for start in range(0, len(stories), READ_WORKERS):
                if len(picked) >= max_items:
                    break
                if time.monotonic() > deadline:
                    ctx.emit("stopped reading pages: out of time", "warning")
                    break
                batch = stories[start : start + READ_WORKERS]
                for story, result in zip(batch, pool.map(attempt, batch), strict=True):
                    if isinstance(result, str):
                        reading, problem = None, "could not be read"
                        detail = result
                    else:
                        reading = result
                        problem = detail = unprintable(story, reading, min_words)
                    if not text_only:
                        picked.append((story, reading))
                        continue
                    if not problem:
                        picked.append((story, reading))
                        continue
                    reason = problem.split(" (")[0]
                    skipped[reason] = skipped.get(reason, 0) + 1
                    ctx.emit(f"skipped {story.get('id')}: {detail}", "debug")
        if skipped:
            summary = ", ".join(
                f"{n} {reason}" for reason, n in sorted(skipped.items())
            )
            ctx.emit(f"skipped as not text articles: {summary}")
        return picked[:max_items]

    def _length(self, ctx: ModuleContext, story: dict, reading: Reading | None) -> str:
        if reading is None:
            return "length unknown"
        try:
            return paper_length(ctx.paper_mm(list(reading.blocks)))
        except Exception as exc:
            ctx.emit(f"story {story.get('id')}: no paper estimate: {exc}", "debug")
            return "length unknown"

    def _comment_lengths(self, ctx: ModuleContext, picked: list) -> dict:
        """What to say about each story's comments, by story id: "comments
        about 1.4 m", or "no comments". Stories that could not be measured
        are left out."""
        deadline = time.monotonic() + COMMENT_BUDGET_SECONDS
        said: dict = {}
        with ThreadPoolExecutor(max_workers=COMMENT_WORKERS) as pool:
            for story, _reading in picked:
                if time.monotonic() > deadline:
                    ctx.emit("stopped measuring comments: out of time", "warning")
                    break
                story_id = story.get("id")
                try:
                    threads = load_threads(ctx, story, pool)
                    if not threads:
                        said[story_id] = "no comments"
                        continue
                    mm = ctx.paper_mm(comment_blocks(ctx, story, threads))
                    said[story_id] = f"comments {paper_length(mm)}"
                except Exception as exc:
                    ctx.emit(f"story {story_id}: no comments estimate: {exc}", "debug")
        return said

    def on_select(self, ctx: ModuleContext, payload) -> list[Element] | list[Document]:
        """Print the chosen story's article, then its comments as a second
        job."""
        story_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(story_id, int):
            raise FetchError("this code does not name a story")
        story = ctx.http.get_json(ITEM.format(story_id))
        if not isinstance(story, dict) or story.get("dead") or story.get("deleted"):
            raise FetchError(f"story {story_id} is no longer available")
        ctx.emit(f"printing story {story_id}: {story.get('title')}")
        article = list(read(ctx, story).blocks)
        if not ctx.options.get("comments", True):
            return article
        try:
            with ThreadPoolExecutor(max_workers=COMMENT_WORKERS) as pool:
                threads = load_threads(ctx, story, pool)
        except Exception as exc:  # the article is still worth printing
            ctx.emit(f"story {story_id}: comments not printed: {exc}", "warning")
            return article
        if not threads:
            return article
        return [Document(article), Document(comment_blocks(ctx, story, threads))]
