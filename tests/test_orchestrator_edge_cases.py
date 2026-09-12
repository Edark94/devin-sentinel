"""Edge cases driven by hand-built Devin session views (no fake server needed)."""

from __future__ import annotations

from sentinel.devin_client import SessionView
from sentinel.models import TaskState
from tests.conftest import issue


class ScriptedDevin:
    """Returns a queue of SessionViews for get_session; records everything else."""

    def __init__(self, views):
        self.views = list(views)
        self.created = 0
        self.messages: list[str] = []
        self.terminated: list[str] = []

    async def create_session(self, prompt, **kw):
        self.created += 1
        return SessionView(session_id=f"s{self.created}", url=f"https://app.devin.ai/sessions/s{self.created}", status="new", status_detail=None, acus_consumed=0)

    async def get_session(self, sid):
        return self.views.pop(0) if len(self.views) > 1 else self.views[0]

    async def send_message(self, sid, msg):
        self.messages.append(msg)

    async def terminate_session(self, sid):
        self.terminated.append(sid)


def view(status, detail, **kw):
    return SessionView(session_id="s1", url="u", status=status, status_detail=detail, acus_consumed=kw.pop("acus", 1.0), **kw)


async def test_blocked_outcome_with_draft_pr_escalates(orchestrator, store, github):
    out = {"outcome": "blocked", "summary": "needs a decision on pytest 9 plugin compat", "follow_ups": ["decide on xdist pin"]}
    orchestrator.devin = ScriptedDevin([view("exit", "finished", pull_requests=[{"pr_url": "https://github.com/Edark94/superset/pull/1", "pr_state": "open"}], structured_output=out)])
    task = await orchestrator.accept_issue(issue(1), trigger="manual")
    await orchestrator.tick()
    await orchestrator.tick()
    t = store.get_task(task.id)
    assert t.state == TaskState.NEEDS_HUMAN and t.pr_url.endswith("/pull/1")
    assert "devin:needs-human" in github.labels[1]
    assert any("Draft PR" in b and "decide on xdist pin" in b for _, b in github.comments)


async def test_budget_exhaustion_escalates_instead_of_retrying(orchestrator, store, github):
    orchestrator.devin = ScriptedDevin([view("suspended", "usage_limit_exceeded", acus=10.0)])
    task = await orchestrator.accept_issue(issue(2), trigger="manual")
    await orchestrator.tick()
    await orchestrator.tick()
    t = store.get_task(task.id)
    assert t.state == TaskState.NEEDS_HUMAN and t.attempts == 1 and orchestrator.devin.created == 1
    assert "usage_limit_exceeded" in t.last_error


async def test_retrigger_of_failed_task_gets_fresh_attempt_budget(orchestrator, store, settings):
    settings.max_retries = 0
    orchestrator.devin = ScriptedDevin([view("error", "error")])
    task = await orchestrator.accept_issue(issue(3), trigger="webhook")
    await orchestrator.tick()
    await orchestrator.tick()
    assert store.get_task(task.id).state == TaskState.FAILED
    # human re-adds the label -> new task with attempts reset, old events kept
    orchestrator.devin = ScriptedDevin([view("exit", "finished", pull_requests=[{"pr_url": "https://github.com/Edark94/superset/pull/3", "pr_state": "open"}], structured_output={"outcome": "pr_opened"})])
    again = await orchestrator.accept_issue(issue(3), trigger="webhook")
    assert again is not None and again.attempts == 0
    await orchestrator.tick()
    await orchestrator.tick()
    t = store.get_task(task.id)
    assert t.state == TaskState.PR_OPENED and t.attempts == 1
    kinds = [e.detail for e in store.list_events(task.id) if e.kind == "task.state"]
    assert any(d.startswith("re-accepted") for d in kinds) and any("-> failed" in d for d in kinds)


async def test_suspended_for_inactivity_without_result_is_retried(orchestrator, store):
    orchestrator.devin = ScriptedDevin([view("suspended", "inactivity")])
    task = await orchestrator.accept_issue(issue(4), trigger="manual")
    await orchestrator.tick()
    await orchestrator.tick()
    t = store.get_task(task.id)
    assert t.attempts == 2 and orchestrator.devin.created == 2


async def test_idle_after_delivery_is_finished_not_nudged(orchestrator, store, github):
    """Real v1 behaviour: after opening the PR Devin sits in `blocked` (waiting for the user)."""
    out = {"outcome": "pr_opened", "pr_url": "https://github.com/Edark94/superset/pull/9", "summary": "one-line bump", "verification": "pip-audit clean", "risk": "low"}
    orchestrator.devin = ScriptedDevin([view("running", "waiting_for_user", pull_requests=[{"pr_url": out["pr_url"], "pr_state": None}], structured_output=out)])
    task = await orchestrator.accept_issue(issue(6), trigger="manual")
    await orchestrator.tick()
    await orchestrator.tick()
    t = store.get_task(task.id)
    assert t.state == TaskState.PR_OPENED and t.nudges == 0 and orchestrator.devin.messages == []
    assert "devin:pr-open" in github.labels[6]


async def test_escalated_task_is_promoted_when_session_delivers(orchestrator, store, github, settings):
    settings.max_nudges = 0
    waiting = view("running", "waiting_for_user")
    delivered = view("running", "waiting_for_user", pull_requests=[{"pr_url": "https://github.com/Edark94/superset/pull/10", "pr_state": None}],
                     structured_output={"outcome": "pr_opened", "summary": "done", "verification": "ok", "risk": "low"})
    orchestrator.devin = ScriptedDevin([waiting, waiting, waiting, delivered])  # tick2 escalates then reconciles (still waiting); tick3 reconciles -> delivered
    task = await orchestrator.accept_issue(issue(7), trigger="manual")
    await orchestrator.tick()   # create + first poll -> waiting, no nudges allowed -> needs_human
    await orchestrator.tick()
    assert store.get_task(task.id).state == TaskState.NEEDS_HUMAN
    await orchestrator.tick()   # reconcile sees delivery -> promoted
    t = store.get_task(task.id)
    assert t.state == TaskState.PR_OPENED and t.pr_url.endswith("/pull/10")
    assert any(e.kind == "task.unblocked" for e in store.list_events(task.id))
