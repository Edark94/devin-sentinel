from __future__ import annotations

import asyncio

from sentinel.models import TaskState
from tests.conftest import issue


async def ticks(orch, n: int, delay: float = 0.25):
    for _ in range(n):
        await orch.tick()
        await asyncio.sleep(delay)


async def test_happy_path_opens_pr_and_reports(orchestrator, store, github):
    task = await orchestrator.accept_issue(issue(101), trigger="webhook")
    assert task and task.state == TaskState.QUEUED
    await orchestrator.tick()
    t = store.get_task(task.id)
    assert t.state in (TaskState.SESSION_CREATED, TaskState.RUNNING) and t.session_id and t.attempts == 1
    assert "devin:in-progress" in github.labels[101] and "devin:remediate" not in github.labels[101]

    await ticks(orchestrator, 6)
    t = store.get_task(task.id)
    assert t.state == TaskState.PR_OPENED, t.state
    assert t.pr_url and t.pr_url.endswith("/pull/9101")
    assert t.structured_output["outcome"] == "pr_opened"
    assert t.acus_consumed > 0
    assert "devin:pr-open" in github.labels[101]
    assert any("Pull request opened" in b for _, b in github.comments)
    kinds = [e.kind for e in store.list_events(task.id)]
    assert "devin.session_created" in kinds and "devin.pr_detected" in kinds

    # merge detection via PR reconciliation
    github.pr_states[t.pr_url] = "merged"
    await orchestrator.tick()
    assert store.get_task(task.id).state == TaskState.MERGED


async def test_intake_is_idempotent(orchestrator):
    assert await orchestrator.accept_issue(issue(7), trigger="webhook")
    assert await orchestrator.accept_issue(issue(7), trigger="sweep") is None


async def test_concurrency_cap(orchestrator, store):
    for n in (1, 2, 3):
        await orchestrator.accept_issue(issue(n), trigger="sweep")
    await orchestrator.tick()
    with_session = [t for t in store.active_tasks() if t.session_id]
    queued = store.list_tasks([TaskState.QUEUED])
    assert len(with_session) == 2 and len(queued) == 1


async def test_blocked_session_is_nudged_then_completes(orchestrator, store, github):
    task = await orchestrator.accept_issue(issue(202, title="Bump x [sim:block]", body="[sim:block]"), trigger="webhook")
    await ticks(orchestrator, 8)
    t = store.get_task(task.id)
    assert t.nudges >= 1
    assert t.state == TaskState.PR_OPENED, (t.state, t.last_error)
    assert any(e.kind == "devin.nudged" for e in store.list_events(task.id))


async def test_failed_session_is_retried_once_then_succeeds(orchestrator, store, github):
    task = await orchestrator.accept_issue(issue(303, title="Bump y [sim:fail]", body="[sim:fail]"), trigger="webhook")
    await ticks(orchestrator, 8)
    t = store.get_task(task.id)
    assert t.attempts == 2
    assert t.state == TaskState.PR_OPENED, (t.state, t.last_error)
    kinds = [e.kind for e in store.list_events(task.id)]
    assert "task.attempt_failed" in kinds and "task.retry" in kinds


async def test_no_change_needed_outcome(orchestrator, store, github):
    task = await orchestrator.accept_issue(issue(404, title="Already fixed [sim:nochange]", body="[sim:nochange]"), trigger="webhook")
    await ticks(orchestrator, 5)
    t = store.get_task(task.id)
    assert t.state == TaskState.NO_CHANGE and t.pr_url is None
    assert "devin:done" in github.labels[404]
    assert any("no change is needed" in b for _, b in github.comments)


async def test_timeout_terminates_and_fails_after_retries(orchestrator, store, settings):
    settings.session_timeout_minutes = 0
    settings.max_retries = 0
    task = await orchestrator.accept_issue(issue(505, title="slow [sim:block]", body="[sim:block]"), trigger="webhook")
    await orchestrator.tick()          # create session
    await orchestrator.tick()          # timed out immediately -> terminate -> fail (no retries)
    t = store.get_task(task.id)
    assert t.state == TaskState.FAILED and "timed out" in (t.last_error or "")
