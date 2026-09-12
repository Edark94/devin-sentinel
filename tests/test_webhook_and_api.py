from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from sentinel.main import build_app
from sentinel.triggers.webhook import verify_signature
from tests.conftest import issue


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_verification():
    body = b'{"a":1}'
    assert verify_signature("k", body, sign("k", body))
    assert not verify_signature("k", body, sign("other", body))
    assert not verify_signature("k", body, None)
    assert verify_signature("", body, None)  # unconfigured secret -> accepted (local only)


async def test_webhook_end_to_end(settings, devin, github, monkeypatch):
    app = build_app(settings, run_background=False)
    app.state.orchestrator.devin = devin
    app.state.orchestrator.gh = github
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
        app.router.lifespan_context(app),
    ):
        payload = {"action": "labeled", "label": {"name": "devin:remediate"}, "issue": issue(11)}
        body = json.dumps(payload).encode()
        headers = {"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d-1", "X-Hub-Signature-256": sign("s3cret", body)}
        r = await c.post("/webhooks/github", content=body, headers=headers)
        assert r.status_code == 200 and r.json()["status"] == "accepted"
        # duplicate delivery is ignored
        r = await c.post("/webhooks/github", content=body, headers=headers)
        assert r.json()["status"] == "duplicate"
        # bad signature rejected
        r = await c.post("/webhooks/github", content=body, headers={**headers, "X-GitHub-Delivery": "d-2", "X-Hub-Signature-256": "sha256=00"})
        assert r.status_code == 401
        # unrelated label ignored
        other = json.dumps({"action": "labeled", "label": {"name": "bug"}, "issue": issue(12, labels=("bug",))}).encode()
        r = await c.post("/webhooks/github", content=other, headers={**headers, "X-GitHub-Delivery": "d-3", "X-Hub-Signature-256": sign("s3cret", other)})
        assert r.json()["status"] == "ignored"

        r = await c.post("/api/tick")
        assert r.status_code == 200
        tasks = (await c.get("/api/tasks")).json()
        assert len(tasks) == 1 and tasks[0]["session_id"]
        summary = (await c.get("/api/summary")).json()
        assert summary["tasks_total"] == 1 and summary["active"] == 1
        assert (await c.get("/metrics")).status_code == 200
        assert "sentinel_devin_sessions_created_total" in (await c.get("/metrics")).text
        html = (await c.get("/")).text
        assert "#11" in html and "devin-sentinel" in html
        assert "Devin remediation report" in (await c.get("/report")).text
