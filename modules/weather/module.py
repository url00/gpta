"""Weather: the day's forecast each morning, plus alerts as they are issued.

Runs every few minutes, like the Google Tasks and Calendar modules. The
first run at or after the morning time prints today's forecast with any
alerts in effect. Every run, day and night, also checks for weather alerts
and prints each new one once; a run with nothing new raises ``Skip``. A
manual run always prints the forecast.

The forecast comes from Open-Meteo and alerts from the US National Weather
Service (so alerts cover the United States only); neither needs an account.
With no coordinates set, the server's location is looked up from its public
IP address once a day.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

from gpta.module_api import (
    Element,
    FetchError,
    Heading,
    KeyValue,
    Module,
    ModuleContext,
    Rule,
    Skip,
    Text,
)

LOCATE_URL = "https://ipapi.co/json/"
FORECAST_API = "https://api.open-meteo.com/v1/forecast"
ALERTS_API = "https://api.weather.gov/alerts/active"
LOCATION_MAX_AGE = timedelta(days=1)
HOURLY_STEP = 3  # hours between the rows of the outlook
HOURLY_ROWS = 6
ALERT_TEXT_LIMIT = 700  # characters of an alert's description or instruction
DAYS = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split()
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
COMPASS = "N NE E SE S SW W NW".split()

# WMO weather interpretation codes, as Open-Meteo reports them.
CONDITIONS = {
    0: "Clear",
    1: "Mostly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Freezing fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Heavy freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Heavy freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Showers",
    82: "Heavy showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorms",
    96: "Thunderstorms, hail",
    99: "Thunderstorms, heavy hail",
}

# NWS severities, least to most serious; each choice is the lowest printed.
SEVERITIES = ["Unknown", "Minor", "Moderate", "Severe", "Extreme"]
ALERT_CHOICES = {
    "all": "Unknown",
    "moderate and up": "Moderate",
    "severe and up": "Severe",
}
ALERTS_OFF = "off"

UNITS = {
    "imperial": {
        "query": {
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
        },
        "wind": "mph",
        "precip": '"',
    },
    "metric": {"query": {}, "wind": "km/h", "precip": " mm"},
}


def _coordinate(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def forecast_url(lat: float, lon: float, units: str, zone_name: str = "") -> str:
    query = {
        "latitude": _coordinate(lat),
        "longitude": _coordinate(lon),
        "current": "temperature_2m,apparent_temperature,weather_code",
        "hourly": "temperature_2m,weather_code,precipitation_probability",
        "daily": (
            "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max,precipitation_sum,"
            "wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant,"
            "sunrise,sunset,uv_index_max"
        ),
        "forecast_days": "1",
        "timezone": zone_name or "auto",
        **UNITS[units]["query"],
    }
    return f"{FORECAST_API}?{urllib.parse.urlencode(query)}"


def alerts_url(lat: float, lon: float) -> str:
    return f"{ALERTS_API}?point={_coordinate(lat)},{_coordinate(lon)}"


# -- location ----------------------------------------------------------
def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number == number and abs(number) != float("inf") else None


def _valid(lat: float | None, lon: float | None) -> bool:
    return lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180


def _location(ctx: ModuleContext, refresh: bool) -> tuple[float, float, str]:
    """``(latitude, longitude, place name)``: the configured coordinates, or
    the server's own, looked up from its IP address and remembered."""
    raw_lat = str(ctx.options.get("latitude") or "").strip()
    raw_lon = str(ctx.options.get("longitude") or "").strip()
    if raw_lat or raw_lon:
        lat, lon = _number(raw_lat), _number(raw_lon)
        if not _valid(lat, lon):
            raise FetchError(
                "latitude and longitude must both be numbers, e.g. 44.81 and "
                "-91.5 (or both blank for the server's location)"
            )
        return lat, lon, ""

    saved = ctx.store.get("location")
    if not (isinstance(saved, dict) and _valid(saved.get("lat"), saved.get("lon"))):
        saved = None
    if saved and not refresh:
        try:
            age = ctx.now - datetime.fromisoformat(str(saved.get("at")))
            if timedelta(0) <= age < LOCATION_MAX_AGE:
                return saved["lat"], saved["lon"], str(saved.get("name") or "")
        except (ValueError, TypeError):
            pass
    try:
        found = ctx.http.get_json(LOCATE_URL)
        if not isinstance(found, dict):
            found = {}
        lat, lon = _number(found.get("latitude")), _number(found.get("longitude"))
        if not _valid(lat, lon):
            raise FetchError("the location lookup returned no coordinates")
    except FetchError as exc:
        if saved:  # a day-old location beats no forecast
            ctx.emit(f"location lookup failed, using the last one: {exc}", "warning")
            return saved["lat"], saved["lon"], str(saved.get("name") or "")
        raise FetchError(
            f"could not find the server's location ({exc}); set latitude and "
            "longitude in the module's options"
        ) from exc
    # Two decimals (about 1 km) is as fine as an IP lookup gets.
    lat, lon = round(lat, 2), round(lon, 2)
    parts = [found.get("city"), found.get("region")]
    name = ", ".join(str(p).strip() for p in parts if isinstance(p, str) and p.strip())
    ctx.store.set(
        "location",
        {"lat": lat, "lon": lon, "name": name[:80], "at": ctx.now.isoformat()},
    )
    return lat, lon, name[:80]


