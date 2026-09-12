"""A small stand-in for the Devin v3 API so the whole pipeline can be exercised without ACUs.

Implements the subset devin-sentinel uses:
  GET  /v3/self
  POST /v3/organizations/{org}/sessions
  GET  /v3/organizations/{org}/sessions
  GET  /v3/organizations/{org}/sessions/{id}
  POST /v3/organizations/{org}/sessions/{id}/messages
  DELETE /v3/organizations/{org}/sessions/{id}
  PUT  /v3/organizations/{org}/sessions/{id}/tags

Sessions advance on wall-clock time. Behaviour is scripted from the prompt so the demo can show
every branch of the orchestrator:
  * default                     -> working ~20s, then finished with a PR + structured output
  * prompt contains "[sim:block]" -> asks for input; each message from the orchestrator unblocks it
  * prompt contains "[sim:fail]"  -> errors out on the first attempt, succeeds on the retry
  * prompt contains "[sim:nochange]" -> finishes with outcome no_change_needed and no PR
Timing is controlled by FAKE_DEVIN_SPEED (seconds per phase, default 20).
"""

from __future__ import annotations

import os
import re
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException

SPEED = float(os.environ.get("FAKE_DEVIN_SPEED", "20"))
ORG = "org-fake123"
app = FastAPI(title="fake-devin")
_sessions: dict[str, dict[str, Any]] = {}
_attempts: dict[str, int] = {}  # issue key -> attempts, to script "fail once"


def _issue_key(prompt: str) -> str:
    m = re.search(r"issue \(#(\d+)\)", prompt)
    return m.group(1) if m else "0"


