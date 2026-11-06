"""Pure scheduling logic (no Discord / APScheduler / database here, so it is easy to test).

Everything the bot posts is driven by ``compute_next_run``: given a schedule definition and a
moment in time, it returns the next moment the schedule should fire (or ``None`` when it is over).
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

from utils import (
    WEEKDAY_NAMES, UserError, fmt_date, fmt_interval, fmt_time12,
    parse_date, parse_interval, parse_time, parse_weekdays,
)

FREQUENCIES = ("once", "hourly", "daily", "weekly", "monthly", "custom")
SPEC_KEYS = (
    "schedule_type", "time_hhmm", "weekdays", "month_day", "run_date",
    "interval_minutes", "start_date", "end_date",
)


def _d(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


def _at(day: date, hh: int, mm: int, tz) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=tz)


def compute_next_run(spec: Mapping[str, Any], after: datetime, tz) -> Optional[datetime]:
    """Return the first run strictly after ``after`` (timezone-aware), or None if there is none."""
    stype = spec["schedule_type"]
    hh, mm = (int(x) for x in (spec.get("time_hhmm") or "08:00").split(":"))
    after = after.astimezone(tz)
    start, end = _d(spec.get("start_date")), _d(spec.get("end_date"))

    # Nothing may run before the start date; shift the search window forward.
    lower = after
    if start:
        start_dt = _at(start, 0, 0, tz)
        if start_dt > lower:
            lower = start_dt - timedelta(seconds=1)

    candidate: Optional[datetime] = None

    if stype == "once":
        run_date = _d(spec.get("run_date"))
        if run_date:
            dt = _at(run_date, hh, mm, tz)
            candidate = dt if dt > after else None

    elif stype == "hourly":
        base = lower.replace(minute=mm, second=0, microsecond=0)
        while base <= lower:
            base += timedelta(hours=1)
        candidate = base

    elif stype == "daily":
        dt = _at(lower.date(), hh, mm, tz)
        if dt <= lower:
            dt = _at(lower.date() + timedelta(days=1), hh, mm, tz)
        candidate = dt

    elif stype == "weekly":
        days = set(spec.get("weekdays") or [])
        if days:
            for offset in range(0, 8):
                day = lower.date() + timedelta(days=offset)
                if day.weekday() in days:
                    dt = _at(day, hh, mm, tz)
                    if dt > lower:
                        candidate = dt
                        break

    elif stype == "monthly":
        month_day = spec.get("month_day")
        if month_day:
            year, month = lower.year, lower.month
            for _ in range(26):
                day_num = min(int(month_day), calendar.monthrange(year, month)[1])
                dt = _at(date(year, month, day_num), hh, mm, tz)
                if dt > lower:
                    candidate = dt
                    break
                month += 1
                if month > 12:
                    month, year = 1, year + 1

    elif stype == "custom":
        interval = spec.get("interval_minutes")
        if not interval or not start:
            raise ValueError("custom schedules need interval_minutes and start_date")
        anchor = _at(start, hh, mm, tz)
        step = timedelta(minutes=int(interval))
        if after < anchor:
            candidate = anchor
        else:
            candidate = anchor + step * (int((after - anchor) // step) + 1)

    else:
        raise ValueError(f"unknown schedule type: {stype}")

    if candidate is None:
        return None
    if end and candidate.date() > end:
        return None
    return candidate


# --------------------------------------------------------------------------- building / validating
def build_spec(
    *,
    tz,
    now: datetime,
    base: Optional[Mapping[str, Any]] = None,
    frequency: Optional[str] = None,
    time_text: Optional[str] = None,
    weekdays_text: Optional[str] = None,
    month_day: Optional[int] = None,
    run_date_text: Optional[str] = None,
    interval_text: Optional[str] = None,
    start_text: Optional[str] = None,
    end_text: Optional[str] = None,
) -> Tuple[Dict[str, Any], datetime]:
    """Combine an existing schedule (``base``, for edits) with the user's new input.

    Returns ``(spec, next_run)``. Raises ``UserError`` with a friendly message when invalid.
    """
    spec: Dict[str, Any] = {k: (base or {}).get(k) for k in SPEC_KEYS}
    today = now.astimezone(tz).date()

    if frequency:
        spec["schedule_type"] = frequency
    stype = spec.get("schedule_type")
    if stype not in FREQUENCIES:
        raise UserError("Please choose a frequency: once, hourly, daily, weekly, monthly or custom.")

    if time_text is not None:
        spec["time_hhmm"] = parse_time(time_text)
    spec["time_hhmm"] = spec.get("time_hhmm") or "08:00"

    if weekdays_text is not None:
        spec["weekdays"] = parse_weekdays(weekdays_text)
    if month_day is not None:
        spec["month_day"] = int(month_day)
    if run_date_text is not None:
        spec["run_date"] = parse_date(run_date_text, today).isoformat()
    if interval_text is not None:
        spec["interval_minutes"] = parse_interval(interval_text)
    if start_text is not None:
        spec["start_date"] = parse_date(start_text, today).isoformat()
    if end_text is not None:
        spec["end_date"] = parse_date(end_text, today).isoformat()

    # Drop settings that do not apply to the chosen frequency.
    if stype != "weekly":
        spec["weekdays"] = None
    if stype != "monthly":
        spec["month_day"] = None
    if stype != "once":
        spec["run_date"] = None
    if stype != "custom":
        spec["interval_minutes"] = None
    if stype == "once":
        spec["start_date"] = spec["end_date"] = None

    if stype == "weekly" and not spec["weekdays"]:
        raise UserError("Weekly schedules need `weekdays`, for example `mon,wed,fri`.")
    if stype == "monthly":
        if not spec["month_day"] or not (1 <= spec["month_day"] <= 31):
            raise UserError("Monthly schedules need `month_day` between 1 and 31.")
    if stype == "once" and not spec["run_date"]:
        raise UserError("One-time schedules need a `run_date`, for example `2026-11-20`.")
    if stype == "custom":
        if not spec["interval_minutes"]:
            raise UserError("Custom schedules need an `interval`, for example `30m`, `2h` or `1d`.")
        spec["start_date"] = spec["start_date"] or today.isoformat()

    start, end = _d(spec["start_date"]), _d(spec["end_date"])
    if start and end and end < start:
        raise UserError("The end date can't be before the start date.")

    next_run = compute_next_run(spec, now, tz)
    if next_run is None:
        if stype == "once":
            raise UserError("That date and time has already passed. Choose a time in the future.")
        raise UserError("That schedule has no upcoming run (it would be after the end date).")
    return spec, next_run


def describe_schedule(spec: Mapping[str, Any]) -> str:
    """A short human-readable description of the frequency."""
    stype = spec["schedule_type"]
    t12 = fmt_time12(spec.get("time_hhmm") or "08:00")
    if stype == "once":
        run_date = _d(spec.get("run_date"))
        text = f"Once on {fmt_date(run_date)} at {t12}" if run_date else "Once"
    elif stype == "hourly":
        minute = int((spec.get("time_hhmm") or "08:00").split(":")[1])
        text = f"Every hour at :{minute:02d}"
    elif stype == "daily":
        text = f"Every day at {t12}"
    elif stype == "weekly":
        names = ", ".join(WEEKDAY_NAMES[d] for d in sorted(spec.get("weekdays") or []))
        text = f"Every week on {names} at {t12}"
    elif stype == "monthly":
        day = spec.get("month_day")
        extra = " (last day of shorter months)" if day and day > 28 else ""
        text = f"Every month on day {day} at {t12}{extra}"
    else:
        text = f"Every {fmt_interval(int(spec.get('interval_minutes') or 0))} from {t12}"
    start, end = _d(spec.get("start_date")), _d(spec.get("end_date"))
    if stype != "once":
        if start and stype != "custom":
            text += f", from {fmt_date(start)}"
        if end:
            text += f", until {fmt_date(end)}"
    return text
