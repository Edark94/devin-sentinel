"""Scheduled trigger: periodically sweep the repo for labelled issues the webhook may have missed.

Webhooks are at-least-once and only fire while the service is reachable. A sweep makes the system
self-healing: anything carrying the trigger label ends up as a task, no matter how it got the label.
"""

from __future__ import annotations

import logging

from ..github_client import GitHubClient
from ..orchestrator import Orchestrator

log = logging.getLogger("sentinel.sweep")


async def sweep_once(gh: GitHubClient, orchestrator: Orchestrator, trigger_label: str) -> int:
    issues = await gh.list_issues_with_label(trigger_label)
    accepted = 0
    for issue in issues:
        task = await orchestrator.accept_issue(issue, trigger="sweep")
        accepted += 1 if task else 0
    log.info("sweep: %d labelled issue(s), %d newly accepted", len(issues), accepted)
    return accepted
