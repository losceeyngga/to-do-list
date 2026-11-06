"""Schedule commands: /schedule /schedules /pause /resume /editschedule /deleteschedule"""
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import embeds
from scheduling_logic import build_spec
from utils import UserError, from_db, to_db

from .common import (
    FREQUENCY_CHOICES, SCHEDULE_DOCS, ConfirmView, check_channel, ensure_manage, get_schedule,
    get_task, pick_channel, schedule_choices, task_choices,
)

log = logging.getLogger("taskbot.schedules")


class Schedules(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _task_for(self, sched):
        if sched["mode"] == "summary" or not sched["task_id"]:
            return None
        return self.bot.db.get_task(sched["guild_id"], sched["task_id"])

    # ------------------------------------------------------------------ /schedule
    @app_commands.command(name="schedule", description="Create an automatic announcement schedule for a task")
    @app_commands.guild_only()
    @app_commands.describe(
        task="The task to announce", frequency="How often to post",
        channel="Channel for announcements (default: the task's channel)",
        time=SCHEDULE_DOCS["time"], weekdays=SCHEDULE_DOCS["weekdays"], month_day=SCHEDULE_DOCS["month_day"],
        run_date=SCHEDULE_DOCS["run_date"], interval=SCHEDULE_DOCS["interval"],
        start_date=SCHEDULE_DOCS["start_date"], end_date=SCHEDULE_DOCS["end_date"],
    )
    @app_commands.choices(frequency=FREQUENCY_CHOICES)
    async def schedule(
        self, interaction: discord.Interaction, task: int, frequency: app_commands.Choice[str],
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
        row = get_task(interaction, task)
        svc = self.bot.service
        now = svc.now()

        if channel is None and row.get("channel_id"):
            target = interaction.guild.get_channel(row["channel_id"])
            if target is None:
                raise UserError("This task's saved channel no longer exists. Pick a new one with the `channel` option.")
            check_channel(interaction, target)
        else:
            target = pick_channel(interaction, channel)

        spec, next_run = build_spec(
            tz=self.bot.tz, now=now, frequency=frequency.value, time_text=time, weekdays_text=weekdays,
            month_day=month_day, run_date_text=run_date, interval_text=interval,
            start_text=start_date, end_text=end_date)
        sched = self.bot.db.create_schedule(
            guild_id=interaction.guild_id, mode="individual", task_id=task, channel_id=target.id,
            next_run_at=to_db(next_run), created_by=interaction.user.id, **spec)
        svc.refresh(sched["id"])
        await interaction.response.send_message(embed=embeds.schedule_embed(
            sched, row, self.bot.tz, now, title="✅ SCHEDULE CREATED", next_run=next_run))

    @schedule.autocomplete("task")
    async def _schedule_task_ac(self, interaction: discord.Interaction, current: str):
        return await task_choices(interaction, current)

    # ------------------------------------------------------------------ /schedules
    @app_commands.command(name="schedules", description="Show all active and paused announcement schedules")
    @app_commands.guild_only()
    async def schedules(self, interaction: discord.Interaction):
        db = self.bot.db
        listing = db.list_schedules(interaction.guild_id, ("active", "paused"))
        tasks = {t["id"]: t for t in db.list_tasks(interaction.guild_id)}
        pages = embeds.schedule_list_embeds(listing, tasks, self.bot.tz, self.bot.service.now())
        await interaction.response.send_message(embeds=pages[:10], ephemeral=True)

    # ------------------------------------------------------------------ /pause
    @app_commands.command(name="pause", description="Temporarily pause a schedule")
    @app_commands.guild_only()
    @app_commands.describe(schedule="The schedule to pause")
    async def pause(self, interaction: discord.Interaction, schedule: int):
        ensure_manage(interaction)
        row = get_schedule(interaction, schedule)
        if row["status"] == "paused":
            raise UserError(f"Schedule **S#{schedule}** is already paused.")
        if row["status"] != "active":
            raise UserError(f"Schedule **S#{schedule}** is {row['status']}, so there is nothing to pause.")
        self.bot.db.update_schedule(schedule, status="paused", next_run_at=None)
        self.bot.service.refresh(schedule)
        updated = self.bot.db.get_schedule(schedule)
        await interaction.response.send_message(embed=embeds.schedule_embed(
            updated, self._task_for(updated), self.bot.tz, self.bot.service.now(), title="⏸️ SCHEDULE PAUSED"))

    @pause.autocomplete("schedule")
    async def _pause_ac(self, interaction: discord.Interaction, current: str):
        return await schedule_choices(interaction, current, ("active",))

    # ------------------------------------------------------------------ /resume
    @app_commands.command(name="resume", description="Resume a paused schedule")
    @app_commands.guild_only()
    @app_commands.describe(schedule="The schedule to resume")
    async def resume(self, interaction: discord.Interaction, schedule: int):
        ensure_manage(interaction)
        row = get_schedule(interaction, schedule)
        if row["status"] == "active":
            raise UserError(f"Schedule **S#{schedule}** is already running.")
        # Resuming continues from the next normal time; missed runs while paused are not replayed.
        nxt = self.bot.service.activate(schedule)
        if nxt is None:
            raise UserError("This schedule has no upcoming runs (its date has passed or it is past the end date). "
                            "Use `/editschedule` to give it new dates.")
        updated = self.bot.db.get_schedule(schedule)
        await interaction.response.send_message(embed=embeds.schedule_embed(
            updated, self._task_for(updated), self.bot.tz, self.bot.service.now(),
            title="▶️ SCHEDULE RESUMED", next_run=nxt))

    @resume.autocomplete("schedule")
    async def _resume_ac(self, interaction: discord.Interaction, current: str):
        return await schedule_choices(interaction, current, ("paused",))

    # ------------------------------------------------------------------ /editschedule
    @app_commands.command(name="editschedule", description="Change an existing schedule (only fill in what you want to change)")
    @app_commands.guild_only()
    @app_commands.describe(
        schedule="The schedule to edit", frequency="New frequency", channel="New announcement channel",
        time=SCHEDULE_DOCS["time"], weekdays=SCHEDULE_DOCS["weekdays"], month_day=SCHEDULE_DOCS["month_day"],
        run_date=SCHEDULE_DOCS["run_date"], interval=SCHEDULE_DOCS["interval"],
        start_date=SCHEDULE_DOCS["start_date"], end_date=SCHEDULE_DOCS["end_date"],
    )
    @app_commands.choices(frequency=FREQUENCY_CHOICES)
    async def editschedule(
        self, interaction: discord.Interaction, schedule: int,
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
        row = get_schedule(interaction, schedule)
        if all(v is None for v in (frequency, channel, time, weekdays, month_day, run_date, interval,
                                   start_date, end_date)):
            raise UserError("Nothing to change. Fill in at least one option.")
        svc = self.bot.service
        now = svc.now()
        if channel is not None:
            check_channel(interaction, channel)

        spec, next_run = build_spec(
            tz=self.bot.tz, now=now, base=row, frequency=frequency.value if frequency else None,
            time_text=time, weekdays_text=weekdays, month_day=month_day, run_date_text=run_date,
            interval_text=interval, start_text=start_date, end_text=end_date)

        fields = dict(spec)
        if channel is not None:
            fields["channel_id"] = channel.id
        if row["status"] == "paused":
            fields["next_run_at"] = None            # stays paused until /resume
        else:
            fields.update(status="active", next_run_at=to_db(next_run), last_error=None)
        self.bot.db.update_schedule(schedule, **fields)
        svc.refresh(schedule)
        updated = self.bot.db.get_schedule(schedule)
        await interaction.response.send_message(embed=embeds.schedule_embed(
            updated, self._task_for(updated), self.bot.tz, now, title="✏️ SCHEDULE UPDATED",
            next_run=next_run if updated["status"] == "active" else None))

    @editschedule.autocomplete("schedule")
    async def _edit_ac(self, interaction: discord.Interaction, current: str):
        return await schedule_choices(interaction, current)

    # ------------------------------------------------------------------ /deleteschedule
    @app_commands.command(name="deleteschedule", description="Permanently delete a schedule")
    @app_commands.guild_only()
    @app_commands.describe(schedule="The schedule to delete")
    async def deleteschedule(self, interaction: discord.Interaction, schedule: int):
        ensure_manage(interaction)
        row = get_schedule(interaction, schedule)
        task = self._task_for(row)
        label = "the Daily Task Summary" if row["mode"] == "summary" else (
            f"the announcements for **{task['subject']}**" if task else "this schedule")
        view = ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"⚠️ Delete schedule **S#{schedule}** ({label})? It will never post again.",
            view=view, ephemeral=True)
        await view.wait()
        if view.value is not True:
            await interaction.edit_original_response(
                content="Cancelled. The schedule is unchanged." if view.value is False else "Timed out. The schedule is unchanged.",
                view=None)
            return
        self.bot.db.update_schedule(schedule, status="deleted", next_run_at=None)
        self.bot.service.refresh(schedule)
        await interaction.edit_original_response(content=f"\U0001F5D1️ Deleted schedule **S#{schedule}**.", view=None)

    @deleteschedule.autocomplete("schedule")
    async def _delete_ac(self, interaction: discord.Interaction, current: str):
        return await schedule_choices(interaction, current)


async def setup(bot):
    await bot.add_cog(Schedules(bot))
