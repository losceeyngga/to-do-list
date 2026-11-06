# Discord Task Management Bot

A Discord bot that stores your class tasks and **automatically posts task announcements** and a
**daily summary** to a channel, on schedules you choose. Everything is saved in a local SQLite
database, so schedules survive restarts.

- Default timezone: **Asia/Manila (UTC+8)**
- Built with Python, discord.py, APScheduler and SQLite

---

## 1. Install Python and the dependencies

1. Install **Python 3.10 or newer** from <https://www.python.org/downloads/>.
   On Windows, tick **"Add python.exe to PATH"** in the installer.
2. Open a terminal (Windows: *Command Prompt* or *PowerShell*) inside this project folder.
3. (Recommended) create a virtual environment:

   ```bash
   python -m venv .venv
   # Windows:        .venv\Scripts\activate
   # macOS / Linux:  source .venv/bin/activate
   ```
4. Install the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

| Package | Why it is needed |
|---|---|
| `discord.py` | talks to Discord and provides slash commands |
| `APScheduler` | runs the timers that publish announcements |
| `python-dotenv` | reads your token from the `.env` file |
| `tzdata` | timezone data (needed on Windows for `Asia/Manila`) |

SQLite comes with Python, so there is nothing to install for the database.

## 2. Create the bot in the Discord Developer Portal

1. Go to <https://discord.com/developers/applications> and click **New Application**.
2. Open the **Bot** tab.
3. **Privileged Gateway Intents:** you do **not** need to turn any of them on. This bot only uses slash commands.
4. Click **Reset Token** and **Copy** the token. Treat it like a password and never share it.

## 3. Invite the bot to your server

1. In the Developer Portal open **OAuth2 → URL Generator**.
2. Under **Scopes** tick **`bot`** and **`applications.commands`**.
3. Under **Bot Permissions** tick: **View Channels**, **Send Messages**, **Embed Links**.
4. Copy the generated URL, open it in your browser, pick your server and click **Authorize**.

