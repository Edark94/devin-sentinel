"""The v1 code path: a key that /v3/self rejects must still drive the whole pipeline."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from fake_devin.app import app as fake_devin_app
from sentinel.devin_client import DevinClient, SessionView
from sentinel.models import TaskState
from tests.conftest import issue

v1_only = FastAPI()
v1_only.mount("/", fake_devin_app)


@v1_only.middleware("http")
async def reject_v3(request, call_next):
    if request.url.path.startswith("/v3"):
        return JSONResponse({"title": "Forbidden", "status": 403, "detail": "Unauthorized"}, status_code=403)
    return await call_next(request)


def v1_client() -> DevinClient:
    c = DevinClient("fake", "http://fake-devin", api_version="auto")
    c._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=v1_only), base_url="http://fake-devin", headers={"Authorization": "Bearer fake"})
    return c


def test_v1_status_normalisation():
    v = SessionView.from_v1_json({"session_id": "devin-abc", "status": "blocked", "status_enum": "blocked", "pull_request": None})
    assert v.is_waiting_for_user and not v.is_terminal and v.url.endswith("/sessions/abc")
    v = SessionView.from_v1_json({"session_id": "devin-abc", "status": "finished", "status_enum": "finished", "pull_request": {"url": "https://x/pull/1"}})
    assert v.is_finished and v.first_pr_url == "https://x/pull/1"
    v = SessionView.from_v1_json({"session_id": "devin-abc", "status": "RUNNING", "status_enum": None})
    assert v.status == "running" and v.status_detail == "working"
    v = SessionView.from_v1_json({"session_id": "devin-abc", "status": "expired", "status_enum": "expired"})
    assert v.is_terminal and not v.is_error


async def test_auto_falls_back_to_v1_and_runs_end_to_end(orchestrator, store, github):
    client = v1_client()
    orchestrator.devin = client
    assert await client.version() == "v1"
    task = await orchestrator.accept_issue(issue(21, title="v1 path [sim:block]", body="[sim:block]"), trigger="manual")
    for _ in range(8):
        await orchestrator.tick()
        await asyncio.sleep(0.25)
    t = store.get_task(task.id)
    assert t.state == TaskState.PR_OPENED, (t.state, t.last_error)
    assert t.nudges >= 1 and t.session_url.startswith("https://app.devin.ai/sessions/")
    assert t.acus_consumed == 0.0  # not available on v1
    await client.aclose()
