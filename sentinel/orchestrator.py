"""The orchestrator: a small state machine that owns the lifecycle of every remediation task.

Responsibilities
  * accept tasks from any trigger (webhook, sweep, scanner, manual) — idempotent per issue
  * admit tasks into Devin sessions under a concurrency cap and per-session ACU budget
  * poll Devin, translate session status into task state, and react:
      - waiting_for_user  -> nudge (bounded), then escalate to a human
      - finished           -> harvest PR + structured output, report on the issue
      - error / timeout    -> retry once with context, then fail loudly
  * mirror every state on the GitHub issue (labels + comments) so the team never needs the dashboard
  * record every transition as an event (audit trail + analytics)

Everything here is deliberately synchronous-in-spirit: one tick() call does one pass over all active
tasks. That makes it trivially testable and safe to run from a scheduler or a background loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from .config import Settings
from .devin_client import DevinAPIError, DevinClient, SessionView
from .github_client import GitHubClient
from .models import Event, Task, TaskState, utcnow
from .observability import metrics
from .prompts import STRUCTURED_OUTPUT_SCHEMA, build_prompt, nudge_message
from .store import Store

log = logging.getLogger("sentinel.orchestrator")

STATUS_LABELS = {
    TaskState.QUEUED: "devin:queued",
    TaskState.SESSION_CREATED: "devin:in-progress",
    TaskState.RUNNING: "devin:in-progress",
    TaskState.BLOCKED: "devin:in-progress",
    TaskState.PR_OPENED: "devin:pr-open",
    TaskState.MERGED: "devin:done",
    TaskState.NO_CHANGE: "devin:done",
    TaskState.NEEDS_HUMAN: "devin:needs-human",
    TaskState.FAILED: "devin:failed",
    TaskState.CANCELLED: "devin:failed",
}
ALL_STATUS_LABELS = sorted(set(STATUS_LABELS.values()))
LABEL_DEFINITIONS = {
    "devin:remediate": ("5319e7", "Trigger: hand this issue to Devin"),
    "devin:queued": ("c5def5", "Sentinel accepted the issue, waiting for a session slot"),
    "devin:in-progress": ("fbca04", "A Devin session is working on this"),
    "devin:pr-open": ("0e8a16", "Devin opened a pull request"),
    "devin:done": ("0e8a16", "Remediated (merged or no change needed)"),
    "devin:needs-human": ("d93f0b", "Devin needs a decision from a human"),
    "devin:failed": ("b60205", "Automated remediation failed"),
    "security": ("ee0701", "Security finding"),
    "dependencies": ("0366d6", "Dependency upgrade"),
    "code-quality": ("bfd4f2", "Code quality"),
    "frontend": ("d4c5f9", "superset-frontend"),
}
CATEGORY_LABELS = ("security", "dependencies", "code-quality", "frontend")


class Orchestrator:
    def __init__(self, cfg: Settings, store: Store, devin: DevinClient, github: GitHubClient):
        self.cfg = cfg
        self.store = store
        self.devin = devin
        self.gh = github
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ intake
    async def accept_issue(self, issue: dict[str, Any], trigger: str) -> Task | None:
        """Idempotent: an issue that already has a non-failed task is ignored."""
        number = int(issue["number"])
        task_id = f"{self.cfg.github_repo}#{number}"
        existing = self.store.get_task(task_id)
        if existing and existing.state.is_active:
            log.info("issue #%s already active (%s), ignoring", number, existing.state)
            return None
        if existing and existing.state.is_success:
            log.info("issue #%s already remediated (%s), ignoring", number, existing.state)
            return None
        labels = [lbl["name"] if isinstance(lbl, dict) else str(lbl) for lbl in issue.get("labels", [])]
        task = Task(
            id=task_id,
            repo=self.cfg.github_repo,
            issue_number=number,
            issue_title=issue.get("title") or f"Issue #{number}",
            issue_body=issue.get("body") or "",
            issue_url=issue.get("html_url") or f"https://github.com/{self.cfg.github_repo}/issues/{number}",
            labels=labels,
            category=next((c for c in CATEGORY_LABELS if c in labels), "general"),
            trigger=trigger,
        )
        if existing:  # re-trigger of a failed / needs-human task: keep history, reset attempt counters
            task.attempts = existing.attempts
        self.store.upsert_task(task)
        metrics.TASKS_TOTAL.labels(trigger=trigger).inc()
        await self._transition(task, TaskState.QUEUED, f"accepted via {trigger}")
        return task

    # ------------------------------------------------------------------ tick
    async def tick(self) -> None:
        """One pass: admit queued tasks, poll active sessions, reconcile open PRs."""
        async with self._lock:
            await self._admit_queued()
            for task in self.store.active_tasks():
                if task.session_id:
                    try:
                        await self._poll_task(task)
                    except DevinAPIError as exc:
                        metrics.DEVIN_API_ERRORS.labels(op="get_session").inc()
                        log.warning("poll failed for %s: %s", task.id, exc)
                    except Exception:  # never let one task break the loop
                        log.exception("unexpected error polling %s", task.id)
            await self._reconcile_prs()
            metrics.refresh_gauges(self.store)

    async def _admit_queued(self) -> None:
        running = [t for t in self.store.active_tasks() if t.session_id]
        slots = self.cfg.max_concurrent_sessions - len(running)
        if slots <= 0:
            return
        queued = sorted(self.store.list_tasks([TaskState.QUEUED]), key=lambda t: t.created_at)
        for task in queued[:slots]:
            await self._start_session(task)

    async def _start_session(self, task: Task, retry_context: str | None = None) -> None:
        prompt = build_prompt(
            task, repo=self.cfg.github_repo, default_branch=self.cfg.github_default_branch, max_acu=self.cfg.devin_max_acu_per_session
        )
        if retry_context:
            prompt += f"\n\n## Note from the orchestrator\nA previous attempt failed: {retry_context}\nAvoid repeating it.\n"
        task.attempts += 1
        tags = ["sentinel", f"issue-{task.issue_number}", task.category, self.cfg.repo_name]
        try:
            view = await self.devin.create_session(
                prompt,
                title=f"[sentinel] #{task.issue_number} {task.issue_title[:70]}",
                tags=tags,
                max_acu_limit=self.cfg.devin_max_acu_per_session,
                structured_output_schema=STRUCTURED_OUTPUT_SCHEMA,
                repos=[f"https://github.com/{self.cfg.github_repo}"],
                devin_mode=self.cfg.devin_mode or None,
                idempotent=False,
            )
        except DevinAPIError as exc:
            metrics.DEVIN_API_ERRORS.labels(op="create_session").inc()
            task.last_error = f"create_session: {exc}"
            self.store.upsert_task(task)
            self._event(task, "devin.create_failed", str(exc))
            if task.attempts > self.cfg.max_retries:
                await self._transition(task, TaskState.FAILED, f"could not create session: {exc}")
            return
        metrics.SESSIONS_CREATED.inc()
        task.session_id = view.session_id
        task.session_url = view.url
        task.session_started_at = utcnow()
        task.nudges = 0
        self.store.upsert_task(task)
        self._event(task, "devin.session_created", f"{view.session_id} attempt={task.attempts}")
        await self._transition(task, TaskState.SESSION_CREATED, f"session {view.session_id}")
        await self._comment(
            task,
            f"🤖 **Devin picked this up** (attempt {task.attempts}, budget {self.cfg.devin_max_acu_per_session} ACU).\n\n"
            f"Session: {view.url}\n\nI will post the pull request here when it is ready.",
        )

    # ------------------------------------------------------------------ polling
    async def _poll_task(self, task: Task) -> None:
        assert task.session_id
        view = await self.devin.get_session(task.session_id)
        if view.acus_consumed != task.acus_consumed:
            task.acus_consumed = view.acus_consumed
        pr_url = view.first_pr_url or (view.structured_output or {}).get("pr_url")
        if pr_url and not task.pr_url:
            task.pr_url = pr_url
            self._event(task, "devin.pr_detected", pr_url)
        if view.structured_output and view.structured_output != task.structured_output:
            task.structured_output = view.structured_output
            self._event(task, "devin.structured_output", str(view.structured_output)[:500])
        self.store.upsert_task(task)

        # ---- decide -----------------------------------------------------
        if view.is_finished or (view.is_terminal and not view.is_error):
            await self._finish(task, view)
        elif view.is_error:
            await self._fail_or_retry(task, f"session {view.status}/{view.status_detail}")
        elif view.is_waiting_for_user:
            await self._handle_blocked(task, view)
        elif self._timed_out(task):
            await self.devin.terminate_session(task.session_id)
            self._event(task, "devin.terminated", "timeout")
            await self._fail_or_retry(task, f"timed out after {self.cfg.session_timeout_minutes} min")
        elif task.state != TaskState.RUNNING:
            await self._transition(task, TaskState.RUNNING, f"devin {view.status}/{view.status_detail}")

    def _timed_out(self, task: Task) -> bool:
        return bool(task.session_started_at) and utcnow() - task.session_started_at > timedelta(minutes=self.cfg.session_timeout_minutes)

    async def _handle_blocked(self, task: Task, view: SessionView) -> None:
        if task.state != TaskState.BLOCKED:
            await self._transition(task, TaskState.BLOCKED, f"devin {view.status_detail}")
        if task.nudges >= self.cfg.max_nudges:
            await self._escalate(task, "Devin is waiting for input and automated nudges are exhausted.", view)
            return
        task.nudges += 1
        self.store.upsert_task(task)
        metrics.NUDGES_TOTAL.inc()
        try:
            await self.devin.send_message(task.session_id or "", nudge_message(task, task.nudges, self.cfg.max_nudges))
            self._event(task, "devin.nudged", f"nudge {task.nudges}/{self.cfg.max_nudges}")
        except DevinAPIError as exc:
            metrics.DEVIN_API_ERRORS.labels(op="send_message").inc()
            self._event(task, "devin.nudge_failed", str(exc))

    async def _finish(self, task: Task, view: SessionView) -> None:
        task.finished_at = utcnow()
        out = task.structured_output or {}
        outcome = out.get("outcome")
        if task.pr_url:
            if task.state != TaskState.PR_OPENED:
                if task.duration_seconds:
                    metrics.TIME_TO_PR.observe(task.duration_seconds)
                await self._transition(task, TaskState.PR_OPENED, task.pr_url)
                await self._comment(task, self._pr_comment(task, out))
        elif outcome == "no_change_needed":
            await self._transition(task, TaskState.NO_CHANGE, out.get("summary", ""))
            await self._comment(task, f"✅ **Devin concluded no change is needed.**\n\n{out.get('summary', '')}\n\n"
                                      f"Verification: {out.get('verification', '-')}\n\nSession: {task.session_url}")
        elif outcome == "blocked":
            await self._escalate(task, out.get("summary", "Devin reported it is blocked."), view)
        else:
            # finished without a PR and without a usable structured output -> treat as failure (retryable)
            await self._fail_or_retry(task, f"session ended ({view.status}/{view.status_detail}) without a PR or a structured outcome")

    async def _fail_or_retry(self, task: Task, reason: str) -> None:
        task.last_error = reason
        self.store.upsert_task(task)
        self._event(task, "task.attempt_failed", reason)
        if task.attempts <= self.cfg.max_retries:
            self._event(task, "task.retry", f"attempt {task.attempts + 1}")
            task.session_id = None
            task.session_url = None
            await self._start_session(task, retry_context=reason)
            return
        task.finished_at = utcnow()
        await self._transition(task, TaskState.FAILED, reason)
        await self._comment(task, f"❌ **Automated remediation failed** after {task.attempts} attempt(s): {reason}\n\n"
                                  f"Last session: {task.session_url}\n\nRe-add the `{self.cfg.trigger_label}` label to retry.")

    async def _escalate(self, task: Task, reason: str, view: SessionView | None) -> None:
        task.finished_at = utcnow()
        task.last_error = reason
        self.store.upsert_task(task)
        await self._transition(task, TaskState.NEEDS_HUMAN, reason)
        follow = (task.structured_output or {}).get("follow_ups") or []
        body = f"🙋 **Devin needs a human.** {reason}\n\nSession (reply there to unblock): {task.session_url}"
        if task.pr_url:
            body += f"\n\nDraft PR: {task.pr_url}"
        if follow:
            body += "\n\nWhat a human must do:\n" + "\n".join(f"- {f}" for f in follow)
        await self._comment(task, body)

    # ------------------------------------------------------------------ PR reconciliation
    async def _reconcile_prs(self) -> None:
        for task in self.store.list_tasks([TaskState.PR_OPENED]):
            if not task.pr_url:
                continue
            state = await self.gh.pr_state_from_url(task.pr_url)
            if state and state != task.pr_state:
                task.pr_state = state
                self.store.upsert_task(task)
                self._event(task, "github.pr_state", state)
            if state == "merged":
                await self._transition(task, TaskState.MERGED, task.pr_url)

    async def on_pull_request_event(self, payload: dict[str, Any]) -> None:
        pr = payload.get("pull_request") or {}
        task = self.store.find_task_by_pr(pr.get("html_url", ""))
        if not task:
            return
        if payload.get("action") == "closed":
            task.pr_state = "merged" if pr.get("merged") else "closed"
            self.store.upsert_task(task)
            self._event(task, "github.pr_state", task.pr_state)
            if task.pr_state == "merged":
                await self._transition(task, TaskState.MERGED, task.pr_url or "")

    # ------------------------------------------------------------------ helpers
    async def _transition(self, task: Task, state: TaskState, detail: str = "") -> None:
        if task.state == state and state != TaskState.QUEUED:
            return
        old = task.state
        task.state = state
        task.updated_at = utcnow()
        self.store.upsert_task(task)
        metrics.TRANSITIONS_TOTAL.labels(to_state=state.value).inc()
        self._event(task, "task.state", f"{old.value} -> {state.value}: {detail}")
        log.info("task transition", extra={"task": task.id, "from": old.value, "to": state.value, "detail": detail[:200]})
        try:
            await self.gh.set_status_label(task.issue_number, STATUS_LABELS[state], ALL_STATUS_LABELS)
            if state.is_terminal or state == TaskState.SESSION_CREATED:
                await self.gh.remove_label(task.issue_number, self.cfg.trigger_label)
        except RuntimeError as exc:
            log.warning("label update failed for %s: %s", task.id, exc)

    async def _comment(self, task: Task, body: str) -> None:
        try:
            await self.gh.comment(task.issue_number, body)
            self._event(task, "github.comment", body[:120])
        except RuntimeError as exc:
            log.warning("comment failed for %s: %s", task.id, exc)

    def _event(self, task: Task, kind: str, detail: str = "") -> None:
        self.store.add_event(Event(task_id=task.id, kind=kind, detail=detail))

    @staticmethod
    def _pr_comment(task: Task, out: dict[str, Any]) -> str:
        files = out.get("files_changed") or []
        lines = [f"🔀 **Pull request opened:** {task.pr_url}", ""]
        if out.get("summary"):
            lines += [out["summary"], ""]
        if out.get("verification"):
            lines += [f"**Verification:** {out['verification']}", ""]
        if files:
            lines += ["**Files changed:**"] + [f"- `{f}`" for f in files[:20]] + [""]
        lines.append(f"**Risk:** {out.get('risk', 'unknown')} · **ACUs:** {task.acus_consumed:.2f} · **Session:** {task.session_url}")
        if out.get("follow_ups"):
            lines += ["", "**Follow-ups for a human:**"] + [f"- {f}" for f in out["follow_ups"]]
        return "\n".join(lines)
