"""SQLite persistence for tasks, schedules and the send log.

SQLite is the single source of truth. APScheduler only keeps in-memory timers, which are rebuilt
from this database every time the bot starts.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from utils import to_db, utcnow

log = logging.getLogger("taskbot.db")

TASK_STATUSES = ("pending", "completed", "cancelled")

_SCHEMA_V1 = """
CREATE TABLE tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    subject     TEXT    NOT NULL,
    description TEXT    NOT NULL,
    deadline    TEXT    NOT NULL,                       -- ISO date, e.g. 2026-11-20
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'completed', 'cancelled')),
    channel_id  INTEGER,                                -- default announcement channel
    created_by  INTEGER,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
CREATE INDEX idx_tasks_guild ON tasks (guild_id, status);

CREATE TABLE schedules (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id         INTEGER NOT NULL,
    mode             TEXT    NOT NULL DEFAULT 'individual'
                     CHECK (mode IN ('individual', 'summary')),
    task_id          INTEGER,                           -- NULL for summary schedules
    channel_id       INTEGER NOT NULL,
    schedule_type    TEXT    NOT NULL
                     CHECK (schedule_type IN ('once','hourly','daily','weekly','monthly','custom')),
    time_hhmm        TEXT    NOT NULL DEFAULT '08:00',  -- local time (TIMEZONE setting)
    weekdays         TEXT,                              -- '0,2,4' (Monday = 0)
    month_day        INTEGER,
    run_date         TEXT,                              -- ISO date (for 'once')
    interval_minutes INTEGER,                           -- for 'custom'
    start_date       TEXT,
    end_date         TEXT,
    status           TEXT    NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active','paused','deleted','finished')),
    last_sent_at     TEXT,                              -- UTC ISO
    next_run_at      TEXT,                              -- UTC ISO
    last_error       TEXT,
    created_by       INTEGER,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
CREATE INDEX idx_sched_due   ON schedules (status, next_run_at);
CREATE INDEX idx_sched_guild ON schedules (guild_id, status);
-- Only one (non-deleted) daily-summary schedule per server.
CREATE UNIQUE INDEX idx_one_summary
    ON schedules (guild_id) WHERE mode = 'summary' AND status != 'deleted';

CREATE TABLE send_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id   INTEGER NOT NULL,
    scheduled_for TEXT    NOT NULL,                     -- the run this row claims
    claimed_at    TEXT    NOT NULL,
    outcome       TEXT    NOT NULL DEFAULT 'claimed',
    detail        TEXT,
    UNIQUE (schedule_id, scheduled_for)                 -- a run can be claimed only once
);
"""

# Add future migrations here: index 0 -> upgrades v1 to v2, and so on.
_MIGRATIONS: List[str] = []

_TASK_FIELDS = {"subject", "description", "deadline", "status", "channel_id"}
_SCHEDULE_FIELDS = {
    "channel_id", "schedule_type", "time_hhmm", "weekdays", "month_day", "run_date",
    "interval_minutes", "start_date", "end_date", "status", "last_sent_at",
    "next_run_at", "last_error", "task_id",
}


class DatabaseError(Exception):
    """Wraps sqlite3 errors so callers can show a friendly message."""


class _Abort(Exception):
    pass


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._migrate()
        except sqlite3.Error as exc:
            raise DatabaseError(f"Could not open the database at {self.path}: {exc}") from exc

    # ------------------------------------------------------------------ plumbing
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self._conn.executescript("BEGIN;" + _SCHEMA_V1 + "PRAGMA user_version = 1;COMMIT;")
                version = 1
                log.info("Created new database schema (v1)")
            for idx in range(version - 1, len(_MIGRATIONS)):
                self._conn.executescript("BEGIN;" + _MIGRATIONS[idx] + f"PRAGMA user_version = {idx + 2};COMMIT;")
                log.info("Migrated database to v%d", idx + 2)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _run(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        try:
            with self._lock:
                return self._conn.execute(sql, params)
        except sqlite3.Error as exc:
            log.exception("Database error running: %s", sql)
            raise DatabaseError(str(exc)) from exc

    def _one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._run(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._run(sql, params).fetchall()

    @staticmethod
    def _sched(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        d = dict(row)
        d["weekdays"] = [int(x) for x in d["weekdays"].split(",")] if d.get("weekdays") else []
        return d

    # ------------------------------------------------------------------ tasks
    def create_task(self, guild_id: int, subject: str, description: str, deadline: str,
                    channel_id: Optional[int], created_by: Optional[int]) -> Dict[str, Any]:
        now = to_db(utcnow())
        try:
            with self._tx() as conn:
                cur = conn.execute(
                    "INSERT INTO tasks (guild_id, subject, description, deadline, status, channel_id,"
                    " created_by, created_at, updated_at) VALUES (?,?,?,?, 'pending', ?,?,?,?)",
                    (guild_id, subject, description, deadline, channel_id, created_by, now, now),
                )
                task_id = cur.lastrowid
        except sqlite3.Error as exc:
            log.exception("create_task failed")
            raise DatabaseError(str(exc)) from exc
        return self.get_task(guild_id, task_id)  # type: ignore[return-value]

    def get_task(self, guild_id: int, task_id: int) -> Optional[Dict[str, Any]]:
        row = self._one("SELECT * FROM tasks WHERE id=? AND guild_id=?", (task_id, guild_id))
        return dict(row) if row else None

    def get_task_any_guild(self, task_id: int) -> Optional[Dict[str, Any]]:
        row = self._one("SELECT * FROM tasks WHERE id=?", (task_id,))
        return dict(row) if row else None

    def list_tasks(self, guild_id: int, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            rows = self._all(
                "SELECT * FROM tasks WHERE guild_id=? AND status=? ORDER BY deadline, id", (guild_id, status))
        else:
            rows = self._all("SELECT * FROM tasks WHERE guild_id=? ORDER BY deadline, id", (guild_id,))
        return [dict(r) for r in rows]

    def update_task(self, task_id: int, **fields: Any) -> None:
        bad = set(fields) - _TASK_FIELDS
        if bad:
            raise ValueError(f"cannot update task fields: {bad}")
        if not fields:
            return
        fields["updated_at"] = to_db(utcnow())
        cols = ", ".join(f"{k}=?" for k in fields)
        self._run(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))

    def delete_task(self, task_id: int) -> List[int]:
        """Delete a task and mark all of its schedules deleted. Returns the affected schedule ids."""
        now = to_db(utcnow())
        try:
            with self._tx() as conn:
                ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM schedules WHERE task_id=? AND status != 'deleted'", (task_id,))]
                conn.execute(
                    "UPDATE schedules SET status='deleted', next_run_at=NULL, updated_at=? WHERE task_id=?",
                    (now, task_id))
                conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        except sqlite3.Error as exc:
            log.exception("delete_task failed")
            raise DatabaseError(str(exc)) from exc
        return ids

    # ------------------------------------------------------------------ schedules
    def create_schedule(self, *, guild_id: int, mode: str, task_id: Optional[int], channel_id: int,
                        schedule_type: str, time_hhmm: str, weekdays: Optional[List[int]] = None,
                        month_day: Optional[int] = None, run_date: Optional[str] = None,
                        interval_minutes: Optional[int] = None, start_date: Optional[str] = None,
                        end_date: Optional[str] = None, next_run_at: Optional[str] = None,
                        status: str = "active", created_by: Optional[int] = None) -> Dict[str, Any]:
        now = to_db(utcnow())
        try:
            with self._tx() as conn:
                cur = conn.execute(
                    "INSERT INTO schedules (guild_id, mode, task_id, channel_id, schedule_type, time_hhmm,"
                    " weekdays, month_day, run_date, interval_minutes, start_date, end_date, status,"
                    " next_run_at, created_by, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (guild_id, mode, task_id, channel_id, schedule_type, time_hhmm,
                     ",".join(str(d) for d in weekdays) if weekdays else None, month_day, run_date,
                     interval_minutes, start_date, end_date, status, next_run_at, created_by, now, now),
                )
                sid = cur.lastrowid
        except sqlite3.IntegrityError as exc:
            raise DatabaseError(f"Could not save the schedule (it may already exist): {exc}") from exc
        except sqlite3.Error as exc:
            log.exception("create_schedule failed")
            raise DatabaseError(str(exc)) from exc
        return self.get_schedule(sid)  # type: ignore[return-value]

    def get_schedule(self, schedule_id: int) -> Optional[Dict[str, Any]]:
        return self._sched(self._one("SELECT * FROM schedules WHERE id=?", (schedule_id,)))

    def get_guild_schedule(self, guild_id: int, schedule_id: int) -> Optional[Dict[str, Any]]:
        """A schedule by id, only if it belongs to this server and is not deleted."""
        return self._sched(self._one(
            "SELECT * FROM schedules WHERE id=? AND guild_id=? AND status != 'deleted'",
            (schedule_id, guild_id)))

    def list_schedules(self, guild_id: int, statuses: tuple = ("active", "paused")) -> List[Dict[str, Any]]:
        marks = ",".join("?" for _ in statuses)
        rows = self._all(
            f"SELECT * FROM schedules WHERE guild_id=? AND status IN ({marks}) ORDER BY id",
            (guild_id, *statuses))
        return [self._sched(r) for r in rows]  # type: ignore[misc]

    def list_active_schedules(self) -> List[Dict[str, Any]]:
        return [self._sched(r) for r in self._all(  # type: ignore[misc]
            "SELECT * FROM schedules WHERE status='active' ORDER BY id")]

    def get_summary_schedule(self, guild_id: int) -> Optional[Dict[str, Any]]:
        return self._sched(self._one(
            "SELECT * FROM schedules WHERE guild_id=? AND mode='summary' AND status != 'deleted'",
            (guild_id,)))

    def schedule_counts_by_task(self, guild_id: int) -> Dict[int, int]:
        rows = self._all(
            "SELECT task_id, COUNT(*) AS n FROM schedules WHERE guild_id=? AND mode='individual'"
            " AND status='active' GROUP BY task_id", (guild_id,))
        return {r["task_id"]: r["n"] for r in rows}

    def update_schedule(self, schedule_id: int, **fields: Any) -> None:
        bad = set(fields) - _SCHEDULE_FIELDS
        if bad:
            raise ValueError(f"cannot update schedule fields: {bad}")
        if not fields:
            return
        if "weekdays" in fields:
            wd = fields["weekdays"]
            fields["weekdays"] = ",".join(str(d) for d in wd) if wd else None
        fields["updated_at"] = to_db(utcnow())
        cols = ", ".join(f"{k}=?" for k in fields)
        self._run(f"UPDATE schedules SET {cols} WHERE id=?", (*fields.values(), schedule_id))

    def due_schedules(self, now_iso: str) -> List[Dict[str, Any]]:
        return [self._sched(r) for r in self._all(  # type: ignore[misc]
            "SELECT * FROM schedules WHERE status='active' AND next_run_at IS NOT NULL"
            " AND next_run_at <= ? ORDER BY next_run_at", (now_iso,))]

    # ------------------------------------------------------------------ duplicate prevention
    def claim_and_advance(self, schedule_id: int, expected_next: str, new_next: Optional[str],
                          new_status: str) -> bool:
        """Atomically claim one run of a schedule and move it to its next run.

        Succeeds for exactly one caller per (schedule, run time): a compare-and-swap on
        ``next_run_at`` plus a UNIQUE row in ``send_log``. Returns False if somebody else already
        claimed this run, or the schedule was paused/deleted in the meantime.
        """
        now = to_db(utcnow())
        try:
            with self._tx() as conn:
                cur = conn.execute(
                    "UPDATE schedules SET next_run_at=?, status=?, updated_at=?"
                    " WHERE id=? AND status='active' AND next_run_at=?",
                    (new_next, new_status, now, schedule_id, expected_next))
                if cur.rowcount != 1:
                    raise _Abort
                ins = conn.execute(
                    "INSERT OR IGNORE INTO send_log (schedule_id, scheduled_for, claimed_at) VALUES (?,?,?)",
                    (schedule_id, expected_next, now))
                if ins.rowcount != 1:
                    raise _Abort
        except _Abort:
            return False
        except sqlite3.Error as exc:
            log.exception("claim_and_advance failed")
            raise DatabaseError(str(exc)) from exc
        return True

    def record_outcome(self, schedule_id: int, scheduled_for: str, outcome: str,
                       detail: Optional[str] = None) -> None:
        self._run("UPDATE send_log SET outcome=?, detail=? WHERE schedule_id=? AND scheduled_for=?",
                  (outcome, detail, schedule_id, scheduled_for))

    def mark_sent(self, schedule_id: int, sent_at_iso: str) -> None:
        self._run("UPDATE schedules SET last_sent_at=?, last_error=NULL WHERE id=?",
                  (sent_at_iso, schedule_id))

    def set_error(self, schedule_id: int, message: Optional[str]) -> None:
        self._run("UPDATE schedules SET last_error=? WHERE id=?", (message, schedule_id))

    def get_send_log(self, schedule_id: int) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._all(
            "SELECT * FROM send_log WHERE schedule_id=? ORDER BY scheduled_for", (schedule_id,))]

    def purge_old_logs(self, older_than_iso: str) -> int:
        cur = self._run("DELETE FROM send_log WHERE claimed_at < ?", (older_than_iso,))
        return cur.rowcount
