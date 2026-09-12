"""GitHub webhook handling: signature verification + event -> orchestrator dispatch."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from ..models import utcnow
from ..observability import metrics
from ..orchestrator import Orchestrator
from ..store import Store

log = logging.getLogger("sentinel.webhook")


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """X-Hub-Signature-256: 'sha256=<hex hmac of raw body>'. Constant-time comparison."""
    if not secret:
        return True  # explicitly unconfigured (local simulation); log loudly at startup instead
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.split("=", 1)[1])


async def handle_event(
    event: str, delivery_id: str, payload: dict[str, Any], *, orchestrator: Orchestrator, store: Store, trigger_label: str
) -> dict[str, Any]:
    if delivery_id and not store.record_delivery(delivery_id, utcnow()):
        metrics.WEBHOOKS_TOTAL.labels(event=event, result="duplicate").inc()
        return {"status": "duplicate", "delivery": delivery_id}

    if event == "ping":
        metrics.WEBHOOKS_TOTAL.labels(event=event, result="ok").inc()
        return {"status": "pong"}

    if event == "issues":
        action = payload.get("action")
        issue = payload.get("issue") or {}
        labels = {lbl["name"] for lbl in issue.get("labels", [])}
        triggered = (action == "labeled" and (payload.get("label") or {}).get("name") == trigger_label) or (
            action in ("opened", "reopened") and trigger_label in labels
        )
        if not triggered:
            metrics.WEBHOOKS_TOTAL.labels(event=event, result="ignored").inc()
            return {"status": "ignored", "reason": f"issues.{action} without trigger label"}
        task = await orchestrator.accept_issue(issue, trigger="webhook")
        metrics.WEBHOOKS_TOTAL.labels(event=event, result="accepted" if task else "deduped").inc()
        return {"status": "accepted" if task else "already_tracked", "task_id": task.id if task else None}

    if event == "pull_request":
        await orchestrator.on_pull_request_event(payload)
        metrics.WEBHOOKS_TOTAL.labels(event=event, result="ok").inc()
        return {"status": "ok"}

    metrics.WEBHOOKS_TOTAL.labels(event=event, result="ignored").inc()
    return {"status": "ignored", "reason": f"unhandled event {event}"}
