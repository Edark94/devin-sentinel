"""Prompt construction and the structured-output contract we ask Devin to honour.

The prompt is the interface between the orchestrator and the agent. It has to carry:
  * the task (the issue, verbatim),
  * the guard-rails (which repo/branch, never touch upstream, budget awareness),
  * the definition of done (PR + structured summary), and
  * the autonomy policy (decide, document, don't wait for humans).
"""

from __future__ import annotations

from typing import Any

from .models import Task

STRUCTURED_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "additionalProperties": False,
    "required": ["outcome", "summary", "verification", "risk"],
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["pr_opened", "no_change_needed", "blocked"],
            "description": "pr_opened: a PR was pushed and opened. no_change_needed: the issue is already fixed / not applicable (explain). blocked: could not complete (explain what a human must do).",
        },
        "pr_url": {"type": ["string", "null"], "description": "Full URL of the pull request, if opened."},
        "branch": {"type": ["string", "null"]},
        "summary": {"type": "string", "description": "2-6 sentences a reviewer can read instead of the diff."},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "verification": {"type": "string", "description": "Exactly what was run to verify (commands + result)."},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "follow_ups": {"type": "array", "items": {"type": "string"}, "description": "Anything a human should still do."},
    },
}

CATEGORY_GUIDANCE = {
    "security": (
        "This is a security finding. Prefer the smallest upgrade that reaches the first patched version. "
        "Do not upgrade unrelated packages. If the pinned file is generated (e.g. pip-compile output), edit the "
        "pin directly AND note in the PR that the source constraint may need a follow-up."
    ),
    "dependencies": (
        "This is a dependency change. Keep the diff limited to the manifest/lockfile and any code strictly required "
        "by the new version. Run the narrowest test subset that proves the upgrade does not break imports/CLI."
    ),
    "code-quality": (
        "This is a code-quality change. Keep behaviour identical; add or adjust a test only if it protects the change. "
        "Run the project's linter (ruff) on the touched files and the unit tests that cover them."
    ),
    "frontend": (
        "This touches superset-frontend. Use the Node version from superset-frontend/.nvmrc and npm ci. "
        "Regenerate package-lock.json only for the affected packages (npm audit fix / targeted npm update), not a full refresh."
    ),
}


def build_prompt(task: Task, *, repo: str, default_branch: str, max_acu: int) -> str:
    branch = f"devin/issue-{task.issue_number}-{slugify(task.issue_title)}"
    guidance = "\n".join(f"- {CATEGORY_GUIDANCE[c]}" for c in _categories(task) if c in CATEGORY_GUIDANCE)
    return f"""You are remediating a GitHub issue in the repository https://github.com/{repo} (a fork of apache/superset).

## Hard rules
1. Work ONLY in https://github.com/{repo}. Never open a pull request against apache/superset or any other upstream repository.
2. Create your branch from `{default_branch}` and name it `{branch}`.
3. Open exactly one pull request against `{repo}:{default_branch}`. Put `Fixes #{task.issue_number}` in the PR body and link this issue.
4. Keep the change minimal and reviewable. Do not refactor unrelated code, do not bump unrelated dependencies, do not reformat files you did not need to touch.
5. You have a budget of about {max_acu} ACUs. If you are not converging, stop, push what you have as a draft PR, and report `outcome: blocked` with a precise description of what remains.
6. Do not wait for a human. When a decision is ambiguous, pick the safest reasonable option, state the assumption in the PR description, and continue.

## Task-specific guidance
{guidance or "- Follow the issue text; keep the diff scoped to what it asks for."}

## The issue (#{task.issue_number}): {task.issue_title}
{task.issue_url}

{task.issue_body.strip() or "(no body)"}

## Definition of done
- Branch pushed, PR opened against `{default_branch}` with a clear description: what changed, why, how it was verified.
- CI-relevant checks you can run locally were run (at minimum: the linter on touched files and the narrowest relevant tests).
- When finished, call provide_structured_output with the schema you were given: outcome, pr_url, branch, summary, files_changed, verification, risk, follow_ups.
"""


def nudge_message(task: Task, nudge_number: int, max_nudges: int) -> str:
    return (
        f"(automated message {nudge_number}/{max_nudges}) No human is monitoring this session. "
        "You have full autonomy on this task: choose the safest reasonable option, document the assumption "
        "in the PR description, and continue. If you are genuinely unable to proceed, open a draft PR with "
        "what you have and finish with outcome `blocked` plus a precise description of what a human must do."
    )


def _categories(task: Task) -> list[str]:
    cats = [task.category]
    for lbl in task.labels:
        if lbl in CATEGORY_GUIDANCE and lbl not in cats:
            cats.append(lbl)
    return cats


def slugify(text: str, max_len: int = 40) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in text)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:max_len].rstrip("-") or "task"
