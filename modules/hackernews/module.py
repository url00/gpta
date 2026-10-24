"""Hacker News top stories of the week."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from gpta.module_api import MAX_ITEMS_LIMIT, Block, FetchError, Module, ModuleContext

TOP_STORIES = "https://hacker-news.firebaseio.com/v0/topstories.json"
ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"


class HackerNewsModule(Module):
    id = "hackernews"
    name = "Hacker News Weekly"
    description = "Prints the highest-scoring Hacker News stories from the last seven days."

    defaults = {
        "enabled": False,
        "interval_seconds": 604800,
        "limits": {"max_items": 5, "max_chars": 1500},
        "options": {"window_days": 7, "min_score": 0, "scan_limit": 30},
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
    }

    def fetch(self, ctx: ModuleContext) -> list[Block]:
        top = ctx.http.get_json(TOP_STORIES)
        if not isinstance(top, list):
            raise FetchError("unexpected topstories response")
        scan_limit = int(ctx.options.get("scan_limit", 30))
        candidate_ids = [int(item) for item in top[:scan_limit] if isinstance(item, int)]
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
                if item.get("type") != "story" or item.get("dead") or item.get("deleted"):
                    continue
                if int(item.get("time", 0)) < cutoff:
                    continue
                if int(item.get("score", 0)) < min_score:
                    continue
                stories.append(item)

        stories.sort(key=lambda item: (-int(item.get("score", 0)), int(item.get("id", 0))))
        stories = stories[: int(ctx.limits["max_items"])]
        ctx.emit(f"selected {len(stories)} stories from {len(candidate_ids)} candidates")
        if not stories:
            return [Block("TOP STORIES THIS WEEK", ("No qualifying stories.",))]

        lines: list[str] = []
        for position, story in enumerate(stories, start=1):
            title = story.get("title") or "(untitled)"
            score = int(story.get("score", 0))
            comments = int(story.get("descendants", 0))
            url = story.get("url") or f"https://news.ycombinator.com/item?id={story.get('id')}"
            lines.extend(
                [
                    f"{position}. {title}",
                    f"   {score} points | {comments} comments",
                    f"   {url}",
                ]
            )
        return [Block("TOP STORIES THIS WEEK", tuple(lines))]
