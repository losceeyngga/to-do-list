"""/daily-summary: configure the automatic summary of all pending tasks."""
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import embeds
from scheduling_logic import build_spec
from utils import to_db

from .common import SCHEDULE_DOCS, SUMMARY_FREQUENCY_CHOICES, check_channel, ensure_manage, pick_channel

log = logging.getLogger("taskbot.summary")


class Summary(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="daily-summary",
        description="Set up or change the automatic summary of all pending tasks (run with no options to view it)")
    @app_commands.guild_only()
    @app_commands.describe(
        enabled="Turn the summary on or off",
        channel="Channel to post the summary in (default: the channel you are typing in)",
        time=SCHEDULE_DOCS["time"],
        frequency="How often to post the summary (default: every day)",
        weekdays=SCHEDULE_DOCS["weekdays"],
        month_day=SCHEDULE_DOCS["month_day"],
        send_now="Also post the summary right now (handy for testing)",
    )
    @app_commands.choices(frequency=SUMMARY_FREQUENCY_CHOICES)
    async def daily_summary(
        self, interaction: discord.Interaction,
        enabled: Optional[bool] = None,
        channel: Optional[discord.TextChannel] = None,
        time: Optional[str] = None,
        frequency: Optional[app_commands.Choice[str]] = None,
        weekdays: Optional[str] = None,
        month_day: Optional[app_commands.Range[int, 1, 31]] = None,
        send_now: bool = False,
    ):
        db, svc, tz = self.bot.db, self.bot.service, self.bot.tz
        existing = db.get_summary_schedule(interaction.guild_id)
        now = svc.now()
        changing = any(v is not None for v in (enabled, channel, time, frequency, weekdays, month_day))

        if not changing and not send_now:  # just show the current setup
            if existing is None:
                await interaction.response.send_message(embed=embeds.info_embed(
                    "The daily summary isn't set up yet.\n\nTurn it on with, for example:\n"
                    "`/daily-summary enabled:True time:8:00 AM` (posts in the channel where you type it).",
                    title="\U0001F4CA DAILY SUMMARY"), ephemeral=True)
            else:
                await interaction.response.send_message(embed=embeds.schedule_embed(
                    existing, None, tz, now, title="\U0001F4CA DAILY SUMMARY SETTINGS"), ephemeral=True)
            return

        if changing:
            ensure_manage(interaction)
        elif send_now:
            ensure_manage(interaction)

        if send_now and not changing:
            target = pick_channel(interaction, channel) if existing is None else interaction.guild.get_channel(existing["channel_id"])
            if target is None:
                target = pick_channel(interaction, None)
            await target.send(embed=embeds.summary_embed(db.list_tasks(interaction.guild_id, "pending"), tz, now),
                              allowed_mentions=discord.AllowedMentions.none())
            await interaction.response.send_message(f"\U0001F4E4 Summary posted in {target.mention}.", ephemeral=True)
            return

        if existing is None:
            if enabled is False:
                await interaction.response.send_message(embed=embeds.info_embed(
                    "The daily summary isn't set up, so there is nothing to turn off."), ephemeral=True)
                return
            target = pick_channel(interaction, channel)
            spec, next_run = build_spec(
                tz=tz, now=now, frequency=frequency.value if frequency else "daily", time_text=time,
                weekdays_text=weekdays, month_day=month_day)
            sched = db.create_schedule(
                guild_id=interaction.guild_id, mode="summary", task_id=None, channel_id=target.id,
                next_run_at=to_db(next_run), created_by=interaction.user.id, **spec)
            svc.refresh(sched["id"])
            title = "✅ DAILY SUMMARY ENABLED"
        else:
            if channel is not None:
                check_channel(interaction, channel)
            spec, next_run = build_spec(
                tz=tz, now=now, base=existing, frequency=frequency.value if frequency else None,
                time_text=time, weekdays_text=weekdays, month_day=month_day)
            fields = dict(spec)
            if channel is not None:
                fields["channel_id"] = channel.id
            turn_off = enabled is False
            turn_on = enabled is True or (enabled is None and existing["status"] in ("active", "finished"))
            if turn_off:
                fields.update(status="paused", next_run_at=None)
                title = "⏸️ DAILY SUMMARY DISABLED"
            elif turn_on:
                fields.update(status="active", next_run_at=to_db(next_run), last_error=None)
                title = "✅ DAILY SUMMARY UPDATED" if enabled is None else "▶️ DAILY SUMMARY ENABLED"
            else:  # still disabled, just changing settings
                fields["next_run_at"] = None
                title = "✏️ DAILY SUMMARY UPDATED (still disabled)"
            db.update_schedule(existing["id"], **fields)
            svc.refresh(existing["id"])
            sched = db.get_schedule(existing["id"])

        out = embeds.schedule_embed(sched, None, tz, now, title=title,
                                    next_run=next_run if sched["status"] == "active" else None)
        await interaction.response.send_message(embed=out)
        if send_now:
            target = interaction.guild.get_channel(sched["channel_id"])
            if target is not None:
                await target.send(embed=embeds.summary_embed(db.list_tasks(interaction.guild_id, "pending"), tz, now),
                                  allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Summary(bot))
