"""End-to-end tests: real slash-command handlers + real SQLite + real scheduler service.

Only Discord and APScheduler are replaced by stand-ins (see _bootstrap.py).
Clock for these tests: Fri 2026-11-06, 07:00 Asia/Manila (= 2026-11-05 23:00 UTC).
"""
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import _bootstrap as bs

import discord  # the stand-in
from discord import app_commands

from cogs.schedules import Schedules
from cogs.summary import Summary
from cogs.tasks import Tasks
from database import Database
from scheduler import AnnouncementService
from utils import UserError

TZ = ZoneInfo("Asia/Manila")
Choice = app_commands.Choice
START = datetime(2026, 11, 5, 23, 0, tzinfo=timezone.utc)  # 07:00 Manila, Friday 2026-11-06


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class Env:
    """A fresh bot environment. ``restart()`` simulates stopping and starting the bot."""

    def __init__(self, db_path: Path, clock: bs.FakeClock):
        self.db_path, self.clock = db_path, clock
        self.guild = bs.FakeGuild()
        self.channel = bs.FakeChannel(555, self.guild)
        self.guild.channels[555] = self.channel
        self._boot()

    def _boot(self):
        self.db = Database(self.db_path)
        self.bot = bs.FakeBot(self.guild, TZ, self.db)
        self.service = AnnouncementService(self.bot, self.db, TZ, "Asia/Manila", 360, now=self.clock)
        self.bot.service = self.service
        self.tasks, self.schedules, self.summary = Tasks(self.bot), Schedules(self.bot), Summary(self.bot)

    def restart(self):
        self.service.shutdown()
        self.db.close()
        self._boot()

    def ia(self):
        return bs.FakeInteraction(self.bot, self.channel)

    async def add_task(self, subject="RPH", desc="Written Assignment", deadline="November 20, 2026", **kw):
        ia = self.ia()
        args = dict(auto_send=False, frequency=None, channel=None, time=None, weekdays=None, month_day=None,
                    run_date=None, interval=None, start_date=None, end_date=None)
        args.update(kw)
        await self.tasks.add_task.callback(self.tasks, ia, subject, desc, deadline, **args)
        return ia

    def sid_for_task(self, task_id):
        return [s for s in self.db.list_schedules(1, ("active", "paused", "finished", "deleted"))
                if s["task_id"] == task_id][0]["id"]


class SystemTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = bs.FakeClock(START)
        self.env = Env(Path(self.tmp.name) / "test.db", self.clock)

    def tearDown(self):
        self.env.db.close()
        self.tmp.cleanup()

    async def daily_task(self, **kw):
        await self.env.add_task(auto_send=True, frequency=Choice("Every day", "daily"), time="8:00 AM", **kw)
        return self.env.db.list_schedules(1)[-1]

    # ------------------------------------------------------------------ creation
    async def test_add_task_saves_task_schedule_and_timer(self):
        ia = await self.env.add_task(auto_send=True, frequency=Choice("Every day", "daily"), time="8:00 AM")
        task = self.env.db.list_tasks(1)[0]
        sched = self.env.db.list_schedules(1)[0]
        self.assertEqual((task["subject"], task["description"], task["deadline"], task["status"]),
                         ("RPH", "Written Assignment", "2026-11-20", "pending"))
        self.assertEqual(sched["next_run_at"], "2026-11-06T00:00:00+00:00")  # 8:00 AM Manila
        self.assertIsNotNone(self.env.service.sched.get_job(f"schedule-{sched['id']}"))
        titles = [e.title for e in ia.embeds]
        self.assertEqual(len(titles), 2)
        confirm = ia.embeds[1]
        names = [f.name for f in confirm.fields]
        for expected in ("Task", "Announcement channel", "Frequency", "Scheduled time", "Timezone",
                         "Next announcement", "Status"):
            self.assertTrue(any(expected in n for n in names), expected)

    async def test_invalid_schedule_does_not_leave_half_created_task(self):
        with self.assertRaises(UserError):
            await self.env.add_task(auto_send=True, frequency=Choice("Every week", "weekly"))  # no weekdays
        with self.assertRaises(UserError):
            await self.env.add_task(deadline="not a date")
        with self.assertRaises(UserError):
            await self.env.add_task(auto_send=True, frequency=Choice("Once", "once"), run_date="2020-01-01")
        self.assertEqual(self.env.db.list_tasks(1), [])
        self.assertEqual(self.env.db.list_schedules(1), [])

    async def test_no_permission_in_channel_is_reported(self):
        self.env.channel.can_send = False
        with self.assertRaises(UserError) as ctx:
            await self.env.add_task(auto_send=True, frequency=Choice("Every day", "daily"))
        self.assertIn("Send Messages", str(ctx.exception))

    # ------------------------------------------------------------------ announcements + duplicates
    async def test_fires_once_and_advances_to_next_day(self):
        sched = await self.daily_task()
        self.clock.set(utc(2026, 11, 6, 0, 0, 5))
        await self.env.service._fire(sched["id"], sched["next_run_at"])
        self.assertEqual(len(self.env.channel.sent), 1)
        embed = self.env.channel.sent[0]
        self.assertEqual(embed.title, "\U0001F4DA TASK ANNOUNCEMENT")
        text = " ".join(f"{f.name} {f.value}" for f in embed.fields)
        for expected in ("RPH", "Written Assignment", "November 20, 2026", "PENDING"):
            self.assertIn(expected, text)
        self.assertIn("Published", embed.footer.text)
        after = self.env.db.get_schedule(sched["id"])
        self.assertEqual(after["next_run_at"], "2026-11-06T00:00:00+00:00".replace("11-06", "11-07"))
        self.assertIsNotNone(after["last_sent_at"])
        # The same timer firing a second time must NOT post again.
        await self.env.service._fire(sched["id"], sched["next_run_at"])
        self.assertEqual(len(self.env.channel.sent), 1)

    async def test_concurrent_timer_and_sweeper_post_only_one_copy(self):
        sched = await self.daily_task()
        self.clock.set(utc(2026, 11, 6, 0, 0, 5))
        svc = self.env.service
        await asyncio.gather(svc._fire(sched["id"], sched["next_run_at"]),
                             svc._fire(sched["id"], sched["next_run_at"]),
                             svc._sweep())
        self.assertEqual(len(self.env.channel.sent), 1)
        self.assertEqual(len(self.env.db.get_send_log(sched["id"])), 1)

    async def test_two_bot_processes_sharing_one_database_cannot_double_post(self):
        sched = await self.daily_task()
        other = Env.__new__(Env)  # a second "process" using the same database file
        other.db_path, other.clock, other.guild, other.channel = self.env.db_path, self.clock, self.env.guild, self.env.channel
        other._boot()
        self.clock.set(utc(2026, 11, 6, 0, 0, 5))
        await asyncio.gather(self.env.service._fire(sched["id"], sched["next_run_at"]),
                             other.service._fire(sched["id"], sched["next_run_at"]))
        self.assertEqual(len(self.env.channel.sent), 1)
        other.db.close()

    async def test_claim_is_unique_at_database_level(self):
        sched = await self.daily_task()
        db = self.env.db
        self.assertTrue(db.claim_and_advance(sched["id"], sched["next_run_at"], "2026-11-07T00:00:00+00:00", "active"))
        self.assertFalse(db.claim_and_advance(sched["id"], sched["next_run_at"], "2026-11-07T00:00:00+00:00", "active"))

    # ------------------------------------------------------------------ restart persistence
    async def test_schedules_survive_restart_and_paused_stay_paused(self):
        env = self.env
        await self.daily_task(subject="RPH")
        await self.daily_task(subject="Math", desc="Patterns")
        await self.daily_task(subject="Prog", desc="C activity")
        ids = [s["id"] for s in env.db.list_schedules(1)]
        ia = env.ia()
        await env.schedules.pause.callback(env.schedules, ia, ids[1])           # pause #2
        env.db.update_schedule(ids[2], status="deleted", next_run_at=None)      # delete #3
        env.service.refresh(ids[2])

        env.restart()  # ---- bot stopped and started again ----
        restored = env.service.restore()
        self.assertEqual(restored, 1)
        self.assertIsNotNone(env.service.sched.get_job(f"schedule-{ids[0]}"))
        self.assertIsNone(env.service.sched.get_job(f"schedule-{ids[1]}"))
        self.assertIsNone(env.service.sched.get_job(f"schedule-{ids[2]}"))
        self.assertEqual(env.db.get_schedule(ids[1])["status"], "paused")
        self.assertEqual(len(env.db.list_tasks(1)), 3)  # tasks survived too

        # A paused or deleted schedule must never post, even if an old timer fires.
        self.clock.set(utc(2026, 11, 6, 0, 1))
        await env.service._fire(ids[1], "2026-11-06T00:00:00+00:00")
        await env.service._fire(ids[2], "2026-11-06T00:00:00+00:00")
        self.assertEqual(env.channel.sent, [])
        await env.service._fire(ids[0], env.db.get_schedule(ids[0])["next_run_at"])
        self.assertEqual(len(env.channel.sent), 1)

    async def test_short_downtime_catches_up_once(self):
        sched = await self.daily_task()
        self.env.restart()
        self.clock.set(utc(2026, 11, 6, 0, 20))  # bot back 20 min after the 8:00 AM post was due
        self.env.service.restore()
        job = self.env.service.sched.get_job(f"schedule-{sched['id']}")
        self.assertLess(job.trigger.run_date - self.clock(), timedelta(seconds=5))  # queued to run now
        await self.env.service._fire(*job.args)
        await self.env.service._sweep()
        self.assertEqual(len(self.env.channel.sent), 1)

    async def test_long_downtime_skips_old_post_and_resumes_normally(self):
        sched = await self.daily_task()
        self.env.restart()
        self.clock.set(utc(2026, 11, 9, 3, 0))  # 3+ days later, 11:00 AM Manila
        self.env.service.restore()
        await self.env.service._fire(sched["id"], sched["next_run_at"])
        self.assertEqual(self.env.channel.sent, [])  # no flood of stale posts
        after = self.env.db.get_schedule(sched["id"])
        self.assertEqual(after["next_run_at"], "2026-11-10T00:00:00+00:00")  # next normal 8:00 AM
        self.assertEqual(self.env.db.get_send_log(sched["id"])[0]["outcome"], "skipped_missed")
        self.assertIsNotNone(self.env.service.sched.get_job(f"schedule-{sched['id']}"))

    # ------------------------------------------------------------------ pause / resume / edit / delete
    async def test_pause_resume_edit_delete_flow(self):
        env = self.env
        sched = await self.daily_task()
        sid = sched["id"]
        await env.schedules.pause.callback(env.schedules, env.ia(), sid)
        self.assertIsNone(env.service.sched.get_job(f"schedule-{sid}"))
        with self.assertRaises(UserError):
            await env.schedules.pause.callback(env.schedules, env.ia(), sid)  # already paused

        self.clock.set(utc(2026, 11, 6, 6, 0))  # 2:00 PM Manila, paused all morning
        ia = env.ia()
        await env.schedules.resume.callback(env.schedules, ia, sid)
        row = env.db.get_schedule(sid)
        self.assertEqual((row["status"], row["next_run_at"]), ("active", "2026-11-07T00:00:00+00:00"))  # not replayed
        self.assertEqual(ia.embeds[0].title, "▶️ SCHEDULE RESUMED")

        await env.schedules.editschedule.callback(
            env.schedules, env.ia(), sid, frequency=Choice("Every week", "weekly"), channel=None,
            time="9:30 PM", weekdays="mon,wed,fri", month_day=None, run_date=None, interval=None,
            start_date=None, end_date=None)
        row = env.db.get_schedule(sid)
        self.assertEqual((row["schedule_type"], row["time_hhmm"], row["weekdays"]), ("weekly", "21:30", [0, 2, 4]))
        self.assertEqual(row["next_run_at"], "2026-11-06T13:30:00+00:00")  # Fri 9:30 PM Manila

        # Delete: soft-deleted, timer gone, never executes.
        env.db.update_schedule(sid, status="deleted", next_run_at=None)
        env.service.refresh(sid)
        self.assertIsNone(env.service.sched.get_job(f"schedule-{sid}"))
        self.clock.set(utc(2026, 11, 6, 14, 0))
        await env.service._fire(sid, "2026-11-06T13:30:00+00:00")
        self.assertEqual(env.channel.sent, [])

    async def test_delete_task_removes_its_schedules(self):
        env = self.env
        sched = await self.daily_task()
        task_id = sched["task_id"]
        for sid in env.db.delete_task(task_id):
            env.service.refresh(sid)
        self.assertEqual(env.db.list_tasks(1), [])
        self.assertEqual(env.db.get_schedule(sched["id"])["status"], "deleted")
        self.assertIsNone(env.service.sched.get_job(f"schedule-{sched['id']}"))

    async def test_once_schedule_finishes_after_posting(self):
        env = self.env
        await env.add_task(auto_send=True, frequency=Choice("Once", "once"), time="8:00 AM", run_date="2026-11-06")
        sched = env.db.list_schedules(1)[0]
        self.clock.set(utc(2026, 11, 6, 0, 0, 3))
        await env.service._fire(sched["id"], sched["next_run_at"])
        self.assertEqual(len(env.channel.sent), 1)
        row = env.db.get_schedule(sched["id"])
        self.assertEqual((row["status"], row["next_run_at"]), ("finished", None))
        self.assertIsNone(env.service.sched.get_job(f"schedule-{sched['id']}"))

    # ------------------------------------------------------------------ error handling
    async def test_deleted_channel_pauses_schedule_with_message(self):
        sched = await self.daily_task()
        del self.env.guild.channels[555]  # channel was deleted in Discord
        self.clock.set(utc(2026, 11, 6, 0, 0, 5))
        await self.env.service._fire(sched["id"], sched["next_run_at"])
        row = self.env.db.get_schedule(sched["id"])
        self.assertEqual(row["status"], "paused")
        self.assertIn("deleted", row["last_error"])

    async def test_missing_permission_is_recorded_and_schedule_keeps_running(self):
        sched = await self.daily_task()
        self.env.channel.can_send = False
        self.clock.set(utc(2026, 11, 6, 0, 0, 5))
        await self.env.service._fire(sched["id"], sched["next_run_at"])
        row = self.env.db.get_schedule(sched["id"])
        self.assertEqual(row["status"], "active")
        self.assertIn("Send Messages", row["last_error"])
        self.assertEqual(self.env.channel.sent, [])

    async def test_send_failure_does_not_crash_and_other_schedules_still_run(self):
        env = self.env
        a = await self.daily_task(subject="A")
        b = await self.daily_task(subject="B")
        self.clock.set(utc(2026, 11, 6, 0, 0, 5))
        env.channel.fail_with = RuntimeError("boom")
        await env.service._fire(a["id"], a["next_run_at"])  # must not raise
        self.assertIn("boom", env.db.get_schedule(a["id"])["last_error"])
        self.assertEqual(env.db.get_schedule(a["id"])["next_run_at"], "2026-11-07T00:00:00+00:00")
        env.channel.fail_with = None
        await env.service._fire(b["id"], b["next_run_at"])
        self.assertEqual(len(env.channel.sent), 1)

    async def test_completed_task_is_not_announced(self):
        env = self.env
        sched = await self.daily_task()
        await env.tasks.task_status.callback(env.tasks, env.ia(), sched["task_id"], Choice("Completed", "completed"))
        self.clock.set(utc(2026, 11, 6, 0, 0, 5))
        await env.service._fire(sched["id"], sched["next_run_at"])
        self.assertEqual(env.channel.sent, [])
        self.assertEqual(env.db.get_send_log(sched["id"])[0]["outcome"], "skipped_not_pending")

    # ------------------------------------------------------------------ daily summary
    async def test_daily_summary_lists_only_pending_tasks(self):
        env = self.env
        await env.add_task(subject="RPH", desc="Written Assignment", deadline="2026-11-20")
        await env.add_task(subject="MATHEMATICS", desc="Mathematical Patterns", deadline="2026-11-22")
        await env.add_task(subject="PROGRAMMING", desc="C Programming Activity", deadline="2026-11-25")
        done = await env.add_task(subject="OLD", desc="Finished thing", deadline="2026-11-01")
        old_id = [t for t in env.db.list_tasks(1) if t["subject"] == "OLD"][0]["id"]
        await env.tasks.task_status.callback(env.tasks, env.ia(), old_id, Choice("Completed", "completed"))

        ia = env.ia()
        await env.summary.daily_summary.callback(
            env.summary, ia, enabled=True, channel=None, time="9:00 AM", frequency=None, weekdays=None,
            month_day=None, send_now=False)
        sched = env.db.get_summary_schedule(1)
        self.assertEqual((sched["mode"], sched["time_hhmm"], sched["status"]), ("summary", "09:00", "active"))

        self.clock.set(utc(2026, 11, 6, 1, 0, 5))
        await env.service._fire(sched["id"], sched["next_run_at"])
        self.assertEqual(len(env.channel.sent), 1)
        embed = env.channel.sent[0]
        self.assertEqual(embed.title, "\U0001F4DA DAILY TASK SUMMARY")
        self.assertEqual(len(embed.fields), 4)  # 3 pending tasks + total
        self.assertIn("TOTAL PENDING TASKS: 3", embed.fields[-1].value)
        self.assertNotIn("OLD", " ".join(f.name for f in embed.fields))

        # Disable then re-enable keeps a single summary schedule and survives restart.
        await env.summary.daily_summary.callback(env.summary, env.ia(), enabled=False, channel=None, time=None,
                                                 frequency=None, weekdays=None, month_day=None, send_now=False)
        env.restart()
        env.service.restore()
        self.assertEqual(env.db.get_summary_schedule(1)["status"], "paused")
        self.assertIsNone(env.service.sched.get_job(f"schedule-{sched['id']}"))

    async def test_individual_and_summary_modes_work_together(self):
        env = self.env
        await self.daily_task()
        await env.summary.daily_summary.callback(env.summary, env.ia(), enabled=True, channel=None, time="8:00 AM",
                                                 frequency=None, weekdays=None, month_day=None, send_now=False)
        self.clock.set(utc(2026, 11, 6, 0, 0, 5))
        for s in env.db.list_schedules(1):
            await env.service._fire(s["id"], s["next_run_at"])
        self.assertEqual(sorted(e.title for e in env.channel.sent),
                         sorted(["\U0001F4DA TASK ANNOUNCEMENT", "\U0001F4DA DAILY TASK SUMMARY"]))


if __name__ == "__main__":
    unittest.main()
