"""Google Calendar: the day's itinerary each morning, reprinted when it changes.

Runs every few minutes, like the Google Tasks module. The first run at or
after the morning time prints today's events; later runs print only when the
itinerary differs from the last one printed (an event added, moved, renamed
or removed) and otherwise raise ``Skip``. Before the morning time nothing is
fetched. A manual run always prints.

It shares ``data/google_credentials.json`` with Google Tasks. The sign-in
must include the Calendar scope: run ``scripts/google_oauth.py`` again if it
was made before this module existed.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from dataclasses import astuple
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from gpta.module_api import (
    MAX_ITEMS_LIMIT,
    Element,
    FetchError,
    Heading,
    Module,
    ModuleContext,
    Skip,
    Text,
    fingerprint,
)

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
CALENDARS_URL = f"{API}/users/me/calendarList?maxResults=250"
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
PRIMARY = "primary"
ALL_CALENDARS = "*"
MAX_PAGES = 4  # of 250 events each, per calendar
DAYS = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split()
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
# Entries that are not things to attend.
HIDDEN_TYPES = {"workingLocation"}


def events_url(calendar_id: str, start: datetime, end: datetime) -> str:
    def stamp(moment: datetime) -> str:
        return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    query = urllib.parse.urlencode(
        {
            "timeMin": stamp(start),
            "timeMax": stamp(end),
            "singleEvents": "true",  # expand repeating events
            "orderBy": "startTime",
            "maxResults": "250",
        }
    )
    return f"{API}/calendars/{urllib.parse.quote(calendar_id, safe='')}/events?{query}"


# -- credentials (the same file as the Google Tasks module) ------------
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
    if SCOPE not in str(data.get("scope") or "").split():
        raise FetchError(
            "the saved Google sign-in does not include Calendar; "
            "run scripts/google_oauth.py again"
        )
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


def _get(ctx: ModuleContext, url: str, headers: dict) -> dict:
    try:
        response = ctx.http.get_json(url, headers=headers)
    except FetchError as exc:
        if getattr(exc, "status", None) == 403:
            raise FetchError(
                "Google refused the Calendar request (403); enable the Google "
                "Calendar API for the project, then run scripts/google_oauth.py "
                "again if it still fails"
            ) from exc
        raise
    return response if isinstance(response, dict) else {}


# -- fetching ----------------------------------------------------------
def _pick_calendars(ctx: ModuleContext, headers: dict) -> list[str]:
    wanted = str(ctx.options.get("calendar") or "").strip()
    if not wanted:
        return [PRIMARY]
    calendars = [
        item
        for item in _get(ctx, CALENDARS_URL, headers).get("items") or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    if wanted == ALL_CALENDARS:
        # Those ticked in the Google Calendar sidebar.
        return [item["id"] for item in calendars if item.get("selected")]
    names = []
    for item in calendars:
        name = str(item.get("summaryOverride") or item.get("summary") or item["id"])
        names.append(name)
        if wanted == item["id"] or wanted.casefold() == name.casefold():
            return [item["id"]]
    found = ", ".join(names) or "none"
    raise FetchError(f"no Google calendar named {wanted!r} (found: {found})")


def _events(ctx: ModuleContext, url: str, headers: dict) -> list[dict]:
    events: list[dict] = []
    page_url = url
    for _ in range(MAX_PAGES):
        response = _get(ctx, page_url, headers)
        events.extend(e for e in response.get("items") or [] if isinstance(e, dict))
        token = response.get("nextPageToken")
        if not isinstance(token, str) or not token:
            break
        page_url = f"{url}&pageToken={urllib.parse.quote(token, safe='')}"
    return events


def _declined(event: dict) -> bool:
    return any(
        isinstance(person, dict)
        and person.get("self")
        and person.get("responseStatus") == "declined"
        for person in event.get("attendees") or []
    )


def _moment(value: object, zone: tzinfo | None) -> datetime | date | None:
    """An event's ``start``/``end``: a local datetime, or a date if all-day."""
    if not isinstance(value, dict):
        return None
    try:
        if isinstance(value.get("dateTime"), str):
            return datetime.fromisoformat(value["dateTime"]).astimezone(zone)
        if isinstance(value.get("date"), str):
            return date.fromisoformat(value["date"])
    except ValueError:
        pass
    return None


def _clock(moment: datetime, twelve_hour: bool) -> str:
    if not twelve_hour:
        return f"{moment:%H:%M}"
    return f"{moment.hour % 12 or 12}:{moment:%M} {'AM' if moment.hour < 12 else 'PM'}"


def _entry(
    event: dict, day_start: datetime, day_end: datetime, twelve_hour: bool
) -> tuple[datetime | None, str, str, str] | None:
    """``(start or None if all day, time label, title, location)`` for an
    event to print, or None for one to leave out."""
    if event.get("status") == "cancelled" or event.get("eventType") in HIDDEN_TYPES:
        return None
    if _declined(event):
        return None
    zone = day_start.tzinfo
    start, end = _moment(event.get("start"), zone), _moment(event.get("end"), zone)
    if start is None:
        return None
    title = str(event.get("summary") or "").strip() or "(no title)"
    location = str(event.get("location") or "").strip().splitlines()
    place = location[0][:80] if location else ""
    if not isinstance(start, datetime):
        return None, "", title, place
    if not isinstance(end, datetime):
        end = start
    begins_today, ends_today = start >= day_start, end <= day_end
    if not begins_today and not ends_today:
        return None, "", title, place  # runs through the whole day
    if not begins_today:
        label = f"until {_clock(end, twelve_hour)}"
    elif not ends_today:
        label = f"from {_clock(start, twelve_hour)}"
    elif end > start:
        label = f"{_clock(start, twelve_hour)} - {_clock(end, twelve_hour)}"
    else:
        label = _clock(start, twelve_hour)
    return max(start, day_start), label, title, place