> The bot must also be allowed to see and post in the announcement channel (check the channel's
> permissions if announcements don't appear).

## 4. Configure the bot token (never put it in the code)

1. Copy `.env.example` to a new file named **`.env`** (same folder).
2. Open `.env` and replace the placeholder:

   ```
   DISCORD_TOKEN=MTU2ODEyMDE4ODYxNDg2OTEzNA.GTuv3p.U5yE7B7Fb2QlQp2hU6JKOu0txg3wZDpk1GEWcc
   ```
3. *(Recommended)* set `GUILD_ID` to your server's ID so slash commands appear **instantly**:
   in Discord go to **Settings → Advanced → Developer Mode (on)**, then right-click your server
   icon → **Copy Server ID**. Without it, commands are global and the first appearance can take up to an hour.

`.env` is listed in `.gitignore`, so it won't be committed by accident.

## 5. Run the bot

```bash
python main.py
```

You should see `Logged in as ...`, `Synced N slash commands ...` and `Restored N active schedule(s)`.
Keep the terminal open: the bot only posts while it is running. Stop it with **Ctrl+C**.

## 6. Slash commands are registered automatically

On every start the bot registers its commands for you. There is nothing to run separately.
Type `/` in your server to see them. If they don't show up, check that you invited the bot with the
`applications.commands` scope (step 3) and wait for the sync (set `GUILD_ID` to make it instant).

## 7. Timezone

All schedule times use the `TIMEZONE` setting in `.env` (default `Asia/Manila`, UTC+8).
To change it, edit `TIMEZONE=` to any [IANA name](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)
(e.g. `Asia/Tokyo`) and restart. Embeds show the timezone and the publish date/time.

## 8. Database

SQLite is created automatically at `data/taskbot.db` on first run. There is nothing to configure.
Change the location with `DATABASE_PATH` in `.env`. To **back up**, copy that file while the bot is stopped.
Tables: `tasks`, `schedules` (everything about when/where to post, including status, last sent and
next run) and `send_log` (one row per claimed run, used to prevent duplicates). The schema is versioned
(`PRAGMA user_version`) so future updates can migrate it safely.

---

## Commands

Task and schedule pickers autocomplete: start typing and choose from the list.
Times accept `8:00 AM`, `8am` or `20:30`. Dates accept `2026-11-20` or `November 20, 2026`.

### Tasks

| Command | What it does |
|---|---|
| `/add-task` | Create a task. Required: `subject`, `description`, `deadline`. Optional: `auto_send`, `frequency`, `time`, `channel`, `weekdays`, `month_day`, `run_date`, `interval`, `start_date`, `end_date`. Setting `frequency` or `auto_send:True` also creates the announcement schedule (default: every day at 8:00 AM) |
| `/tasks` | List all tasks (optionally filter by status) |
| `/edit-task` | Change a task's subject, description, deadline or channel |
| `/delete-task` | Delete a task and its schedules (asks for confirmation) |
| `/task-status` | Set a task to Pending / Completed / Cancelled |

Example: `/add-task subject:RPH description:Written Assignment deadline:November 20, 2026 auto_send:True frequency:Every day time:8:00 AM channel:#announcements`

### Schedules

| Command | What it does |
|---|---|
| `/schedule` | Add an announcement schedule to a task (a task can have several) |
| `/schedules` | Show all active and paused schedules with their next run |
| `/pause` / `/resume` | Pause a schedule / continue it (missed runs while paused are not replayed) |
| `/editschedule` | Change frequency, time, weekdays, dates or channel (fill in only what changes) |
| `/deleteschedule` | Permanently delete a schedule (asks for confirmation) |

Frequency options and what to fill in:

| Frequency | Extra options |
|---|---|
| Once | `run_date` + `time` |
| Every hour | `time` (only the minute is used: `8:30 AM` means :30 every hour) |
| Every day | `time` |
| Every week | `weekdays` (e.g. `mon,wed,fri`, `mon-fri`, `weekdays`) + `time` |
| Every month | `month_day` (1-31; day 31 uses the last day of shorter months) + `time` |
| Custom | `interval` (e.g. `30m`, `2h`, `1d`, `1h30m`; minimum 5m), counted from `start_date` + `time` |

Every schedule can also use `start_date` and `end_date` (the end date is included).

### Daily summary

`/daily-summary` posts **one embed with all pending tasks** at a set time.

- No options → shows the current setup.
- `/daily-summary enabled:True time:8:00 AM` → turn it on (posts in the channel you type it in; add `channel:` to pick another).
- `enabled:False` → turn it off (settings are kept). Other options: `frequency`, `weekdays`, `month_day`.
- `send_now:True` → also post a summary right away, a handy way to test.

**Both announcement modes can run together:** per-task announcements (individual schedules) and the
daily summary are independent, so enable either or both.

### Optional: limit who can change things

Set `RESTRICT_TO_MANAGERS=true` in `.env` so only members with **Manage Server** can add/edit/delete.
Anyone can still view tasks and schedules.

---

## How reliability works

- **Persistence:** tasks and schedules live in SQLite. On startup the bot rebuilds every active timer
  from the database. Paused, deleted and finished schedules get no timer.
- **No duplicates:** before posting, a run is *claimed* in the database in one atomic step (compare-and-swap on the
  schedule's next run, plus a unique log row). Timers, the safety-net sweeper, or even two copies of the bot
  cannot post the same run twice.
- **Missed runs:** if the bot was offline, a run that is at most `MISFIRE_GRACE_MINUTES` late (default 6 hours; hourly
  schedules: 30 minutes) is posted **once**; older ones are skipped and the schedule continues at its next normal time. You never get a flood of old posts.
- **A safety-net check runs every minute** to re-create any missing timer and fire anything overdue (e.g. after a reconnect).
- **Errors don't crash the bot:** a deleted channel pauses that schedule with an explanation (shown in `/schedules`);
  missing permissions are reported when you create the schedule and logged if they change later; one failing
  announcement never affects the others. Logs go to the console and `logs/bot.log`.
- Announcements are skipped while a task is **Completed** or **Cancelled** (colors in `/tasks` and status updates:
  🟡 pending, 🟢 completed, ⚪ cancelled, 🔴 overdue).

## Running the tests (optional)

```bash
python -m unittest discover -s tests -v
```

The tests run the bot's own code (database, scheduler, slash-command handlers) against small stand-ins for Discord and
APScheduler, so they need no token or internet. They cover restart persistence, paused/deleted schedules,
duplicate prevention, missed runs, errors and the summary.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Configuration problem: DISCORD_TOKEN is missing` | Create `.env` from `.env.example` and paste your token (step 4) |
| `Discord rejected the bot token` | Reset the token in the Developer Portal and paste the new one |
| Slash commands don't appear | Re-invite with the `applications.commands` scope; set `GUILD_ID` for instant sync; restart |
| "I can't post in #channel" | Give the bot **View Channel, Send Messages, Embed Links** in that channel |
| Nothing posts | The bot must be running at the scheduled time; check `/schedules` for the next run and any ⚠️ message |
| Wrong time | Check `TIMEZONE` in `.env`; times are in that timezone |
| `ZoneInfoNotFoundError` on Windows | Run `pip install tzdata` |

## Project layout

```
main.py                bot entry point, command sync, error handler
config.py              settings from .env
database.py            SQLite tables and queries, atomic duplicate-proof claim
scheduler.py           APScheduler timers, restore on start, sweeper, delivery
scheduling_logic.py    pure "when is the next run" calculation + validation
embeds.py              all Discord embed designs
utils.py               parsing / formatting helpers
cogs/                  slash commands (tasks, schedules, summary)
tests/                 offline tests
```
