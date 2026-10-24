"""Google Tasks printer using a refresh token from scripts/google_oauth.py."""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

from gpta.module_api import MAX_ITEMS_LIMIT, Block, FetchError, Module, ModuleContext

TOKEN_URL = "https://oauth2.googleapis.com/token"
LISTS_URL = "https://tasks.googleapis.com/tasks/v1/users/@me/lists?maxResults=100"
TASKS_URL = "https://tasks.googleapis.com/tasks/v1/lists/{}/tasks?showCompleted={}&maxResults=100"


def _credentials_path(ctx: ModuleContext) -> Path:
    configured = str(ctx.options.get("credentials_file") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = ctx.data_dir / path
        return path
    return ctx.data_dir / "google_credentials.json"


def _load_credentials(ctx: ModuleContext) -> dict:
    path = _credentials_path(ctx)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FetchError(
            f"Google credentials not found at {path}; run scripts/google_oauth.py first"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FetchError(f"invalid Google credentials at {path}") from exc
    for key in ("client_id", "client_secret", "refresh_token"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise FetchError(f"Google credentials at {path} missing {key}")
    return data


def _access_token(ctx: ModuleContext, credentials: dict) -> str:
    response = ctx.http.post_form_json(
        TOKEN_URL,
        {
            "client_id": credentials["client_id"],
            "client_secret": credentials["client_secret"],
            "refresh_token": credentials["refresh_token"],
            "grant_type": "refresh_token",
        },
    )
    token = response.get("access_token") if isinstance(response, dict) else None
    if not isinstance(token, str) or not token:
        raise FetchError("Google token refresh did not return an access token")
    ctx.emit("refreshed Google access token")
    return token


def _pick_list(items: list[dict], wanted: str) -> tuple[str, str]:
    if wanted and wanted != "@default":
        for item in items:
            if item.get("id") == wanted:
                return wanted, item.get("title") or wanted
        return wanted, wanted
    for item in items:
        if item.get("title") == "My Tasks" and item.get("id"):
            return item["id"], item["title"]
    if items and items[0].get("id"):
        return items[0]["id"], items[0].get("title") or items[0]["id"]
    return "", "Google Tasks"


def _sort_key(task: dict):
    due = task.get("due") or ""
    return (due == "", due, task.get("position") or "", task.get("title") or "")


class GoogleTasksModule(Module):
    id = "google_tasks"
    name = "Google Tasks"
    description = "Prints pending tasks from a Google Tasks list."

    defaults = {
        "enabled": False,
        "interval_seconds": 3600,
        "limits": {"max_items": 15, "max_chars": 2000},
        "options": {
            "credentials_file": "",
            "task_list_id": "@default",
            "show_completed": False,
        },
    }
    limits_schema = {"max_items": MAX_ITEMS_LIMIT}
    options_schema = {
        "credentials_file": {
            "type": "str",
            "label": "Credentials file (blank = data/google_credentials.json)",
            "max_length": 500,
        },
        "task_list_id": {
            "type": "str",
            "label": "Task list ID",
            "max_length": 200,
        },
        "show_completed": {
            "type": "bool",
            "label": "Include completed tasks",
        },
    }

    def fetch(self, ctx: ModuleContext) -> list[Block]:
        credentials = _load_credentials(ctx)
        token = _access_token(ctx, credentials)
        headers = {"Authorization": f"Bearer {token}"}

        lists = ctx.http.get_json(LISTS_URL, headers=headers)
        items = lists.get("items", []) if isinstance(lists, dict) else []
        list_id, list_title = _pick_list(
            items, str(ctx.options.get("task_list_id") or "@default")
        )
        if not list_id:
            return [Block("TASKS", ("No task lists found.",))]

        show_completed = bool(ctx.options.get("show_completed", False))
        url = TASKS_URL.format(
            urllib.parse.quote(list_id, safe=""), "true" if show_completed else "false"
        )
        response = ctx.http.get_json(url, headers=headers)
        tasks = response.get("items", []) if isinstance(response, dict) else []
        tasks = [task for task in tasks if isinstance(task, dict)]
        if not show_completed:
            tasks = [task for task in tasks if task.get("status") != "completed"]
        tasks.sort(key=_sort_key)
        tasks = tasks[: int(ctx.limits["max_items"])]

        lines: list[str] = []
        for task in tasks:
            title = task.get("title") or "(untitled)"
            lines.append(f"[ ] {title}")
            due = task.get("due")
            if isinstance(due, str) and due:
                lines.append(f"  due {due[:10]}")
            notes = task.get("notes")
            if isinstance(notes, str) and notes.strip():
                first = notes.strip().splitlines()[0]
                lines.append(f"  {first[:60]}")
        if not lines:
            lines = ["No pending tasks."]
        ctx.emit(f"selected {len(tasks)} tasks from {list_title}")
        return [Block(f"TASKS - {list_title}", tuple(lines))]
