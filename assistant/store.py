"""SQLite persistence: conversations, reminders, standing instructions, facts and voice jobs.

The conversation history is append-only. Claude's thinking blocks are bound to the
conversation that produced them, so we never edit or trim earlier turns; when a
conversation gets long or stale we start a new one instead (see `current_conversation`).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    notes TEXT,
    due_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_at);
CREATE TABLE IF NOT EXISTS instructions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    result TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class Reminder:
    id: int
    title: str
    notes: str | None
    due_at: datetime
    channel: str
    status: str
    created_at: datetime
    delivered_at: datetime | None
    fail_count: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Reminder":
        return cls(
            id=row["id"],
            title=row["title"],
            notes=row["notes"],
            due_at=parse_iso(row["due_at"]),
            channel=row["channel"],
            status=row["status"],
            created_at=parse_iso(row["created_at"]),
            delivered_at=parse_iso(row["delivered_at"]) if row["delivered_at"] else None,
            fail_count=row["fail_count"],
        )


@dataclass
class Instruction:
    id: int
    text: str
    created_at: datetime


@dataclass
class VoiceJob:
    id: str
    kind: str
    status: str
    payload: dict[str, Any]
    result: str | None
    created_at: datetime
    updated_at: datetime


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ conversations
    def current_conversation(self, max_messages: int, idle_hours: int) -> str:
        """Return the active conversation id, rolling over to a fresh one when it is too long or stale."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversations ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            now = utcnow()
            if row is not None:
                stale = parse_iso(row["last_active_at"]) < now - timedelta(hours=idle_hours)
                if row["message_count"] < max_messages and not stale:
                    return row["id"]
            conv_id = uuid.uuid4().hex
            self._conn.execute(
                "INSERT INTO conversations (id, started_at, last_active_at, message_count) VALUES (?, ?, ?, 0)",
                (conv_id, iso(now), iso(now)),
            )
            return conv_id

    def append_message(self, conversation_id: str, role: str, content: Any) -> None:
        with self._lock:
            now = iso(utcnow())
            self._conn.execute(
                "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (conversation_id, role, json.dumps(content, ensure_ascii=False), now),
            )
            self._conn.execute(
                "UPDATE conversations SET message_count = message_count + 1, last_active_at = ? WHERE id = ?",
                (now, conversation_id),
            )

    def conversation_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]

    # ------------------------------------------------------------------ reminders
    def create_reminder(self, title: str, due_at: datetime, channel: str, notes: str | None = None) -> Reminder:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO reminders (title, notes, due_at, channel, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                (title, notes, iso(due_at), channel, iso(utcnow())),
            )
            return self.get_reminder(cur.lastrowid)  # type: ignore[arg-type]

    def get_reminder(self, reminder_id: int) -> Reminder | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return Reminder.from_row(row) if row else None

    def list_reminders(self, include_done: bool = False, limit: int = 50) -> list[Reminder]:
        with self._lock:
            if include_done:
                rows = self._conn.execute(
                    "SELECT * FROM reminders ORDER BY due_at DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM reminders WHERE status = 'pending' ORDER BY due_at ASC LIMIT ?", (limit,)
                ).fetchall()
        return [Reminder.from_row(r) for r in rows]

    def due_reminders(self, now: datetime | None = None) -> list[Reminder]:
        now = now or utcnow()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reminders WHERE status = 'pending' AND due_at <= ? ORDER BY due_at",
                (iso(now),),
            ).fetchall()
        return [Reminder.from_row(r) for r in rows]

    def claim_reminder(self, reminder_id: int) -> bool:
        """Atomically move a pending reminder to 'delivering'. Returns False if someone else got it."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reminders SET status = 'delivering' WHERE id = ? AND status = 'pending'", (reminder_id,)
            )
            return cur.rowcount == 1

    def mark_reminder_delivered(self, reminder_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reminders SET status = 'delivered', delivered_at = ? WHERE id = ?",
                (iso(utcnow()), reminder_id),
            )

    def mark_reminder_failed(self, reminder_id: int, max_attempts: int = 3) -> None:
        with self._lock:
            self._conn.execute("UPDATE reminders SET fail_count = fail_count + 1 WHERE id = ?", (reminder_id,))
            row = self._conn.execute("SELECT fail_count FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
            status = "failed" if row and row["fail_count"] >= max_attempts else "pending"
            self._conn.execute("UPDATE reminders SET status = ? WHERE id = ?", (status, reminder_id))

    def reschedule_reminder(self, reminder_id: int, due_at: datetime) -> Reminder | None:
        with self._lock:
            self._conn.execute(
                "UPDATE reminders SET due_at = ?, status = 'pending', delivered_at = NULL, fail_count = 0 WHERE id = ?",
                (iso(due_at), reminder_id),
            )
        return self.get_reminder(reminder_id)

    def cancel_reminder(self, reminder_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status IN ('pending', 'delivering')",
                (reminder_id,),
            )
            return cur.rowcount == 1

    # ------------------------------------------------------------------ instructions
    def add_instruction(self, text: str) -> Instruction:
        with self._lock:
            now = utcnow()
            cur = self._conn.execute(
                "INSERT INTO instructions (text, active, created_at) VALUES (?, 1, ?)", (text, iso(now))
            )
            return Instruction(id=cur.lastrowid, text=text, created_at=now)  # type: ignore[arg-type]

    def list_instructions(self) -> list[Instruction]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text, created_at FROM instructions WHERE active = 1 ORDER BY id"
            ).fetchall()
        return [Instruction(id=r["id"], text=r["text"], created_at=parse_iso(r["created_at"])) for r in rows]

    def remove_instruction(self, instruction_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE instructions SET active = 0 WHERE id = ? AND active = 1", (instruction_id,)
            )
            return cur.rowcount == 1

    # ------------------------------------------------------------------ facts
    def remember(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO facts (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key.strip().lower(), value, iso(utcnow())),
            )

    def forget(self, key: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM facts WHERE key = ?", (key.strip().lower(),))
            return cur.rowcount == 1

    def facts(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM facts ORDER BY key").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ------------------------------------------------------------------ voice jobs
    def create_voice_job(self, kind: str, payload: dict[str, Any], status: str = "pending") -> VoiceJob:
        with self._lock:
            now = utcnow()
            job_id = uuid.uuid4().hex
            self._conn.execute(
                "INSERT INTO voice_jobs (id, kind, status, payload, result, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (job_id, kind, status, json.dumps(payload, ensure_ascii=False), iso(now), iso(now)),
            )
            return VoiceJob(job_id, kind, status, payload, None, now, now)

    def get_voice_job(self, job_id: str) -> VoiceJob | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM voice_jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        return VoiceJob(
            id=row["id"],
            kind=row["kind"],
            status=row["status"],
            payload=json.loads(row["payload"]),
            result=row["result"],
            created_at=parse_iso(row["created_at"]),
            updated_at=parse_iso(row["updated_at"]),
        )

    def update_voice_job(
        self, job_id: str, *, status: str | None = None, result: str | None = None, payload: dict[str, Any] | None = None
    ) -> None:
        with self._lock:
            sets, params = ["updated_at = ?"], [iso(utcnow())]
            if status is not None:
                sets.append("status = ?")
                params.append(status)
            if result is not None:
                sets.append("result = ?")
                params.append(result)
            if payload is not None:
                sets.append("payload = ?")
                params.append(json.dumps(payload, ensure_ascii=False))
            params.append(job_id)
            self._conn.execute(f"UPDATE voice_jobs SET {', '.join(sets)} WHERE id = ?", params)
