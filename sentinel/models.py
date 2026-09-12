"""Domain model: a remediation Task and its lifecycle."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class TaskState(StrEnum):
    """Lifecycle of one remediation task (one GitHub issue -> one or more Devin sessions)."""

    QUEUED = "queued"                # accepted, waiting for a concurrency slot
    SESSION_CREATED = "session_created"
    RUNNING = "running"              # Devin actively working
    BLOCKED = "blocked"              # Devin waiting for user input; sentinel nudges
    PR_OPENED = "pr_opened"          # terminal-ish success: PR exists, awaiting review
    MERGED = "merged"                # PR merged
    NO_CHANGE = "no_change"          # Devin concluded nothing to do (documented on issue)
    NEEDS_HUMAN = "needs_human"      # escalated; nudges exhausted or Devin reported blocked
    FAILED = "failed"                # session errored / timed out / budget exceeded
    CANCELLED = "cancelled"

    @property
    def is_active(self) -> bool:
        return self in {
            TaskState.QUEUED,
            TaskState.SESSION_CREATED,
            TaskState.RUNNING,
            TaskState.BLOCKED,
        }

    @property
    def is_success(self) -> bool:
        return self in {TaskState.PR_OPENED, TaskState.MERGED, TaskState.NO_CHANGE}

    @property
    def is_terminal(self) -> bool:
        return not self.is_active


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Task:
    id: str                       # "<owner>/<repo>#<issue>"
    repo: str
    issue_number: int
    issue_title: str
    issue_body: str
    issue_url: str
    labels: list[str] = field(default_factory=list)
    category: str = "general"      # security | dependencies | code-quality | general
    trigger: str = "webhook"       # webhook | sweep | manual | scanner
    state: TaskState = TaskState.QUEUED
    session_id: str | None = None
    session_url: str | None = None
    pr_url: str | None = None
    pr_state: str | None = None
    attempts: int = 0
    nudges: int = 0
    acus_consumed: float = 0.0
    last_error: str | None = None
    structured_output: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    session_started_at: datetime | None = None
    finished_at: datetime | None = None

    # ---- helpers -------------------------------------------------------
    @property
    def duration_seconds(self) -> float | None:
        if not self.session_started_at:
            return None
        end = self.finished_at or utcnow()
        return (end - self.session_started_at).total_seconds()

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        d["labels"] = json.dumps(self.labels)
        d["structured_output"] = json.dumps(self.structured_output) if self.structured_output else None
        for k in ("created_at", "updated_at", "session_started_at", "finished_at"):
            d[k] = d[k].isoformat() if d[k] else None
        return d

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Task:
        d = dict(row)
        d["state"] = TaskState(d["state"])
        d["labels"] = json.loads(d["labels"] or "[]")
        d["structured_output"] = json.loads(d["structured_output"]) if d.get("structured_output") else None
        for k in ("created_at", "updated_at", "session_started_at", "finished_at"):
            d[k] = datetime.fromisoformat(d[k]) if d.get(k) else None
        return cls(**d)

    def to_api(self) -> dict[str, Any]:
        d = self.to_row()
        d["labels"] = self.labels
        d["structured_output"] = self.structured_output
        d["duration_seconds"] = self.duration_seconds
        d["is_active"] = self.state.is_active
        return d


@dataclass
class Event:
    """Audit-trail entry. Every state transition and external side effect is recorded."""

    task_id: str
    kind: str
    detail: str = ""
    ts: datetime = field(default_factory=utcnow)
    id: int | None = None

    def to_api(self) -> dict[str, Any]:
        return {"id": self.id, "task_id": self.task_id, "kind": self.kind, "detail": self.detail, "ts": self.ts.isoformat()}
