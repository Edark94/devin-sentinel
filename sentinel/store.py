"""SQLite persistence. Deliberately boring: one file, no ORM, safe for a single orchestrator process."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Event, Task, TaskState

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    issue_title TEXT NOT NULL,
    issue_body TEXT NOT NULL,
    issue_url TEXT NOT NULL,
    labels TEXT NOT NULL,
    category TEXT NOT NULL,
    trigger TEXT NOT NULL,
    state TEXT NOT NULL,
    session_id TEXT,
    session_url TEXT,
    pr_url TEXT,
    pr_state TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    nudges INTEGER NOT NULL DEFAULT 0,
    acus_consumed REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    structured_output TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    session_started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    # ---- tasks ---------------------------------------------------------
    def upsert_task(self, task: Task) -> None:
        row = task.to_row()
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row)
        updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "id")
        with self._lock:
            self._conn.execute(
                f"INSERT INTO tasks ({cols}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}",
                row,
            )

    def get_task(self, task_id: str) -> Task | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return Task.from_row(dict(r)) if r else None

    def get_task_by_session(self, session_id: str) -> Task | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM tasks WHERE session_id=?", (session_id,)).fetchone()
        return Task.from_row(dict(r)) if r else None

    def find_task_by_pr(self, pr_url: str) -> Task | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM tasks WHERE pr_url=?", (pr_url,)).fetchone()
        return Task.from_row(dict(r)) if r else None

    def list_tasks(self, states: list[TaskState] | None = None, limit: int = 500) -> list[Task]:
        q = "SELECT * FROM tasks"
        params: list[Any] = []
        if states:
            q += " WHERE state IN (" + ",".join("?" * len(states)) + ")"
            params += [s.value for s in states]
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [Task.from_row(dict(r)) for r in rows]

    def active_tasks(self) -> list[Task]:
        return self.list_tasks([s for s in TaskState if s.is_active])

    # ---- events --------------------------------------------------------
    def add_event(self, event: Event) -> Event:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (task_id, kind, detail, ts) VALUES (?,?,?,?)",
                (event.task_id, event.kind, event.detail, event.ts.isoformat()),
            )
            event.id = cur.lastrowid
        return event

    def list_events(self, task_id: str | None = None, limit: int = 200) -> list[Event]:
        q = "SELECT * FROM events"
        params: list[Any] = []
        if task_id:
            q += " WHERE task_id=?"
            params.append(task_id)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [
            Event(id=r["id"], task_id=r["task_id"], kind=r["kind"], detail=r["detail"], ts=datetime.fromisoformat(r["ts"]))
            for r in rows
        ]

    # ---- webhook dedupe ------------------------------------------------
    def record_delivery(self, delivery_id: str, received_at: datetime) -> bool:
        """Returns True if this delivery id is new (first time seen)."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO deliveries (delivery_id, received_at) VALUES (?,?)",
                    (delivery_id, received_at.isoformat()),
                )
                return True
            except sqlite3.IntegrityError:
                return False
