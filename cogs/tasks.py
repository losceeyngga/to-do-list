"""Task commands: /add-task /tasks /edit-task /delete-task /task-status"""
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import embeds
from scheduling_logic import build_spec
from utils import UserError, from_db, parse_date, to_db

from .common import (
    FREQUENCY_CHOICES, SCHEDULE_DOCS, STATUS_CHOICES, ConfirmView, check_channel, ensure_manage,
    get_task, pick_channel, task_choices,
)

log = logging.getLogger("taskbot.tasks")


class Tasks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------------ /add-task
    @app_commands.command(name="add-task", description="Create a task and optionally set up automatic announcements")
    @app_commands.guild_only()
    @app_commands.describe(
        subject="Subject or course, e.g. RPH",
        description="What needs to be done, e.g. Written Assignment",
        deadline="Due date, e.g. 2026-11-20 or November 20, 2026",
        auto_send="Automatically post this task on a schedule? (default: no)",
        frequency="How often to post (setting this also turns on auto-send; default: every day)",
        channel="Channel for announcements (default: the channel you are typing in)",
        time=SCHEDULE_DOCS["time"], weekdays=SCHEDULE_DOCS["weekdays"], month_day=SCHEDULE_DOCS["month_day"],
        run_date=SCHEDULE_DOCS["run_date"], interval=SCHEDULE_DOCS["interval"],
        start_date=SCHEDULE_DOCS["start_date"], end_date=SCHEDULE_DOCS["end_date"],
    )
    @app_commands.choices(frequency=FREQUENCY_CHOICES)
    async def add_task(
        self, interaction: discord.Interaction,
        subject: app_commands.Range[str, 1, 100],
        description: app_commands.Range[str, 1, 500],
        deadline: str,
        auto_send: bool = False,
        frequency: Optional[app_commands.Choice[str]] = None,
        channel: Optional[discord.TextChannel] = None,
        time: Optional[str] = None,
        weekdays: Optional[str] = None,
        month_day: Optional[app_commands.Range[int, 1, 31]] = None,
        run_date: Optional[str] = None,
        interval: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ):
        ensure_manage(interaction)
        svc = self.bot.service
        now = svc.now()
        target = pick_channel(interaction, channel)
        deadline_date = parse_date(deadline, now.astimezone(self.bot.tz).date())

        spec = next_run = None
        if auto_send or frequency is not None:
            # Validate the whole schedule BEFORE saving anything, so we never half-create a task.
            spec, next_run = build_spec(
                tz=self.bot.tz, now=now, frequency=frequency.value if frequency else "daily",
                time_text=time, weekdays_text=weekdays, month_day=month_day, run_date_text=run_date,
                interval_text=interval, start_text=start_date, end_text=end_date)

        db = self.bot.db
        task = db.create_task(interaction.guild_id, subject.strip(), description.strip(),
                              deadline_date.isoformat(), target.id, interaction.user.id)
        result = [embeds.task_embed(task, self.bot.tz, now, title="✅ TASK ADDED",
                                    closing=f"Task ID: **#{task['id']}**", footer_prefix="Created")]
        if spec:
            sched = db.create_schedule(
                guild_id=interaction.guild_id, mode="individual", task_id=task["id"], channel_id=target.id,
                next_run_at=to_db(next_run), created_by=interaction.user.id, **spec)
            svc.refresh(sched["id"])
            result.append(embeds.schedule_embed(sched, task, self.bot.tz, now, title="\U0001F4E2 AUTO-ANNOUNCEMENT SET",
                                                next_run=next_run))
        else:
            result[0].set_footer(text=result[0].footer.text + "  •  Tip: use /schedule to add automatic announcements")
        await interaction.response.send_message(embeds=result)

    # ------------------------------------------------------------------ /tasks
    @app_commands.command(name="tasks", description="Show all saved tasks")
    @app_commands.guild_only()
    @app_commands.describe(status="Only show tasks with this status (default: all)")
    @app_commands.choices(status=STATUS_CHOICES)
    async def tasks(self, interaction: discord.Interaction, status: Optional[app_commands.Choice[str]] = None):
        db = self.bot.db
        listing = db.list_tasks(interaction.guild_id, status.value if status else None)
        counts = db.schedule_counts_by_task(interaction.guild_id)
        pages = embeds.task_list_embeds(
            listing, self.bot.tz, self.bot.service.now(), counts,
            heading="\U0001F4CB TASKS" if not status else f"\U0001F4CB {status.name.upper()} TASKS")
        note = None
        if len(pages) > 10:  # Discord allows 10 embeds per message
            pages, note = pages[:10], f"Showing the first {10 * embeds.TASKS_PER_PAGE} tasks. Filter by status to see others."
        await interaction.response.send_message(content=note, embeds=pages, ephemeral=True)

    # ------------------------------------------------------------------ /edit-task
    @app_commands.command(name="edit-task", description="Change a task's subject, description, deadline or channel")
    @app_commands.guild_only()
    @app_commands.describe(
        task="The task to edit", subject="New subject", description="New description",
        deadline="New due date, e.g. 2026-11-25",
        channel="New announcement channel (also moves this task's scheduled announcements)")
    async def edit_task(
        self, interaction: discord.Interaction, task: int,
        subject: Optional[app_commands.Range[str, 1, 100]] = None,
        description: Optional[app_commands.Range[str, 1, 500]] = None,
        deadline: Optional[str] = None,
        channel: Optional[discord.TextChannel] = None,
    ):
        ensure_manage(interaction)
        row = get_task(interaction, task)
        changes = {}
        if subject is not None:
            changes["subject"] = subject.strip()
        if description is not None:
            changes["description"] = description.strip()
        if deadline is not None:
            today = self.bot.service.now().astimezone(self.bot.tz).date()
            changes["deadline"] = parse_date(deadline, today).isoformat()
        if channel is not None:
            check_channel(interaction, channel)
            changes["channel_id"] = channel.id
        if not changes:
            raise UserError("Nothing to change. Fill in at least one option (subject, description, deadline or channel).")

        db, svc = self.bot.db, self.bot.service
        db.update_task(task, **changes)
        if channel is not None:
            for s in db.list_schedules(interaction.guild_id, ("active", "paused")):
                if s["task_id"] == task:
                    db.update_schedule(s["id"], channel_id=channel.id)
                    svc.refresh(s["id"])
        updated = db.get_task(interaction.guild_id, task)
        await interaction.response.send_message(embed=embeds.task_embed(
            updated, self.bot.tz, svc.now(), title="✏️ TASK UPDATED",
            closing=f"Task ID: **#{task}**", footer_prefix="Updated"))

    @edit_task.autocomplete("task")
    async def _edit_task_ac(self, interaction: discord.Interaction, current: str):
        return await task_choices(interaction, current)

    # ------------------------------------------------------------------ /delete-task
    @app_commands.command(name="delete-task", description="Delete a task and all of its announcement schedules")
    @app_commands.guild_only()
    @app_commands.describe(task="The task to delete")
    async def delete_task(self, interaction: discord.Interaction, task: int):
        ensure_manage(interaction)
        row = get_task(interaction, task)
        view = ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"⚠️ Delete task **#{row['id']} {row['subject']} — {row['description']}** "
            "and stop all of its scheduled announcements? This can't be undone.",
            view=view, ephemeral=True)
        await view.wait()
        if view.value is not True:
            await interaction.edit_original_response(
                content="Cancelled. Nothing was deleted." if view.value is False else "Timed out. Nothing was deleted.",
                view=None)
            return
        for sid in self.bot.db.delete_task(task):
            self.bot.service.refresh(sid)
        await interaction.edit_original_response(
            content=f"\U0001F5D1️ Deleted task **#{row['id']} {row['subject']}** and its schedules.", view=None)

    @delete_task.autocomplete("task")
    async def _delete_task_ac(self, interaction: discord.Interaction, current: str):
        return await task_choices(interaction, current)

    # ------------------------------------------------------------------ /task-status
    @app_commands.command(name="task-status", description="Mark a task as Pending, Completed or Cancelled")
    @app_commands.guild_only()
    @app_commands.describe(task="The task to update", status="The new status")
    @app_commands.choices(status=STATUS_CHOICES)
    async def task_status(self, interaction: discord.Interaction, task: int, status: app_commands.Choice[str]):
        ensure_manage(interaction)
        get_task(interaction, task)
        self.bot.db.update_task(task, status=status.value)
        updated = self.bot.db.get_task(interaction.guild_id, task)
        embed = embeds.task_embed(
            updated, self.bot.tz, self.bot.service.now(), title="\U0001F4CC TASK STATUS UPDATED",
            closing=("Scheduled announcements continue." if status.value == "pending"
                     else "Scheduled announcements are skipped while this task isn't Pending."),
            footer_prefix="Updated")
        await interaction.response.send_message(embed=embed)

    @task_status.autocomplete("task")
    async def _task_status_ac(self, interaction: discord.Interaction, current: str):
        return await task_choices(interaction, current)


async def setup(bot):
    await bot.add_cog(Tasks(bot))
