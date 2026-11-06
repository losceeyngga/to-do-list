"""Small helpers: friendly errors, parsing of user input, and formatting."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import List

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_WEEKDAY_LOOKUP = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


class UserError(Exception):
    """An error whose message is safe and helpful to show directly to the Discord user."""


# --------------------------------------------------------------------------- time storage
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_db(dt: datetime) -> str:
    """Store datetimes as UTC ISO strings so they sort/compare correctly in SQLite."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_db(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- parsing
_TIME_12 = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m?\.?$", re.I)
_TIME_24 = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_time(text: str) -> str:
    """'8:00 AM', '8am', '08:00', '20:30', 'noon' -> 'HH:MM' (24h)."""
    raw = (text or "").strip().lower()
    if raw == "noon":
        return "12:00"
    if raw == "midnight":
        return "00:00"
    m = _TIME_12.match(raw)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if not (1 <= hour <= 12) or minute > 59:
            raise UserError(f"`{text}` is not a valid time. Try something like `8:00 AM` or `20:30`.")
        if m.group(3).lower() == "p" and hour != 12:
            hour += 12
        elif m.group(3).lower() == "a" and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute:02d}"
    m = _TIME_24.match(raw)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            raise UserError(f"`{text}` is not a valid time. Try something like `8:00 AM` or `20:30`.")
        return f"{hour:02d}:{minute:02d}"
    raise UserError(f"I couldn't understand the time `{text}`. Try `8:00 AM`, `8pm` or `20:30`.")


_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
    "%d %B %Y", "%d %b %Y", "%m/%d/%Y", "%m-%d-%Y",
)


def parse_date(text: str, today: date) -> date:
    """Parse a date typed by the user. Month/day/year (US style) is used for numeric dates."""
    raw = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", (text or "").strip(), flags=re.I)
    raw = re.sub(r"\s+", " ", raw)
    low = raw.lower()
    if low == "today":
        return today
    if low == "tomorrow":
        return today + timedelta(days=1)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise UserError(
        f"I couldn't understand the date `{text}`. Try `2026-11-20` or `November 20, 2026` "
        "(the year is required)."
    )


def parse_weekdays(text: str) -> List[int]:
    """'mon,wed,fri', 'Mon-Fri', 'weekdays', 'weekends' -> sorted list of 0..6 (Monday=0)."""
    result = set()
    cleaned = (text or "").lower().replace("&", ",").replace(" and ", ",")
    for token in re.split(r"[,\s/]+", cleaned):
        token = token.strip()
        if not token:
            continue
        if token in ("weekday", "weekdays"):
            result.update(range(0, 5))
        elif token in ("weekend", "weekends"):
            result.update((5, 6))
        elif token in ("everyday", "daily", "all"):
            result.update(range(0, 7))
        elif "-" in token:
            a, _, b = token.partition("-")
            if a not in _WEEKDAY_LOOKUP or b not in _WEEKDAY_LOOKUP:
                raise UserError(f"I couldn't understand the weekday range `{token}`. Example: `mon-fri`.")
            start, end = _WEEKDAY_LOOKUP[a], _WEEKDAY_LOOKUP[b]
            day = start
            while True:
                result.add(day)
                if day == end:
                    break
                day = (day + 1) % 7
        elif token in _WEEKDAY_LOOKUP:
            result.add(_WEEKDAY_LOOKUP[token])
        else:
            raise UserError(
                f"I couldn't understand the weekday `{token}`. Use names like `mon,wed,fri`, "
                "`mon-fri`, `weekdays` or `weekends`."
            )
    if not result:
        raise UserError("Please give at least one weekday, e.g. `mon,wed,fri`.")
    return sorted(result)


_INTERVAL_PART = re.compile(r"(\d+)\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d)", re.I)


def parse_interval(text: str) -> int:
    """'30m', '2h', '1d', '1h30m' -> minutes (min 5 minutes, max 366 days)."""
    raw = (text or "").strip().lower()
    total, consumed = 0, 0
    for m in _INTERVAL_PART.finditer(raw):
        amount, unit = int(m.group(1)), m.group(2).lower()
        if unit.startswith("d"):
            total += amount * 1440
        elif unit.startswith("h"):
            total += amount * 60
        else:
            total += amount
        consumed += len(m.group(0))
    if total == 0 or consumed != len(re.sub(r"[\s,]+", "", raw)):
        raise UserError(f"I couldn't understand the interval `{text}`. Try `30m`, `2h`, `1d` or `1h30m`.")
    if total < 5:
        raise UserError("The shortest custom interval is 5 minutes.")
    if total > 366 * 1440:
        raise UserError("The longest custom interval is 366 days.")
    return total


# --------------------------------------------------------------------------- formatting
def fmt_time12(hhmm: str) -> str:
    hour, minute = (int(x) for x in hhmm.split(":"))
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:{minute:02d} {suffix}"


def fmt_date(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def fmt_date_short(d: date) -> str:
    return f"{d:%B} {d.day}"


def fmt_dt(dt: datetime, tz) -> str:
    local = dt.astimezone(tz)
    return f"{local:%b} {local.day}, {local.year} • {fmt_time12(local.strftime('%H:%M'))}"


def fmt_interval(minutes: int) -> str:
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if mins:
        parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
    return " ".join(parts)


def clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"
