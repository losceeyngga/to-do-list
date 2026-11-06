"""Discord Task Management Bot: entry point.  Run with:  python main.py"""
import logging
import logging.handlers
import sys

import discord
from discord import app_commands
from discord.ext import commands

import embeds
from config import BASE_DIR, ConfigError, Settings, load_settings
from database import Database, DatabaseError
from scheduler import AnnouncementService
from utils import UserError

log = logging.getLogger("taskbot")

EXTENSIONS = ("cogs.tasks", "cogs.schedules", "cogs.summary")


def setup_logging(level: str) -> None:
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    root.addHandler(console)
    root.addHandler(file_handler)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.INFO)


class TaskBot(commands.Bot):
    def __init__(self, settings: Settings, db: Database):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default(),
                         help_command=None)
        self.settings = settings
        self.tz = settings.tz
        self.db = db
        self.service = AnnouncementService(
            self, db, settings.tz, settings.timezone_name, settings.misfire_grace_minutes)
        self.tree.on_error = self.on_tree_error

    async def setup_hook(self) -> None:
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        # Register slash commands. With GUILD_ID set they appear instantly in that server;
        # otherwise they are global (can take up to an hour the first time).
        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d slash commands to server %s", len(synced), self.settings.guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global slash commands", len(synced))
        self.service.start()  # restores every active schedule from the database

    async def close(self) -> None:
        self.service.shutdown()
        await super().close()
        self.db.close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id %s) in %d server(s)", self.user, self.user.id, len(self.guilds))

    async def on_resumed(self) -> None:
        log.info("Connection to Discord resumed")

    async def on_disconnect(self) -> None:
        log.warning("Disconnected from Discord (discord.py will reconnect automatically)")

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, UserError):
            message = str(original)
        elif isinstance(error, app_commands.TransformerError):
            message = "I didn't understand one of the options. For task/schedule, pick an entry from the list that appears as you type."
        elif isinstance(error, app_commands.CheckFailure):
            message = "You don't have permission to use this command."
        elif isinstance(original, DatabaseError):
            log.error("Database error in /%s: %s", interaction.command.name if interaction.command else "?", original)
            message = "I had trouble reading or saving data. Please try again in a moment."
        elif isinstance(original, discord.Forbidden):
            message = "Discord says I don't have permission to do that here. Check my channel permissions."
        else:
            log.exception("Unhandled error in /%s", interaction.command.name if interaction.command else "?",
                          exc_info=original)
            message = "Something unexpected went wrong. It has been logged. Please try again."
        embed = embeds.error_embed(message)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            log.warning("Could not deliver an error message to the user")


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\nConfiguration problem: {exc}\n", file=sys.stderr)
        return 1
    setup_logging(settings.log_level)
    try:
        db = Database(settings.db_path)
    except DatabaseError as exc:
        log.error("%s", exc)
        return 1
    log.info("Timezone: %s | Database: %s", settings.timezone_name, settings.db_path)
    bot = TaskBot(settings, db)
    try:
        bot.run(settings.token, log_handler=None)
    except discord.LoginFailure:
        log.error("Discord rejected the bot token. Check DISCORD_TOKEN in your .env file "
                  "(use 'Reset Token' in the Developer Portal if unsure).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