def _itinerary(entries: list[tuple], today: date, limit: int) -> list[Element]:
    blocks: list[Element] = [
        Heading(f"{DAYS[today.weekday()]}, {MONTHS[today.month - 1]} {today.day}")
    ]
    if not entries:
        return [*blocks, Text("Nothing scheduled.")]
    # All-day events first, then by start time; the sort is stable, so
    # events at the same time keep Google's order.
    entries = sorted(entries, key=lambda e: (e[0] is not None, e[0] or 0))
    blocks.append(
        Text(f"{len(entries)} event{'s' if len(entries) != 1 else ''}", size="small")
    )
    all_day_shown = False
    for start, label, title, place in entries[:limit]:
        if start is None and not all_day_shown:
            blocks.append(Text("All day", bold=True))
            all_day_shown = True
        if label:
            blocks.append(Text(label, bold=True))
        blocks.append(Text(title))
        if place:
            blocks.append(Text(place, size="small"))
    if len(entries) > limit:
        blocks.append(Text(f"+{len(entries) - limit} more", size="small"))
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


def _midnight(day: date, zone: tzinfo | None) -> datetime:
    naive = datetime.combine(day, time())
    return naive.replace(tzinfo=zone) if zone else naive.astimezone()


def _morning(ctx: ModuleContext) -> time:
    raw = str(ctx.options.get("morning_time") or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise FetchError(f"morning time must be HH:MM (24-hour), not {raw!r}")
    return time(int(match.group(1)), int(match.group(2)))


def _old_fingerprint(blocks: list[Element]) -> str:
    """The hash saved by 0.10.0 and earlier. It covered every field of a
    block, so it changed whenever a release added one; still recognised so
    that upgrading does not reprint an unchanged list."""
    raw = json.dumps([[type(b).__name__, *astuple(b)] for b in blocks], default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class GoogleCalendarModule(Module):
    id = "google_calendar"
    name = "Google Calendar"
    description = (
        "Prints today's itinerary each morning, then again whenever it changes."
    )

    defaults = {
        "enabled": False,
        "interval_seconds": 300,
        "limits": {"max_items": 30, "max_chars": 3000},
        "options": {
            "morning_time": "08:00",
            "timezone": "",
            "calendar": "",
            "clock": "12-hour",
            "credentials_file": "",
        },
    }
    limits_schema = {"max_items": {**MAX_ITEMS_LIMIT, "label": "Max events"}}
    options_schema = {
        "morning_time": {
            "type": "str",
            "label": "First print of the day (HH:MM, 24-hour)",
            "max_length": 5,
        },
        "timezone": {
            "type": "str",
            "label": "Time zone, e.g. America/Chicago (blank = server's)",
            "max_length": 60,
        },
        "calendar": {
            "type": "str",
            "label": "Calendar name (blank = your main one, * = all shown)",
            "max_length": 200,
        },
        "clock": {
            "type": "select",
            "label": "Event times",
            "choices": ["12-hour", "24-hour"],
        },
        "credentials_file": {
            "type": "str",
            "label": "Credentials file (blank = data/google_credentials.json)",
            "max_length": 500,
        },
    }

    def fetch(self, ctx: ModuleContext) -> list[Element]:
        zone = _zone(ctx)
        local = ctx.now.astimezone(zone)
        morning = _morning(ctx)
        manual = ctx.trigger == "manual"
        if not manual and local.time() < morning:
            raise Skip(f"waiting for {morning:%H:%M}")

        token = _access_token(ctx, _load_credentials(ctx))
        headers = {"Authorization": f"Bearer {token}"}
        today = local.date()
        day_start = _midnight(today, zone)
        day_end = _midnight(today + timedelta(days=1), zone)
        twelve_hour = ctx.options.get("clock") != "24-hour"

        entries, seen = [], set()
        for calendar_id in _pick_calendars(ctx, headers):
            url = events_url(calendar_id, day_start, day_end)
            for event in _events(ctx, url, headers):
                # An event shared between two of your calendars prints once.
                key = event.get("iCalUID") or event.get("id")
                entry = _entry(event, day_start, day_end, twelve_hour)
                if entry is None or (key and (key, entry[0]) in seen):
                    continue
                seen.add((key, entry[0]))
                entries.append(entry)
        blocks = _itinerary(entries, today, int(ctx.limits["max_items"]))

        printed = fingerprint(blocks)
        first_today = ctx.store.get("printed_day") != today.isoformat()
        if (
            not manual
            and not first_today
            and ctx.store.get("fingerprint") in (printed, _old_fingerprint(blocks))
        ):
            ctx.store.set("fingerprint", printed)
            raise Skip("no calendar changes")
        if local.time() >= morning:  # an early manual run is not the morning list
            ctx.store.set("printed_day", today.isoformat())
        ctx.store.set("fingerprint", printed)
        ctx.emit("morning itinerary" if first_today else "calendar changed; reprinting")
        return blocks
