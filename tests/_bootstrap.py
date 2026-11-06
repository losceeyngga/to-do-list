"""Test bootstrap: puts the project on sys.path and installs tiny stand-ins for discord.py and
APScheduler, so the project's own logic (database, scheduling, commands, restart recovery,
duplicate prevention) can be tested offline without a Discord connection.

The stand-ins only imitate the small part of each library that this project uses.
"""
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _mod(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


# ------------------------------------------------------------------ discord stand-in
class HTTPException(Exception):
    def __init__(self, status: int = 500, text: str = ""):
        super().__init__(text or f"HTTP {status}")
        self.status = status


class Forbidden(HTTPException):
    def __init__(self, text: str = "Forbidden"):
        super().__init__(403, text)


class NotFound(HTTPException):
    def __init__(self, text: str = "Not Found"):
        super().__init__(404, text)


class Embed:
    def __init__(self, title=None, description=None, color=None, **_):
        self.title, self.description, self.color = title, description, color
        self.fields: List[SimpleNamespace] = []
        self.footer = SimpleNamespace(text=None)
        self.timestamp = None

    def add_field(self, *, name, value, inline=True):
        self.fields.append(SimpleNamespace(name=name, value=value, inline=inline))
        return self

    def set_footer(self, *, text=None, **_):
        self.footer = SimpleNamespace(text=text)
        return self


class AllowedMentions:
    @classmethod
    def none(cls):
        return cls()


class TextChannel:
    pass


class Thread:
    pass


class _Item:
    label = None


def _button(**_):
    def deco(fn):
        item = _Item()
        item.callback = fn
        return item
    return deco


class View:
    def __init__(self, timeout=None):
        self.timeout = timeout

    def stop(self):
        pass

    async def wait(self):
        return None


def _install_discord():
    discord_mod = _mod(
        "discord", HTTPException=HTTPException, Forbidden=Forbidden, NotFound=NotFound, Embed=Embed,
        AllowedMentions=AllowedMentions, TextChannel=TextChannel, Thread=Thread, Interaction=object,
        Object=lambda id: SimpleNamespace(id=id), Intents=SimpleNamespace(default=lambda: None),
        LoginFailure=type("LoginFailure", (Exception,), {}),
        ButtonStyle=SimpleNamespace(danger=1, secondary=2),
    )
    discord_mod.abc = SimpleNamespace(GuildChannel=object)
    discord_mod.ui = SimpleNamespace(View=View, Button=_Item, button=_button)

    class Choice:
        def __init__(self, name, value):
            self.name, self.value = name, value

        def __class_getitem__(cls, item):
            return cls

    class Range:
        def __class_getitem__(cls, item):
            return item[0]

    class _Command:
        def __init__(self, fn, name, description):
            self.callback, self.name, self.description = fn, name, description

        def autocomplete(self, _param):
            return lambda f: f

    def command(*, name, description):
        return lambda fn: _Command(fn, name, description)

    ident = lambda **_: (lambda f: f)  # noqa: E731
    app_commands = _mod(
        "discord.app_commands", Choice=Choice, Range=Range, command=command, describe=ident,
        choices=ident, guild_only=lambda: (lambda f: f),
        AppCommandError=type("AppCommandError", (Exception,), {}),
        TransformerError=type("TransformerError", (Exception,), {}),
        CheckFailure=type("CheckFailure", (Exception,), {}),
    )
    discord_mod.app_commands = app_commands
    commands_mod = _mod("discord.ext.commands", Cog=object, Bot=object, when_mentioned=None)
    ext = _mod("discord.ext", commands=commands_mod)
    discord_mod.ext = ext


# ------------------------------------------------------------------ APScheduler stand-in
class FakeJob:
    def __init__(self, id, func, trigger, args):
        self.id, self.func, self.trigger, self.args = id, func, trigger, args


class FakeScheduler:
    def __init__(self, *a, **kw):
        self.jobs: Dict[str, FakeJob] = {}
        self.running = False

    def add_job(self, func, trigger=None, args=None, id=None, replace_existing=False, **kw):
        if id in self.jobs and not replace_existing:
            raise ValueError("job exists")
        self.jobs[id] = FakeJob(id, func, trigger, args or [])
        return self.jobs[id]

    def get_job(self, id):
        return self.jobs.get(id)

    def remove_job(self, id):
        self.jobs.pop(id)

    def start(self):
        self.running = True

    def shutdown(self, wait=True):
        self.running = False


def _install_apscheduler():
    _mod("apscheduler")
    _mod("apscheduler.schedulers")
    _mod("apscheduler.schedulers.asyncio", AsyncIOScheduler=FakeScheduler)
    _mod("apscheduler.triggers")
    _mod("apscheduler.triggers.date", DateTrigger=lambda run_date: SimpleNamespace(run_date=run_date))
    _mod("apscheduler.triggers.interval", IntervalTrigger=lambda **kw: SimpleNamespace(**kw))


_install_discord()
_install_apscheduler()


# ------------------------------------------------------------------ fakes used by system tests
class FakeClock:
    def __init__(self, start: datetime):
        self.value = start

    def __call__(self) -> datetime:
        return self.value

    def set(self, dt: datetime) -> None:
        self.value = dt


class FakeChannel(TextChannel):
    def __init__(self, channel_id: int, guild):
        self.id, self.guild = channel_id, guild
        self.sent: List[Embed] = []
        self.can_send = True
        self.fail_with: Optional[Exception] = None

    @property
    def mention(self):
        return f"<#{self.id}>"

    def permissions_for(self, _member):
        return SimpleNamespace(view_channel=True, send_messages=self.can_send, embed_links=True)

    async def send(self, *, embed=None, **_):
        if self.fail_with:
            raise self.fail_with
        self.sent.append(embed)


class FakeGuild:
    def __init__(self):
        self.id = 1
        self.me = object()
        self.channels: Dict[int, FakeChannel] = {}

    def get_channel(self, cid):
        return self.channels.get(cid)


class FakeBot:
    def __init__(self, guild: FakeGuild, tz, db):
        self.guild, self.tz, self.db = guild, tz, db
        self.settings = SimpleNamespace(restrict_to_managers=False)
        self.service = None

    def is_ready(self):
        return True

    async def wait_until_ready(self):
        return None

    def get_channel(self, cid):
        return self.guild.get_channel(cid)

    async def fetch_channel(self, cid):
        raise NotFound()


class FakeResponse:
    def __init__(self):
        self.messages: List[dict] = []
        self.done = False

    def is_done(self):
        return self.done

    async def send_message(self, content=None, embed=None, embeds=None, ephemeral=False, view=None):
        self.done = True
        self.messages.append(dict(content=content, embeds=embeds or ([embed] if embed else []),
                                  ephemeral=ephemeral))


class FakeInteraction:
    def __init__(self, bot: FakeBot, channel: FakeChannel):
        self.client, self.guild, self.guild_id, self.channel = bot, bot.guild, bot.guild.id, channel
        self.user = SimpleNamespace(id=42, guild_permissions=SimpleNamespace(manage_guild=False, administrator=False))
        self.response = FakeResponse()

    @property
    def embeds(self):
        return [e for m in self.response.messages for e in m["embeds"]]
