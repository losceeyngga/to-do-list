"""Configuration loaded from environment variables / the .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class ConfigError(Exception):
    """Raised when the bot is configured incorrectly (shown to the user at startup)."""


@dataclass(frozen=True)
class Settings:
    token: str
    timezone_name: str
    tz: ZoneInfo
    db_path: Path
    guild_id: Optional[int]
    misfire_grace_minutes: int
    restrict_to_managers: bool
    log_level: str


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token.lower().startswith("paste"):
        raise ConfigError(
            "DISCORD_TOKEN is missing. Copy .env.example to .env and paste your bot token "
            "after DISCORD_TOKEN= (see README step 4)."
        )

    tz_name = os.getenv("TIMEZONE", "Asia/Manila").strip() or "Asia/Manila"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(
            f"TIMEZONE '{tz_name}' is not a valid timezone name. Use a name like 'Asia/Manila'. "
            "On Windows, also make sure the 'tzdata' package is installed "
            "(pip install -r requirements.txt)."
        ) from None

    db_raw = os.getenv("DATABASE_PATH", "").strip()
    db_path = Path(db_raw) if db_raw else BASE_DIR / "data" / "taskbot.db"
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    guild_raw = os.getenv("GUILD_ID", "").strip()
    guild_id: Optional[int] = None
    if guild_raw:
        if not guild_raw.isdigit():
            raise ConfigError("GUILD_ID must be a number (your server's ID) or left empty.")
        guild_id = int(guild_raw)

    grace_raw = os.getenv("MISFIRE_GRACE_MINUTES", "360").strip() or "360"
    if not grace_raw.isdigit() or int(grace_raw) < 1:
        raise ConfigError("MISFIRE_GRACE_MINUTES must be a positive whole number.")

    return Settings(
        token=token,
        timezone_name=tz_name,
        tz=tz,
        db_path=db_path,
        guild_id=guild_id,
        misfire_grace_minutes=int(grace_raw),
        restrict_to_managers=_as_bool(os.getenv("RESTRICT_TO_MANAGERS", "false")),
        log_level=(os.getenv("LOG_LEVEL", "INFO").strip() or "INFO").upper(),
    )
