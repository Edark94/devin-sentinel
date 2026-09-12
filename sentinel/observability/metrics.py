"""Prometheus metrics + the leadership-facing summary computed from the store.

Two audiences:
  * SREs: /metrics (counters, gauges, histograms) — plug into an existing Prometheus/Grafana.
  * Engineering leadership: summary() — "is this working?" in six numbers.
"""

from __future__ import annotations

from statistics import median
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

from ..models import TaskState
from ..store import Store

TASKS_TOTAL = Counter("sentinel_tasks_total", "Tasks accepted, by trigger", ["trigger"])
TRANSITIONS_TOTAL = Counter("sentinel_state_transitions_total", "State transitions", ["to_state"])
SESSIONS_CREATED = Counter("sentinel_devin_sessions_created_total", "Devin sessions created")
NUDGES_TOTAL = Counter("sentinel_nudges_total", "Automated replies sent to blocked sessions")
WEBHOOKS_TOTAL = Counter("sentinel_webhooks_total", "GitHub webhook deliveries", ["event", "result"])
DEVIN_API_ERRORS = Counter("sentinel_devin_api_errors_total", "Devin API errors", ["op"])
ACTIVE_TASKS = Gauge("sentinel_active_tasks", "Tasks in an active state", ["state"])
ACUS_CONSUMED = Gauge("sentinel_acus_consumed_total", "Sum of ACUs consumed across all sessions")
TIME_TO_PR = Histogram(
    "sentinel_time_to_pr_seconds", "Seconds from session start to PR opened", buckets=(300, 600, 900, 1800, 2700, 3600, 5400, 7200)
)


def refresh_gauges(store: Store) -> None:
    counts = {s: 0 for s in TaskState if s.is_active}
    acus = 0.0
    for t in store.list_tasks(limit=5000):
        if t.state.is_active:
            counts[t.state] += 1
        acus += t.acus_consumed
    for s, n in counts.items():
        ACTIVE_TASKS.labels(state=s.value).set(n)
    ACUS_CONSUMED.set(acus)


def summary(store: Store) -> dict[str, Any]:
    tasks = store.list_tasks(limit=5000)
    by_state = {s.value: 0 for s in TaskState}
    for t in tasks:
        by_state[t.state.value] += 1
    terminal = [t for t in tasks if t.state.is_terminal]
    successes = [t for t in terminal if t.state.is_success]
    prs = [t for t in tasks if t.pr_url]
    merged = [t for t in tasks if t.state == TaskState.MERGED]
    durations = [t.duration_seconds for t in prs if t.duration_seconds]
    acus_total = sum(t.acus_consumed for t in tasks)
    acus_on_prs = sum(t.acus_consumed for t in prs)
    return {
        "tasks_total": len(tasks),
        "active": sum(1 for t in tasks if t.state.is_active),
        "by_state": by_state,
        "prs_opened": len(prs),
        "prs_merged": len(merged),
        "needs_human": by_state[TaskState.NEEDS_HUMAN.value],
        "failed": by_state[TaskState.FAILED.value],
        "success_rate": round(len(successes) / len(terminal), 3) if terminal else None,
        "median_time_to_pr_minutes": round(median(durations) / 60, 1) if durations else None,
        "acus_total": round(acus_total, 2),
        "acus_per_pr": round(acus_on_prs / len(prs), 2) if prs else None,
        "by_category": _by_category(tasks),
    }


def _by_category(tasks: list[Any]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for t in tasks:
        c = out.setdefault(t.category, {"total": 0, "prs": 0, "failed": 0, "needs_human": 0})
        c["total"] += 1
        c["prs"] += 1 if t.pr_url else 0
        c["failed"] += 1 if t.state == TaskState.FAILED else 0
        c["needs_human"] += 1 if t.state == TaskState.NEEDS_HUMAN else 0
    return out
