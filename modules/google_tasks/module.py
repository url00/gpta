"""Google Tasks: a morning list, then a fresh one whenever the tasks change.

The module runs every few minutes. The first run at or after the morning
time prints the day's list; later runs print only when the list differs from
the last one printed, and otherwise raise ``Skip``. Before the morning time
nothing is fetched. A manual run always prints.

Access uses an OAuth refresh token for a Desktop-app client, written once by
``scripts/google_oauth.py`` (see the README for the Google Cloud setup).
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from dataclasses import astuple
from datetime import UTC, date, datetime, time, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from gpta.module_api import (
    MAX_ITEMS_LIMIT,
    Checkbox,
    Element,
    FetchError,
    Heading,
    Module,
    ModuleContext,
    Skip,
    Text,
)

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://tasks.googleapis.com/tasks/v1"
LISTS_URL = f"{API}/users/@me/lists?maxResults=100"
DEFAULT_LIST = "@default"
ALL_LISTS = "*"
MAX_PAGES = 10  # of 100 tasks each, per list
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def tasks_url(list_id: str, **params: str) -> str:
    query = urllib.parse.urlencode({"maxResults": "100", **params})
    return f"{API}/lists/{urllib.parse.quote(list_id, safe='@')}/tasks?{query}"


def pending_url(list_id: str) -> str:
    return tasks_url(list_id, showCompleted="false")


def completed_url(list_id: str, since: datetime) -> str:
    # Tasks ticked off in Google's own apps are "hidden" as well as completed.
    return tasks_url(
        list_id,
        showCompleted="true",
        showHidden="true",
        completedMin=since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


# -- credentials -------------------------------------------------------
def _credentials_path(ctx: ModuleContext) -> Path:
    configured = str(ctx.options.get("credentials_file") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else ctx.data_dir / path
    return ctx.data_dir / "google_credentials.json"


def _load_credentials(ctx: ModuleContext) -> dict:
    path = _credentials_path(ctx)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FetchError(
            f"Google credentials not found at {path}; run scripts/google_oauth.py"
        ) from exc
    except json.JSONDecodeError as exc:
        raise FetchError(f"invalid Google credentials at {path}") from exc
    for key in ("client_id", "client_secret", "refresh_token"):
        if not isinstance(data, dict) or not isinstance(data.get(key), str):
            raise FetchError(f"Google credentials at {path} missing {key}")
    return data


def _access_token(ctx: ModuleContext, credentials: dict) -> str:
    try:
        response = ctx.http.post_form_json(
            TOKEN_URL,
            {
                "client_id": credentials["client_id"],
                "client_secret": credentials["client_secret"],
                "refresh_token": credentials["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
    except FetchError as exc:
        if getattr(exc, "status", None) in (400, 401):
            raise FetchError(
                "Google rejected the saved sign-in (expired or revoked); run "
                "scripts/google_oauth.py again. If this happens weekly, the "
                "OAuth app is still in 'Testing': publish it to production."
            ) from exc
        raise
    token = response.get("access_token") if isinstance(response, dict) else None
    if not isinstance(token, str) or not token:
        raise FetchError("Google token refresh did not return an access token")
    return token


# -- fetching ----------------------------------------------------------
def _pick_lists(ctx: ModuleContext, headers: dict) -> list[tuple[str, str]]:
    """``(id, title)`` of each list to print."""
    wanted = str(ctx.options.get("task_list") or "").strip()
    if not wanted:
        return [(DEFAULT_LIST, "Tasks")]
    response = ctx.http.get_json(LISTS_URL, headers=headers)
    items = response.get("items") if isinstance(response, dict) else None
    lists = [
        (item["id"], str(item.get("title") or item["id"]))
        for item in items or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    if wanted == ALL_LISTS:
        return lists
    for list_id, title in lists:
        if wanted in (list_id, title) or wanted.casefold() == title.casefold():
            return [(list_id, title)]
    names = ", ".join(title for _, title in lists) or "none"
    raise FetchError(f"no Google task list named {wanted!r} (found: {names})")


def _all_pages(ctx: ModuleContext, url: str, headers: dict) -> list[dict]:
    tasks: list[dict] = []
    page_url = url
    for _ in range(MAX_PAGES):
        response = ctx.http.get_json(page_url, headers=headers)
        if not isinstance(response, dict):
            break
        tasks.extend(t for t in response.get("items") or [] if isinstance(t, dict))
        token = response.get("nextPageToken")
        if not isinstance(token, str) or not token:
            break
        page_url = f"{url}&pageToken={urllib.parse.quote(token, safe='')}"
    # Google keeps blank rows as tasks with an empty title.
    return [t for t in tasks if str(t.get("title") or "").strip()]


def _sort_key(task: dict):
    due = task.get("due") or ""
    return (due == "", due, task.get("position") or "", task.get("title") or "")


def _due_label(due: object, today: date) -> str:
    """``2026-10-20T00:00:00.000Z`` -> ``Oct 20`` (or ``Today``, ``Overdue``).
    Google stores only the date of a due time."""
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", due) if isinstance(due, str) else None
    if not match:
        return ""
    try:
        day = date(*map(int, match.groups()))
    except ValueError:
        return ""
    if day == today:
        return "Today"
    if day < today:
        return "Overdue"
    return f"{MONTHS[day.month - 1]} {day.day}"


def _checkbox(task: dict, today: date) -> Checkbox:
    done = task.get("status") == "completed"
    notes = task.get("notes")
    lines = notes.strip().splitlines() if isinstance(notes, str) else []
    return Checkbox(
        str(task["title"]).strip(),
        checked=done,
        value="" if done else _due_label(task.get("due"), today),
        note=lines[0][:80] if lines else "",
    )


def _list_blocks(
    title: str, pending: list[dict], done: list[dict], today: date, limit: int
) -> list[Element]:
    blocks: list[Element] = [Heading(title)]
    summary = f"{len(pending)} open"
    if done:
        summary += f", {len(done)} done today"
    blocks.append(Text(summary, size="small"))
    tasks = sorted(pending, key=_sort_key) + sorted(
        done, key=lambda t: str(t.get("completed") or "")
    )
    blocks.extend(_checkbox(task, today) for task in tasks[:limit])
    if len(tasks) > limit:
        blocks.append(Text(f"+{len(tasks) - limit} more", size="small"))
    return blocks


# -- schedule ----------------------------------------------------------
def _zone(ctx: ModuleContext) -> tzinfo | None:
    name = str(ctx.options.get("timezone") or "").strip()
    if not name:
        return None  # the server's local time zone
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError) as exc:
        raise FetchError(f"unknown time zone {name!r}") from exc


def _morning(ctx: ModuleContext) -> time:
    raw = str(ctx.options.get("morning_time") or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise FetchError(f"morning time must be HH:MM (24-hour), not {raw!r}")
    return time(int(match.group(1)), int(match.group(2)))


def _fingerprint(blocks: list[Element]) -> str:
    raw = json.dumps([[type(b).__name__, *astuple(b)] for b in blocks], default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class GoogleTasksModule(Module):
    id = "google_tasks"
    name = "Google Tasks"
    description = (
        "Prints your Google Tasks each morning, then again whenever they change."
    )

    defaults = {
        "enabled": False,
        "interval_seconds": 300,
        "limits": {"max_items": 30, "max_chars": 3000},
        "options": {
            "morning_time": "08:00",
            "timezone": "",
            "task_list": "",
            "show_completed": True,
            "credentials_file": "",
        },
    }
    limits_schema = {"max_items": {**MAX_ITEMS_LIMIT, "label": "Max tasks per list"}}
    options_schema = {
        "morning_time": {
            "type": "str",
            "label": "First print of the day (HH:MM, 24-hour)",
            "max_length": 5,
        },
        "timezone": {
            "type": "str",
            "label": "Time zone, e.g. America/Toronto (blank = server's)",
            "max_length": 60,
        },
        "task_list": {
            "type": "str",
            "label": "Task list name (blank = default list, * = all lists)",
            "max_length": 200,
        },
        "show_completed": {
            "type": "bool",
            "label": "Show tasks completed today as ticked",
        },
        "credentials_file": {
            "type": "str",
            "label": "Credentials file (blank = data/google_credentials.json)",
            "max_length": 500,
        },
    }

    def fetch(self, ctx: ModuleContext) -> list[Element]:
        local = ctx.now.astimezone(_zone(ctx))
        morning = _morning(ctx)
        manual = ctx.trigger == "manual"
        if not manual and local.time() < morning:
            raise Skip(f"waiting for {morning:%H:%M}")

        token = _access_token(ctx, _load_credentials(ctx))
        headers = {"Authorization": f"Bearer {token}"}
        today = local.date()
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        limit = int(ctx.limits["max_items"])

        blocks: list[Element] = []
        for list_id, title in _pick_lists(ctx, headers):
            pending = [
                task
                for task in _all_pages(ctx, pending_url(list_id), headers)
                if task.get("status") != "completed"
            ]
            done: list[dict] = []
            if ctx.options.get("show_completed", True):
                done = [
                    task
                    for task in _all_pages(
                        ctx, completed_url(list_id, midnight), headers
                    )
                    if task.get("status") == "completed"
                ]
            blocks.extend(_list_blocks(title, pending, done, today, limit))
        if not blocks:
            blocks = [Heading("Tasks"), Text("No task lists found.")]

        fingerprint = _fingerprint(blocks)
        first_today = ctx.store.get("printed_day") != today.isoformat()
        if (
            not manual
            and not first_today
            and ctx.store.get("fingerprint") == fingerprint
        ):
            raise Skip("no task changes")
        if local.time() >= morning:  # an early manual run is not the morning list
            ctx.store.set("printed_day", today.isoformat())
        ctx.store.set("fingerprint", fingerprint)
        ctx.emit("morning list" if first_today else "tasks changed; printing new list")
        return blocks
