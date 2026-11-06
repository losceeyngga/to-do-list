"""The announcement engine: APScheduler timers backed by the SQLite database.

How it stays reliable
---------------------
* The database is the source of truth. Every active schedule stores ``next_run_at``.
* On startup ``restore()`` re-creates one APScheduler timer per active schedule.
  Paused / deleted / finished schedules get no timer.
* When a timer fires, ``_fire`` first *claims* the run in the database (atomic compare-and-swap
  on ``next_run_at`` + a UNIQUE row in ``send_log``) and only then posts. A second timer, the
  sweeper, or even a second bot process cannot claim the same run, so duplicates are impossible.
* A sweeper runs every minute as a safety net: it fires anything overdue and re-creates any
  timer that went missing (for example after a reconnect).
* Runs missed during downtime are posted ONCE if they are recent (grace period), otherwise
  skipped; the schedule then continues from its next normal time. Never a flood of old posts.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

import embeds
from database import Database, DatabaseError
from scheduling_logic import compute_next_run
from utils import from_db, to_db, utcnow

log = logging.getLogger("taskbot.scheduler")

SWEEPER_ID = "__sweeper__"


class AnnouncementService:
    def __init__(self, bot: Any, db: Database, tz, tz_name: str, grace_minutes: int,
                 now: Callable[[], datetime] = utcnow):
        self.bot = bot
        self.db = db
        self.tz = tz
        self.grace = timedelta(minutes=grace_minutes)
        self.now = now
        self._locks: Dict[int, asyncio.Lock] = {}
        self.sched = AsyncIOScheduler(
            timezone=tz_name,
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 86400},
        )

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Restore every active schedule from the database and start the timers."""
        self.restore()
        self.sched.add_job(self._sweep, IntervalTrigger(seconds=60), id=SWEEPER_ID, replace_existing=True)
        self.sched.start()
        try:
            purged = self.db.purge_old_logs(to_db(self.now() - timedelta(days=90)))
            if purged:
                log.info("Purged %d old send-log rows", purged)
        except DatabaseError:
            pass
        log.info("Scheduler started (timezone: %s)", self.tz)

    def shutdown(self) -> None:
        try:
            self.sched.shutdown(wait=False)
        except Exception:  # scheduler may not be running
            pass

    def restore(self) -> int:
        """Re-create timers for all active schedules. Returns how many were restored."""
        restored = 0
        for row in self.db.list_active_schedules():
            if not row.get("next_run_at"):
                nxt = self._safe_next(row, self.now())
                if nxt is None:
                    self.db.update_schedule(row["id"], status="finished", next_run_at=None)
                    log.info("Schedule S#%s has no future runs; marked finished", row["id"])
                    continue
                self.db.update_schedule(row["id"], next_run_at=to_db(nxt))
                row = self.db.get_schedule(row["id"]) or row
            self._add_job(row)
            restored += 1
        log.info("Restored %d active schedule(s) from the database", restored)
        return restored

    # ------------------------------------------------------------------ timers
    @staticmethod
    def _job_id(schedule_id: int) -> str:
        return f"schedule-{schedule_id}"

    def _add_job(self, row: Dict[str, Any]) -> None:
        next_dt = from_db(row["next_run_at"])
        now = self.now()
        run_at = next_dt if next_dt > now else now + timedelta(seconds=2)  # overdue: run right away
        self.sched.add_job(
            self._fire,
            trigger=DateTrigger(run_date=run_at.astimezone(timezone.utc)),
            args=[row["id"], row["next_run_at"]],
            id=self._job_id(row["id"]),
            replace_existing=True,
        )

    def _remove_job(self, schedule_id: int) -> None:
        try:
            if self.sched.get_job(self._job_id(schedule_id)) is not None:
                self.sched.remove_job(self._job_id(schedule_id))
        except Exception:  # job already gone
            pass

    def refresh(self, schedule_id: int) -> None:
        """Make the timer match the database row (call after any create/edit/pause/resume/delete)."""
        row = self.db.get_schedule(schedule_id)
        if row and row["status"] == "active" and row.get("next_run_at"):
            self._add_job(row)
        else:
            self._remove_job(schedule_id)

    def activate(self, schedule_id: int) -> Optional[datetime]:
        """Recompute the next run from *now*, save it and start the timer. Returns the next run."""
        row = self.db.get_schedule(schedule_id)
        if row is None:
            return None
        nxt = self._safe_next(row, self.now())
        if nxt is None:
            self.db.update_schedule(schedule_id, status="finished", next_run_at=None)
        else:
            self.db.update_schedule(schedule_id, status="active", next_run_at=to_db(nxt), last_error=None)
        self.refresh(schedule_id)
        return nxt

    def _safe_next(self, row: Dict[str, Any], after: datetime) -> Optional[datetime]:
        try:
            return compute_next_run(row, after, self.tz)
        except Exception:
            log.exception("Could not compute next run for schedule S#%s", row.get("id"))
            return None

    # ------------------------------------------------------------------ safety net
    async def _sweep(self) -> None:
        """Every minute: fire overdue schedules and re-create missing timers."""
        try:
            if not self.bot.is_ready():
                return
            now = self.now()
            for row in self.db.due_schedules(to_db(now)):
                await self._fire(row["id"], row["next_run_at"])
            for row in self.db.list_active_schedules():
                if row.get("next_run_at") and self.sched.get_job(self._job_id(row["id"])) is None:
                    log.warning("Timer for S#%s was missing; re-created it", row["id"])
                    self._add_job(row)
        except Exception:
            log.exception("Sweeper failed (will retry next minute)")

    # ------------------------------------------------------------------ firing
    def _grace_for(self, row: Dict[str, Any]) -> timedelta:
        grace = self.grace
        if row["schedule_type"] == "hourly":
            grace = min(grace, timedelta(minutes=30))
        elif row["schedule_type"] == "custom" and row.get("interval_minutes"):
            grace = min(grace, timedelta(minutes=int(row["interval_minutes"]) / 2))
        return grace

    async def _fire(self, schedule_id: int, expected_next: str) -> None:
        lock = self._locks.setdefault(schedule_id, asyncio.Lock())
        async with lock:
            try:
                await self._fire_locked(schedule_id, expected_next)
            except Exception:
                # One failed announcement must never take the bot (or other schedules) down.
                log.exception("Unexpected error while running schedule S#%s", schedule_id)

    async def _fire_locked(self, schedule_id: int, expected_next: str) -> None:
        row = self.db.get_schedule(schedule_id)
        if row is None or row["status"] != "active":
            log.info("S#%s ignored: not active (paused/deleted/finished)", schedule_id)
            return
        if row["next_run_at"] != expected_next:
            log.info("S#%s ignored: stale timer (already handled or rescheduled)", schedule_id)
            return

        if not self.bot.is_ready():
            await self.bot.wait_until_ready()

        now = self.now()
        scheduled_for = from_db(expected_next)
        if scheduled_for > now + timedelta(seconds=1):
            self._add_job(row)  # fired early; put the timer back
            return

        late = now - scheduled_for
        nxt = self._safe_next(row, max(now, scheduled_for))
        new_status = "active" if nxt else "finished"
        claimed = self.db.claim_and_advance(schedule_id, expected_next, to_db(nxt) if nxt else None, new_status)
        if not claimed:
            log.warning("S#%s run %s was already claimed; duplicate prevented", schedule_id, expected_next)
            return

        # The next run is stored; arm its timer (or clear it if the schedule is finished).
        self.refresh(schedule_id)

        if late > self._grace_for(row):
            log.warning("S#%s missed its %s run by %s (more than the grace period); skipped",
                        schedule_id, expected_next, late)
            self.db.record_outcome(schedule_id, expected_next, "skipped_missed", f"late by {late}")
            return

        if late > timedelta(minutes=2):
            log.info("S#%s catching up on a run that was %s late", schedule_id, late)
        outcome, detail = await self._deliver(row)
        self.db.record_outcome(schedule_id, expected_next, outcome, detail)

    # ------------------------------------------------------------------ delivering
    async def _resolve_channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)  # raises NotFound / Forbidden
        return channel

    async def _deliver(self, row: Dict[str, Any]) -> tuple[str, Optional[str]]:
        sid = row["id"]
        try:
            channel = await self._resolve_channel(row["channel_id"])
        except discord.NotFound:
            msg = "The announcement channel was deleted. Schedule paused; use /editschedule to pick a new channel, then /resume."
            self.db.update_schedule(sid, status="paused", next_run_at=None, last_error=msg)
            self.refresh(sid)
            log.error("S#%s: channel %s no longer exists; schedule paused", sid, row["channel_id"])
            return "channel_missing", msg
        except (discord.Forbidden, discord.HTTPException) as exc:
            msg = f"I can't access the announcement channel ({exc.__class__.__name__}). Check my permissions."
            self.db.set_error(sid, msg)
            log.error("S#%s: %s", sid, msg)
            return "no_access", msg

        guild = getattr(channel, "guild", None)
        if guild is not None and getattr(guild, "me", None) is not None:
            perms = channel.permissions_for(guild.me)
            missing = [name for name, ok in (
                ("View Channel", perms.view_channel), ("Send Messages", perms.send_messages),
                ("Embed Links", perms.embed_links)) if not ok]
            if missing:
                msg = f"I'm missing permission(s) in <#{row['channel_id']}>: {', '.join(missing)}."
                self.db.set_error(sid, msg)
                log.error("S#%s: %s", sid, msg)
                return "no_permission", msg

        now = self.now()
        if row["mode"] == "summary":
            embed = embeds.summary_embed(
                self.db.list_tasks(row["guild_id"], "pending"), self.tz, now,
                title="\U0001F4DA DAILY TASK SUMMARY" if row["schedule_type"] in ("daily", "hourly", "custom")
                else "\U0001F4DA TASK SUMMARY")
        else:
            task = self.db.get_task(row["guild_id"], row["task_id"]) if row["task_id"] else None
            if task is None:
                self.db.update_schedule(sid, status="deleted", next_run_at=None)
                self.refresh(sid)
                log.warning("S#%s: its task no longer exists; schedule deleted", sid)
                return "task_missing", "task deleted"
            if task["status"] != "pending":
                log.info("S#%s: task #%s is %s; announcement skipped", sid, task["id"], task["status"])
                return "skipped_not_pending", task["status"]
            embed = embeds.task_embed(task, self.tz, now)

        try:
            await self._send_with_retry(channel, embed)
        except discord.Forbidden:
            msg = f"Discord refused to let me post in <#{row['channel_id']}> (missing permissions)."
            self.db.set_error(sid, msg)
            log.error("S#%s: %s", sid, msg)
            return "no_permission", msg
        except discord.NotFound:
            msg = "The announcement channel was deleted. Schedule paused; use /editschedule to pick a new channel, then /resume."
            self.db.update_schedule(sid, status="paused", next_run_at=None, last_error=msg)
            self.refresh(sid)
            return "channel_missing", msg
        except Exception as exc:
            msg = f"Could not post the announcement: {exc}"
            self.db.set_error(sid, msg)
            log.exception("S#%s: send failed", sid)
            return "send_failed", msg

        self.db.mark_sent(sid, to_db(now))
        log.info("S#%s announcement posted to channel %s", sid, row["channel_id"])
        return "sent", None

    @staticmethod
    async def _send_with_retry(channel, embed) -> None:
        for attempt in range(1, 4):
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                return
            except (discord.Forbidden, discord.NotFound):
                raise
            except discord.HTTPException as exc:
                transient = getattr(exc, "status", 500) >= 500
                if not transient or attempt == 3:
                    raise
                await asyncio.sleep(2 ** attempt)
            except (OSError, asyncio.TimeoutError):  # connection dropped
                if attempt == 3:
                    raise
                await asyncio.sleep(2 ** attempt)
