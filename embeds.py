"""All Discord embeds in one place so the bot looks consistent."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Optional

import discord

from scheduling_logic import describe_schedule
from utils import clip, fmt_date, fmt_date_short, fmt_dt, fmt_time12

SEP = "━" * 18

COLOR_PENDING = 0xF1C40F    # yellow
COLOR_COMPLETED = 0x2ECC71  # green
COLOR_CANCELLED = 0x95A5A6  # grey
COLOR_OVERDUE = 0xE74C3C    # red
COLOR_SUMMARY = 0x3498DB    # blue
COLOR_SCHEDULE = 0x5865F2   # blurple
COLOR_PAUSED = 0xE67E22     # orange
COLOR_ERROR = 0xED4245

MAX_SUMMARY_TASKS = 20
TASKS_PER_PAGE = 8

_STATUS = {
    "pending": ("\U0001F7E1", "PENDING", COLOR_PENDING),
    "completed": ("\U0001F7E2", "COMPLETED", COLOR_COMPLETED),
    "cancelled": ("⚪", "CANCELLED", COLOR_CANCELLED),
    "overdue": ("\U0001F534", "PENDING • OVERDUE", COLOR_OVERDUE),
}


def _deadline(task: Mapping[str, Any]) -> date:
    return date.fromisoformat(task["deadline"])


def effective_status(task: Mapping[str, Any], today: date) -> str:
    if task["status"] == "pending" and _deadline(task) < today:
        return "overdue"
    return task["status"]


def days_left_text(task: Mapping[str, Any], today: date) -> str:
    if task["status"] != "pending":
        return ""
    delta = (_deadline(task) - today).days
    if delta < 0:
        return f"{-delta} day{'s' if delta != -1 else ''} overdue"
    if delta == 0:
        return "due today"
    if delta == 1:
        return "due tomorrow"
    return f"{delta} days left"


def _today(now: datetime, tz) -> date:
    return now.astimezone(tz).date()


def _footer(embed: discord.Embed, now: datetime, tz, prefix: str = "Published") -> None:
    embed.timestamp = now
    embed.set_footer(text=f"\U0001F552 {prefix} {fmt_dt(now, tz)} ({getattr(tz, 'key', tz)})")


# --------------------------------------------------------------------------- task embeds
def task_embed(task: Mapping[str, Any], tz, now: datetime, *, title: str = "\U0001F4DA TASK ANNOUNCEMENT",
               closing: str = "\U0001F4E2 Don't forget to accomplish your task!",
               footer_prefix: str = "Published") -> discord.Embed:
    today = _today(now, tz)
    status_key = effective_status(task, today)
    emoji, label, color = _STATUS[status_key]
    left = days_left_text(task, today)

    embed = discord.Embed(title=title, description=SEP, color=color)
    embed.add_field(name="\U0001F4D6 SUBJECT", value=f"**{clip(task['subject'], 200)}**", inline=False)
    embed.add_field(name="\U0001F4DD TASK", value=clip(task["description"], 1000), inline=False)
    deadline_value = fmt_date(_deadline(task)) + (f"\n*{left}*" if left else "")
    embed.add_field(name="\U0001F4C5 DEADLINE", value=deadline_value, inline=False)
    embed.add_field(name=f"{emoji} STATUS", value=f"**{label}**", inline=False)
    embed.add_field(name="​", value=f"{SEP}\n{closing}", inline=False)
    _footer(embed, now, tz, footer_prefix)
    return embed


def summary_embed(tasks: List[Mapping[str, Any]], tz, now: datetime, *, title: str = "\U0001F4DA DAILY TASK SUMMARY") -> discord.Embed:
    today = _today(now, tz)
    pending = sorted((t for t in tasks if t["status"] == "pending"), key=lambda t: (t["deadline"], t["id"]))
    if not pending:
        embed = discord.Embed(
            title=title,
            description=f"{SEP}\n\n\U0001F389 **No pending tasks right now!**\n\n{SEP}\nEnjoy the free time!",
            color=COLOR_COMPLETED)
        _footer(embed, now, tz)
        return embed

    any_overdue = any(_deadline(t) < today for t in pending)
    embed = discord.Embed(title=title, description=SEP, color=COLOR_OVERDUE if any_overdue else COLOR_SUMMARY)
    for task in pending[:MAX_SUMMARY_TASKS]:
        left = days_left_text(task, today)
        flag = "\U0001F534 " if _deadline(task) < today else ""
        value = (f"\U0001F4DD {clip(task['description'], 120)}\n"
                 f"\U0001F4C5 Deadline: {fmt_date_short(_deadline(task))}" + (f" ({left})" if left else ""))
        embed.add_field(name=f"\U0001F4D6 {flag}{clip(task['subject'], 60).upper()}", value=value, inline=False)
    hidden = len(pending) - MAX_SUMMARY_TASKS
    more = f"\n*…and {hidden} more. Use `/tasks` to see everything.*" if hidden > 0 else ""
    embed.add_field(
        name="​",
        value=f"{SEP}\n\U0001F4CA **TOTAL PENDING TASKS: {len(pending)}**{more}\n\nStay productive and accomplish your tasks!",
        inline=False)
    _footer(embed, now, tz)
    return embed


def task_list_embeds(tasks: List[Mapping[str, Any]], tz, now: datetime,
                     schedule_counts: Optional[Dict[int, int]] = None,
                     heading: str = "\U0001F4CB TASKS") -> List[discord.Embed]:
    today = _today(now, tz)
    counts = schedule_counts or {}
    if not tasks:
        return [discord.Embed(
            title=heading, color=COLOR_SCHEDULE,
            description="No tasks yet. Create one with `/add-task`.")]
    pages = [tasks[i:i + TASKS_PER_PAGE] for i in range(0, len(tasks), TASKS_PER_PAGE)]
    result = []
    for number, page in enumerate(pages, start=1):
        embed = discord.Embed(title=f"{heading} ({len(tasks)})", description=SEP, color=COLOR_SCHEDULE)
        for task in page:
            key = effective_status(task, today)
            emoji, label, _ = _STATUS[key]
            left = days_left_text(task, today)
            n = counts.get(task["id"], 0)
            auto = f" • \U0001F4E2 {n} auto-announcement{'s' if n != 1 else ''}" if n else ""
            embed.add_field(
                name=f"#{task['id']} {emoji} {clip(task['subject'], 80)}",
                value=(f"\U0001F4DD {clip(task['description'], 150)}\n"
                       f"\U0001F4C5 {fmt_date(_deadline(task))}" + (f" ({left})" if left else "")
                       + f"\n**{label}**{auto}"),
                inline=False)
        if len(pages) > 1:
            embed.set_footer(text=f"Page {number}/{len(pages)}")
        result.append(embed)
    return result


# --------------------------------------------------------------------------- schedule embeds
def _discord_ts(dt: datetime) -> str:
    return f"<t:{int(dt.timestamp())}:R>"


def schedule_embed(schedule: Mapping[str, Any], task: Optional[Mapping[str, Any]], tz, now: datetime,
                   *, title: str = "✅ SCHEDULE SAVED", next_run: Optional[datetime] = None) -> discord.Embed:
    """Confirmation card shown whenever a schedule is created or changed."""
    from utils import from_db  # local import keeps module import light

    status = schedule["status"]
    color = COLOR_PAUSED if status in ("paused", "finished") else COLOR_SCHEDULE
    embed = discord.Embed(title=title, description=SEP, color=color)
    if schedule["mode"] == "summary":
        name = "\U0001F4CA Daily Task Summary (all pending tasks)"
    elif task:
        name = f"#{task['id']} {clip(task['subject'], 60)} — {clip(task['description'], 100)}"
    else:
        name = "(task removed)"
    embed.add_field(name="\U0001F4DD Task", value=name, inline=False)
    embed.add_field(name="\U0001F4E2 Announcement channel", value=f"<#{schedule['channel_id']}>", inline=True)
    embed.add_field(name="\U0001F501 Frequency", value=describe_schedule(schedule), inline=True)
    embed.add_field(name="⏰ Scheduled time", value=fmt_time12(schedule["time_hhmm"]), inline=True)
    embed.add_field(name="\U0001F30D Timezone", value=f"{getattr(tz, 'key', tz)}", inline=True)
    nxt = next_run
    if nxt is None and schedule.get("next_run_at"):
        nxt = from_db(schedule["next_run_at"])
    if nxt and status == "active":
        embed.add_field(name="⏭️ Next announcement",
                        value=f"{fmt_dt(nxt, tz)}\n{_discord_ts(nxt)}", inline=True)
    else:
        embed.add_field(name="⏭️ Next announcement", value="—", inline=True)
    status_icon = {"active": "\U0001F7E2 Active", "paused": "\U0001F7E0 Paused",
                   "finished": "⚫ Finished", "deleted": "\U0001F534 Deleted"}[status]
    embed.add_field(name="\U0001F4CC Status", value=status_icon, inline=True)
    if schedule.get("last_error"):
        embed.add_field(name="⚠️ Last problem", value=clip(schedule["last_error"], 300), inline=False)
    embed.add_field(name="​", value=f"Schedule ID: **S#{schedule['id']}**", inline=False)
    _footer(embed, now, tz, "Updated")
    return embed


def schedule_list_embeds(schedules: List[Mapping[str, Any]], tasks: Mapping[int, Mapping[str, Any]], tz,
                         now: datetime) -> List[discord.Embed]:
    from utils import from_db

    if not schedules:
        return [discord.Embed(
            title="\U0001F4C5 SCHEDULES", color=COLOR_SCHEDULE,
            description="No active schedules. Create one with `/schedule` or `/add-task`.")]
    lines = []
    for s in schedules:
        icon = "\U0001F7E2" if s["status"] == "active" else "\U0001F7E0"
        if s["mode"] == "summary":
            label = "\U0001F4CA Daily Task Summary"
        else:
            t = tasks.get(s["task_id"])
            label = f"\U0001F4D6 {clip(t['subject'], 40)} — {clip(t['description'], 50)}" if t else "\U0001F4D6 (task removed)"
        nxt = ""
        if s["status"] == "active" and s.get("next_run_at"):
            nxt_dt = from_db(s["next_run_at"])
            nxt = f"\n└ next: {fmt_dt(nxt_dt, tz)} ({_discord_ts(nxt_dt)})"
        err = f"\n└ ⚠️ {clip(s['last_error'], 120)}" if s.get("last_error") else ""
        lines.append(
            f"{icon} **S#{s['id']}** {label}\n└ {describe_schedule(s)} → <#{s['channel_id']}> "
            f"({s['status']}){nxt}{err}")
    chunks, current = [], ""
    for line in lines:
        if len(current) + len(line) + 2 > 3500:
            chunks.append(current)
            current = ""
        current += line + "\n\n"
    chunks.append(current)
    embeds = []
    for i, chunk in enumerate(chunks, start=1):
        e = discord.Embed(title=f"\U0001F4C5 SCHEDULES ({len(schedules)})", description=chunk.strip(),
                          color=COLOR_SCHEDULE)
        if len(chunks) > 1:
            e.set_footer(text=f"Page {i}/{len(chunks)}")
        embeds.append(e)
    return embeds


def error_embed(message: str, title: str = "❌ Something needs fixing") -> discord.Embed:
    return discord.Embed(title=title, description=message, color=COLOR_ERROR)


def info_embed(message: str, title: str = "ℹ️ Info", color: int = COLOR_SCHEDULE) -> discord.Embed:
    return discord.Embed(title=title, description=message, color=color)