def _view(s: dict[str, Any]) -> dict[str, Any]:
    elapsed = time.time() - s["created"]
    phase = int(elapsed // SPEED)
    mode = s["mode"]
    status, detail, prs, out = "running", "working", [], None

    if mode == "fail" and s["attempt"] == 1:
        if phase >= 1:
            status, detail = "error", "error"
    elif mode == "block":
        # phase 1: waiting_for_user until a message arrives, then continue
        if phase >= 1 and s["messages"] == 0:
            detail = "waiting_for_user"
        elif phase >= 2 + s["messages"]:
            status, detail = "exit", "finished"
    elif mode == "nochange":
        if phase >= 1:
            status, detail = "exit", "finished"
    else:
        if phase >= 2:
            status, detail = "exit", "finished"

    if detail == "finished":
        n = _issue_key(s["prompt"])
        if mode == "nochange":
            out = {"outcome": "no_change_needed", "pr_url": None, "branch": None,
                   "summary": "The pinned version already satisfies the advisory; nothing to change.",
                   "files_changed": [], "verification": "pip-audit -r requirements/base.txt reports no findings", "risk": "low", "follow_ups": []}
        else:
            pr_url = f"https://github.com/{s['repo']}/pull/{9000 + int(n)}"
            prs = [{"pr_url": pr_url, "pr_state": "open"}]
            out = {"outcome": "pr_opened", "pr_url": pr_url, "branch": f"devin/issue-{n}-sim",
                   "summary": "Bumped the vulnerable pin to the first patched release and re-ran the relevant checks.",
                   "files_changed": ["requirements/development.txt"], "verification": "pip-audit clean; pytest tests/unit_tests/utils -q passed",
                   "risk": "low", "follow_ups": []}
    acus = round(min(elapsed / SPEED, 3) * 0.7 + (0.3 if detail == "finished" else 0), 2)
    return {
        "session_id": s["id"], "url": f"https://app.devin.ai/sessions/{s['id'].split('-',1)[1]}", "status": status, "status_detail": detail,
        "acus_consumed": acus, "pull_requests": prs, "structured_output": out, "tags": s["tags"], "org_id": ORG,
        "created_at": int(s["created"]), "updated_at": int(time.time()), "title": s["title"], "origin": "api",
    }


@app.get("/v3/self")
def self_():
    return {"principal_type": "pat_user", "user_id": "user-fake", "user_name": "fake", "api_key_id": "k", "api_key_name": "fake", "org_id": ORG}


@app.post("/v3/organizations/{org}/sessions")
def create(org: str, body: dict[str, Any]):
    prompt = body["prompt"]
    key = _issue_key(prompt)
    _attempts[key] = _attempts.get(key, 0) + 1
    mode = next((m for m in ("block", "fail", "nochange") if f"[sim:{m}]" in prompt), "ok")
    repo_m = re.search(r"https://github\.com/([\w.-]+/[\w.-]+)", prompt)
    sid = f"devin-{uuid.uuid4().hex[:12]}"
    _sessions[sid] = {"id": sid, "prompt": prompt, "mode": mode, "created": time.time(), "messages": 0, "attempt": _attempts[key],
                      "tags": body.get("tags", []), "title": body.get("title"), "repo": repo_m.group(1) if repo_m else "example/repo", "terminated": False}
    return _view(_sessions[sid])


@app.get("/v3/organizations/{org}/sessions")
def list_(org: str):
    return {"items": [_view(s) for s in _sessions.values()], "has_next_page": False}


@app.get("/v3/organizations/{org}/sessions/{sid}")
def get(org: str, sid: str):
    s = _sessions.get(sid)
    if not s:
        raise HTTPException(404, "no such session")
    if s["terminated"]:
        v = _view(s)
        v.update(status="suspended", status_detail="user_request")
        return v
    return _view(s)


@app.post("/v3/organizations/{org}/sessions/{sid}/messages")
def message(org: str, sid: str, body: dict[str, Any]):
    s = _sessions.get(sid)
    if not s:
        raise HTTPException(404, "no such session")
    s["messages"] += 1
    s["created"] = time.time() - SPEED * (1 + s["messages"])  # resume the clock past the block phase
    return _view(s)


@app.delete("/v3/organizations/{org}/sessions/{sid}")
def terminate(org: str, sid: str):
    s = _sessions.get(sid)
    if not s:
        raise HTTPException(404, "no such session")
    s["terminated"] = True
    return _view(s)


@app.put("/v3/organizations/{org}/sessions/{sid}/tags")
def tags(org: str, sid: str, body: dict[str, Any]):
    _sessions[sid]["tags"] = body.get("tags", [])
    return {"tags": _sessions[sid]["tags"]}


# ---- v1 surface (what personal API keys can use today) ---------------------------------------
V3_TO_V1_ENUM = {("running", "working"): "working", ("running", "waiting_for_user"): "blocked", ("exit", "finished"): "finished",
                 ("running", "finished"): "finished", ("error", "error"): "expired", ("suspended", "user_request"): "expired"}


def _v1(s: dict[str, Any]) -> dict[str, Any]:
    v = _view(s)
    if s["terminated"]:
        v.update(status="suspended", status_detail="user_request")
    enum = V3_TO_V1_ENUM.get((v["status"], v["status_detail"]), "working")
    pr = v["pull_requests"][0]["pr_url"] if v["pull_requests"] else None
    return {"session_id": v["session_id"], "status": enum.upper() if enum == "working" else enum, "status_enum": enum, "title": v["title"],
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z", "tags": v["tags"],
            "pull_request": {"url": pr} if pr else None, "structured_output": v["structured_output"], "messages": []}


@app.post("/v1/sessions")
def v1_create(body: dict[str, Any]):
    v = create(ORG, body)
    return {"session_id": v["session_id"], "url": v["url"], "is_new_session": True}


@app.get("/v1/sessions")
def v1_list():
    return {"sessions": [_v1(s) for s in _sessions.values()]}


@app.get("/v1/sessions/{sid}")
def v1_get(sid: str):
    if sid not in _sessions:
        raise HTTPException(404, "Session not found")
    return _v1(_sessions[sid])


@app.post("/v1/sessions/{sid}/message")
def v1_message(sid: str, body: dict[str, Any]):
    message(ORG, sid, body)


@app.delete("/v1/sessions/{sid}")
def v1_terminate(sid: str):
    terminate(ORG, sid)
    return {"status": "ok"}


@app.put("/v1/sessions/{sid}/tags")
def v1_tags(sid: str, body: dict[str, Any]):
    return tags(ORG, sid, body)


@app.get("/healthz")
def healthz():
    return {"ok": True, "sessions": len(_sessions)}