# -- forecast ----------------------------------------------------------
def _clock(moment: datetime, twelve_hour: bool, minutes: bool = True) -> str:
    if not twelve_hour:
        return f"{moment:%H:%M}"
    half = "AM" if moment.hour < 12 else "PM"
    if not minutes:
        return f"{moment.hour % 12 or 12} {half}"
    return f"{moment.hour % 12 or 12}:{moment:%M} {half}"


def _condition(code: object) -> str:
    number = _number(code)
    return CONDITIONS.get(int(number), "") if number is not None else ""


def _degrees(value: object) -> str:
    number = _number(value)
    # "+ 0" turns a rounded -0 into 0.
    return f"{round(number) + 0}°" if number is not None else "--"


def _first(daily: dict, key: str) -> object:
    values = daily.get(key)
    return values[0] if isinstance(values, list) and values else None


def _local_time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _outlook(data: dict, twelve_hour: bool) -> list[Element]:
    """The rest of the day, one row every few hours."""
    hourly = data.get("hourly") if isinstance(data.get("hourly"), dict) else {}
    current = data.get("current") if isinstance(data.get("current"), dict) else {}
    now = _local_time(current.get("time"))
    times = hourly.get("time") if isinstance(hourly.get("time"), list) else []

    def column(key: str) -> list:
        values = hourly.get(key)
        ok = isinstance(values, list) and len(values) == len(times)
        return values if ok else [None] * len(times)

    rows = zip(
        times,
        column("temperature_2m"),
        column("weather_code"),
        column("precipitation_probability"),
        strict=True,
    )
    upcoming = [
        (moment, *rest)
        for stamp, *rest in rows
        if (moment := _local_time(stamp)) and (now is None or moment > now)
    ]
    blocks: list[Element] = []
    for moment, temperature, code, chance in upcoming[::HOURLY_STEP][:HOURLY_ROWS]:
        parts = [_degrees(temperature), _condition(code)]
        chance = _number(chance)
        if chance is not None and chance >= 20:
            parts.append(f"{round(chance)}%")
        blocks.append(
            KeyValue(
                _clock(moment, twelve_hour, minutes=False),
                " ".join(p for p in parts if p),
            )
        )
    return blocks


