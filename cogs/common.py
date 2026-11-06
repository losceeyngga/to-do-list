"""Helpers shared by all command modules."""
from typing import List, Optional

import discord
from discord import app_commands

from utils import UserError, clip

FREQUENCY_CHOICES = [
    app_commands.Choice(name="Once", value="once"),
    app_commands.Choice(name="Every hour", value="hourly"),
    app_commands.Choice(name="Every day", value="daily"),
    app_commands.Choice(name="Every week (pick weekdays)", value="weekly"),
    app_commands.Choice(name="Every month (pick a day)", value="monthly"),
    app_commands.Choice(name="Custom interval", value="custom"),
]

SUMMARY_FREQUENCY_CHOICES = [
    app_commands.Choice(name="Every day", value="daily"),
    app_commands.Choice(name="Every week (pick weekdays)", value="weekly"),
    app_commands.Choice(name="Every month (pick a day)", value="monthly"),
    app_commands.Choice(name="Every hour", value="hourly"),
]

STATUS_CHOICES = [
    app_commands.Choice(name="Pending", value="pending"),
    app_commands.Choice(name="Completed", value="completed"),
    app_commands.Choice(name="Cancelled", value="cancelled"),
]

SCHEDULE_DOCS = dict(
    time="Time of day, e.g. 8:00 AM or 20:30 (for hourly: only the minute is used)",
    weekdays="For weekly: days like mon,wed,fri or mon-fri or weekdays",
    month_day="For monthly: day of the month (1-31; short months use their last day)",
    run_date="For once: the date to post, e.g. 2026-11-20 or November 20, 2026",
    interval="For custom: how often, e.g. 30m, 2h, 1d or 1h30m (minimum 5m)",
    start_date="Don't post before this date (optional)",
    end_date="Stop posting after this date (optional)",
)


def ensure_manage(interaction: discord.Interaction) -> None:
    """If RESTRICT_TO_MANAGERS is on, only members with 'Manage Server' may change things."""
    if interaction.guild is None:
        raise UserError("This command only works inside a server.")
    if interaction.client.settings.restrict_to_managers:
        perms = getattr(interaction.user, "guild_permissions", None)
        if not perms or not (perms.manage_guild or perms.administrator):
            raise UserError("Only members with the **Manage Server** permission can change tasks and schedules.")


def check_channel(interaction: discord.Interaction, channel) -> None:
    """Make sure the bot can actually post in the chosen channel."""
    if channel is None or not hasattr(channel, "permissions_for"):
        raise UserError("Please choose a normal text channel for announcements.")
    perms = channel.permissions_for(interaction.guild.me)
    missing = [name for name, ok in (
        ("View Channel", perms.view_channel), ("Send Messages", perms.send_messages),
        ("Embed Links", perms.embed_links)) if not ok]
    if missing:
        raise UserError(
            f"I can't post in {channel.mention}. Please give me these permission(s) there: "
            f"**{', '.join(missing)}**.")


def pick_channel(interaction: discord.Interaction, channel: Optional[discord.abc.GuildChannel]):
    chosen = channel or interaction.channel
    if not isinstance(chosen, (discord.TextChannel, discord.Thread)):
        raise UserError("Please pick a text channel with the `channel` option.")
    check_channel(interaction, chosen)
    return chosen


def get_task(interaction: discord.Interaction, task_id: int) -> dict:
    task = interaction.client.db.get_task(interaction.guild_id, task_id)
    if task is None:
        raise UserError(f"I couldn't find task **#{task_id}**. Pick one from the list that appears as you type.")
    return task


def get_schedule(interaction: discord.Interaction, schedule_id: int) -> dict:
    sched = interaction.client.db.get_guild_schedule(interaction.guild_id, schedule_id)
    if sched is None:
        raise UserError(f"I couldn't find schedule **S#{schedule_id}**. Pick one from the list that appears as you type.")
    return sched


async def task_choices(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[int]]:
    if interaction.guild_id is None:
        return []
    needle = (current or "").lower().lstrip("#")
    out = []
    for task in interaction.client.db.list_tasks(interaction.guild_id):
        label = f"#{task['id']} {task['subject']} — {task['description']}"
        if needle and needle not in label.lower():
            continue
        suffix = "" if task["status"] == "pending" else f" [{task['status']}]"
        out.append(app_commands.Choice(name=clip(label + suffix, 100), value=task["id"]))
        if len(out) >= 25:
            break
    return out


async def schedule_choices(interaction: discord.Interaction, current: str,
                           statuses: tuple = ("active", "paused", "finished")) -> List[app_commands.Choice[int]]:
    from scheduling_logic import describe_schedule

    if interaction.guild_id is None:
        return []
    db = interaction.client.db
    needle = (current or "").lower().lstrip("s#")
    out = []
    for s in db.list_schedules(interaction.guild_id, statuses):
        if s["mode"] == "summary":
            name = "Daily Summary"
        else:
            task = db.get_task(interaction.guild_id, s["task_id"]) if s["task_id"] else None
            name = task["subject"] if task else "(task removed)"
        label = f"S#{s['id']} {name} — {describe_schedule(s)} [{s['status']}]"
        if needle and needle not in label.lower():
            continue
        out.append(app_commands.Choice(name=clip(label, 100), value=s["id"]))
        if len(out) >= 25:
            break
    return out


class ConfirmView(discord.ui.View):
    """Two buttons (confirm / cancel) that only the person who ran the command can press."""

    def __init__(self, user_id: int, confirm_label: str = "Yes, delete"):
        super().__init__(timeout=45)
        self.user_id = user_id
        self.value: Optional[bool] = None
        self.confirm_button.label = confirm_label

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the person who ran this command can answer.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._guard(interaction):
            self.value = True
            await interaction.response.defer()
            self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._guard(interaction):
            self.value = False
            await interaction.response.defer()
            self.stop()
