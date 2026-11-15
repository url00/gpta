"""Hacker News top stories of the week, each with a code to print it.

The list shows every story beside a QR code. Scanning one prints the
article (reader mode of the linked page, or the post's own text), and each
row says roughly how much paper that would take.

By default only stories that read well on a receipt are listed: enough
prose, and little that a thermal printer cannot show (pictures, video,
code listings, tables). See :func:`unprintable`.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

from gpta.module_api import (
    MAX_ITEMS_LIMIT,
    Bar,
    BarChart,
    Choice,
    Element,
    FetchError,
    Heading,
    Module,
    ModuleContext,
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
        choices: list[Choice] = []
        bars: list[Bar] = []
        for position, (story, reading) in enumerate(picked, start=1):
            score = int(story.get("score", 0))
            note = [
                f"{score} points",
                f"{int(story.get('descendants', 0))} comments",
                site(story),
            ]
            if estimates:
                note.append(self._length(ctx, story, reading))
            choices.append(
                Choice(
                    story.get("title") or "(untitled)",
                    {"id": story.get("id")},
                    " · ".join(note),
                )
            )
            bars.append(Bar(f"#{position}", score))
        blocks.append(Text("Scan a code to print that article.", size="small"))
        blocks += ctx.selections.offer(choices, expires_hours=CODE_HOURS)
        return blocks + [Rule(), BarChart(tuple(bars), title="Points")]

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

    def on_select(self, ctx: ModuleContext, payload) -> list[Element]:
        """Print the chosen story's article."""
        story_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(story_id, int):
            raise FetchError("this code does not name a story")
        story = ctx.http.get_json(ITEM.format(story_id))
        if not isinstance(story, dict) or story.get("dead") or story.get("deleted"):
            raise FetchError(f"story {story_id} is no longer available")
        ctx.emit(f"printing story {story_id}: {story.get('title')}")
        return list(read(ctx, story).blocks)
