"""Hacker News top stories of the week, each with a code to print it.

The list shows every story beside a QR code. Scanning one prints the
article (reader mode of the linked page, or the post's own text), and each
row says roughly how much paper that would take.
"""

from __future__ import annotations

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
    Rule,
    Text,
)

TOP_STORIES = "https://hacker-news.firebaseio.com/v0/topstories.json"
ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"
DISCUSSION = "https://news.ycombinator.com/item?id={}"
#: How long a printed list's codes work.
CODE_HOURS = 14 * 24


def article(ctx: ModuleContext, story: dict) -> list[Element]:
    """The story's article as blocks: the linked page in reader mode, or the
    post's own text (Ask HN and the like). Raises FetchError."""
    url = story.get("url")
    if url:
        return ctx.reader.article(url)
    text = story.get("text")
    if not text:
        raise FetchError("this story has no article or text")
    return ctx.reader.html(
        text, DISCUSSION.format(story.get("id")), story.get("title") or ""
    )


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
        "days, each with a QR code that prints the article when scanned."
    )

    defaults = {
        "enabled": False,
        "interval_seconds": 604800,
        "limits": {"max_items": 5, "max_chars": 1500},
        "options": {
            "window_days": 7,
            "min_score": 0,
            "scan_limit": 30,
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
        # Costs one page download per listed story on every run.
        "paper_estimates": {"type": "bool", "label": "Show paper estimates"},
    }

    def fetch(self, ctx: ModuleContext) -> list[Element]:
        top = ctx.http.get_json(TOP_STORIES)
        if not isinstance(top, list):
            raise FetchError("unexpected topstories response")
        scan_limit = int(ctx.options.get("scan_limit", 30))
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
        stories = stories[: int(ctx.limits["max_items"])]
        ctx.emit(
            f"selected {len(stories)} stories from {len(candidate_ids)} candidates"
        )
        blocks: list[Element] = [Heading("Top stories this week")]
        if not stories:
            return blocks + [Text("No qualifying stories.")]

        lengths = self._lengths(ctx, stories)
        choices: list[Choice] = []
        bars: list[Bar] = []
        for position, story in enumerate(stories, start=1):
            score = int(story.get("score", 0))
            note = [
                f"{score} points",
                f"{int(story.get('descendants', 0))} comments",
                site(story),
            ]
            if lengths is not None:
                mm = lengths.get(story.get("id"))
                note.append(paper_length(mm) if mm else "length unknown")
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

    def _lengths(self, ctx: ModuleContext, stories: list[dict]) -> dict | None:
        """Paper each story's article would use, in mm, by story id; stories
        that cannot be read are left out. None when estimates are off."""
        if not ctx.options.get("paper_estimates", True):
            return None

        def measure(story: dict) -> int:
            return ctx.paper_mm(article(ctx, story))

        lengths: dict = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(measure, story): story for story in stories}
            for future in as_completed(futures):
                story_id = futures[future].get("id")
                try:
                    lengths[story_id] = future.result()
                except Exception as exc:
                    ctx.emit(f"story {story_id}: no paper estimate: {exc}", "debug")
        return lengths

    def on_select(self, ctx: ModuleContext, payload) -> list[Element]:
        """Print the chosen story's article."""
        story_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(story_id, int):
            raise FetchError("this code does not name a story")
        story = ctx.http.get_json(ITEM.format(story_id))
        if not isinstance(story, dict) or story.get("dead") or story.get("deleted"):
            raise FetchError(f"story {story_id} is no longer available")
        ctx.emit(f"printing story {story_id}: {story.get('title')}")
        return article(ctx, story)