def _forecast(
    data: dict, place: str, units: str, twelve_hour: bool
) -> tuple[date | None, list[Element]]:
    daily = data.get("daily") if isinstance(data.get("daily"), dict) else {}
    current = data.get("current") if isinstance(data.get("current"), dict) else {}
    high, low = _first(daily, "temperature_2m_max"), _first(daily, "temperature_2m_min")
    if _number(high) is None or _number(low) is None:
        raise FetchError("the forecast has no temperatures for today")
    try:
        day = date.fromisoformat(str(_first(daily, "time")))
    except ValueError:
        day = None

    blocks: list[Element] = []
    if place:
        blocks.append(Text(place, size="small"))
    condition = _condition(_first(daily, "weather_code"))
    if condition:
        blocks.append(Text(condition, size="large", bold=True))
    blocks.append(KeyValue("High / Low", f"{_degrees(high)} / {_degrees(low)}"))
    if _number(current.get("temperature_2m")) is not None:
        now = _degrees(current.get("temperature_2m"))
        feels = _degrees(current.get("apparent_temperature"))
        if feels not in ("--", now):
            now = f"{now} (feels {feels})"
        blocks.append(KeyValue("Now", now))

    chance = _number(_first(daily, "precipitation_probability_max"))
    amount = _number(_first(daily, "precipitation_sum"))
    if chance is not None:
        value = f"{round(chance)}%"
        if amount:
            digits = 2 if units == "imperial" else 1
            value += f", {amount:.{digits}f}{UNITS[units]['precip']}"
        blocks.append(KeyValue("Precipitation", value))

    wind = _number(_first(daily, "wind_speed_10m_max"))
    if wind is not None:
        value = f"{round(wind)} {UNITS[units]['wind']}"
        direction = _number(_first(daily, "wind_direction_10m_dominant"))
        if direction is not None:
            value = f"{COMPASS[round(direction / 45) % 8]} {value}"
        gusts = _number(_first(daily, "wind_gusts_10m_max"))
        if gusts is not None and round(gusts) > round(wind):
            value += f", gusts {round(gusts)}"
        blocks.append(KeyValue("Wind", value))

    uv = _number(_first(daily, "uv_index_max"))
    if uv is not None:
        blocks.append(KeyValue("UV index", str(round(uv))))
    sunrise = _local_time(_first(daily, "sunrise"))
    sunset = _local_time(_first(daily, "sunset"))
    if sunrise and sunset:
        blocks.append(
            KeyValue(
                "Sun",
                f"{_clock(sunrise, twelve_hour)} - {_clock(sunset, twelve_hour)}",
            )
        )

    outlook = _outlook(data, twelve_hour)
    if outlook:
        blocks += [Rule(), *outlook]
    return day, blocks


# -- alerts ------------------------------------------------------------
def _active_alerts(ctx: ModuleContext, lat: float, lon: float) -> list[dict]:
    """Alerts in effect at the location, most serious first."""
    choice = str(ctx.options.get("alerts") or "")
    if choice == ALERTS_OFF:
        return []
    lowest = SEVERITIES.index(ALERT_CHOICES.get(choice, "Unknown"))
    try:
        response = ctx.http.get_json(alerts_url(lat, lon))
    except FetchError as exc:
        if getattr(exc, "status", None) in (400, 404):
            # The National Weather Service only covers the United States.
            ctx.emit("no alert service for this location", "debug")
            return []
        raise
    features = response.get("features") if isinstance(response, dict) else None
    alerts = []
    for feature in features or []:
        alert = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(alert, dict) or not isinstance(alert.get("id"), str):
            continue
        if alert.get("status") != "Actual" or alert.get("messageType") == "Cancel":
            continue
        severity = alert.get("severity")
        rank = SEVERITIES.index(severity) if severity in SEVERITIES else 0
        if rank >= lowest:
            alerts.append((rank, alert))
    alerts.sort(key=lambda pair: -pair[0])  # stable: keeps the newest first
    return [alert for _, alert in alerts]


def _event(alert: dict) -> str:
    return str(alert.get("event") or "").strip() or "Weather alert"


def _until(alert: dict, zone: tzinfo | None, twelve_hour: bool, today: date) -> str:
    for key in ("ends", "expires"):
        try:
            end = datetime.fromisoformat(str(alert.get(key))).astimezone(zone)
        except ValueError:
            continue
        label = f"Until {_clock(end, twelve_hour)}"
        if end.date() != today:
            label += f" {DAYS[end.weekday()][:3]}, {MONTHS[end.month - 1]} {end.day}"
        return label
    return ""


def _paragraphs(value: object) -> str:
    """NWS text is hard-wrapped; rejoin the lines and keep the paragraphs."""
    paragraphs = [
        " ".join(part.split()) for part in re.split(r"\n\s*\n", str(value or ""))
    ]
    text = "\n".join(p for p in paragraphs if p)
    if len(text) > ALERT_TEXT_LIMIT:
        text = text[:ALERT_TEXT_LIMIT].rstrip() + "..."
    return text


def _alert_summary(
    alerts: list[dict], zone: tzinfo | None, twelve_hour: bool, today: date
) -> list[Element]:
    blocks: list[Element] = []
    for alert in alerts:
        blocks.append(Text(_event(alert), bold=True))
        until = _until(alert, zone, twelve_hour, today)
        if until:
            blocks.append(Text(until, size="small"))
    return blocks


def _alert_details(
    alerts: list[dict], zone: tzinfo | None, twelve_hour: bool, today: date
) -> list[Element]:
    blocks: list[Element] = []
    for index, alert in enumerate(alerts):
        if index:
            blocks.append(Rule())
        blocks.append(Heading(_event(alert)))
        until = _until(alert, zone, twelve_hour, today)
        if until:
            blocks.append(Text(until, bold=True))
        for key in ("description", "instruction"):
            text = _paragraphs(alert.get(key))
            if text:
                blocks.append(Text(text))
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


class WeatherModule(Module):
    id = "weather"
    name = "Weather"
    description = (
        "Prints today's forecast each morning, and weather alerts as they are issued."
    )

    defaults = {
        "enabled": False,
        "interval_seconds": 300,
        "limits": {"max_chars": 3000},
        "options": {
            "morning_time": "08:00",
            "timezone": "",
            "latitude": "",
            "longitude": "",
            "units": "imperial",
            "clock": "12-hour",
            "alerts": "all",
        },
    }
    options_schema = {
        "morning_time": {
            "type": "str",
            "label": "Forecast print time (HH:MM, 24-hour)",
            "max_length": 5,
        },
        "timezone": {
            "type": "str",
            "label": "Time zone, e.g. America/Chicago (blank = server's)",
            "max_length": 60,
        },
        "latitude": {
            "type": "str",
            "label": "Latitude, e.g. 44.81 (blank = server's location)",
            "max_length": 12,
        },
        "longitude": {
            "type": "str",
            "label": "Longitude, e.g. -91.5 (blank = server's location)",
            "max_length": 12,
        },
        "units": {
            "type": "select",
            "label": "Units",
            "choices": list(UNITS),
        },
        "clock": {
            "type": "select",
            "label": "Times",
            "choices": ["12-hour", "24-hour"],
        },
        "alerts": {
            "type": "select",
            "label": "Weather alerts to print (US only)",
            "choices": [*ALERT_CHOICES, ALERTS_OFF],
        },
    }

    def fetch(self, ctx: ModuleContext) -> list[Element]:
        zone = _zone(ctx)
        local = ctx.now.astimezone(zone)
        today = local.date()
        morning = _morning(ctx)
        manual = ctx.trigger == "manual"
        after_morning = local.time() >= morning
        first_today = ctx.store.get("printed_day") != today.isoformat()
        want_forecast = manual or (after_morning and first_today)
        units = ctx.options.get("units")
        units = units if units in UNITS else "imperial"
        twelve_hour = ctx.options.get("clock") != "24-hour"

        lat, lon, place = _location(ctx, refresh=want_forecast)

        try:
            alerts = _active_alerts(ctx, lat, lon)
        except FetchError as exc:
            if not want_forecast:
                raise
            # Alerts being unavailable must not cost the morning forecast.
            ctx.emit(f"could not check weather alerts: {exc}", "warning")
            alerts = None

        printed = ctx.store.get("alerts_printed")
        printed = set(printed) if isinstance(printed, list) else set()
        if alerts is not None:
            active = {alert["id"] for alert in alerts}
            fresh = [alert for alert in alerts if alert["id"] not in printed]
            # Forget alerts that have ended, so the list stays short.
            ctx.store.set("alerts_printed", sorted(active))

        if not want_forecast:
            if not fresh:
                raise Skip(
                    "no new weather alerts"
                    if after_morning or not first_today
                    else f"no new weather alerts; forecast at {morning:%H:%M}"
                )
            ctx.emit(f"{len(fresh)} new weather alert(s)")
            return _alert_details(fresh, zone, twelve_hour, today)

        zone_name = str(ctx.options.get("timezone") or "").strip()
        data = ctx.http.get_json(forecast_url(lat, lon, units, zone_name))
        day, body = _forecast(
            data if isinstance(data, dict) else {}, place, units, twelve_hour
        )
        day = day or today
        blocks: list[Element] = [
            Heading(f"{DAYS[day.weekday()]}, {MONTHS[day.month - 1]} {day.day}"),
            *body,
        ]
        if alerts:
            blocks += [
                Rule(heavy=True),
                Text("Alerts in effect", size="small"),
                *_alert_summary(alerts, zone, twelve_hour, today),
            ]
        if after_morning:  # an early manual run is not the morning forecast
            ctx.store.set("printed_day", today.isoformat())
        ctx.emit("morning forecast" if first_today and after_morning else "forecast")
        return blocks
