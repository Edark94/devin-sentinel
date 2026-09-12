from __future__ import annotations

from sentinel.devin_client import SessionView
from sentinel.triggers.triage import (
    TRIAGE_OUTPUT_SCHEMA,
    build_triage_prompt,
    render_issue,
    run_triage,
)


class TriageDevin:
    def __init__(self, views):
        self.views = list(views)
        self.messages = []
        self.created = None

    async def create_session(self, prompt, **kw):
        self.created = (prompt, kw)
        return SessionView(session_id="s-triage", url="https://app.devin.ai/sessions/triage", status="new", status_detail=None, acus_consumed=0)

    async def get_session(self, sid):
        return self.views.pop(0) if len(self.views) > 1 else self.views[0]

    async def send_message(self, sid, msg):
        self.messages.append(msg)


FINDING = {
    "title": "SQL parsing helper silently swallows ParseError", "category": "code-quality", "files": ["superset/sql/parse.py"],
    "problem": "Errors are swallowed.", "evidence": "superset/sql/parse.py:120 `except Exception: pass`",
    "remediation": "Re-raise as SupersetParseError.", "verification": "pytest tests/unit_tests/sql/parse_tests.py -q", "confidence": "high",
}


def test_prompt_and_schema():
    p = build_triage_prompt("Edark94/superset", "master", "superset/sql", 3, 5)
    assert "Read-only" in p and "superset/sql" in p and "up to 3" in p and "5 ACUs" in p
    assert TRIAGE_OUTPUT_SCHEMA["properties"]["findings"]["items"]["required"][0] == "title"


async def test_triage_waits_nudges_once_and_files_issues(settings, github, store):
    idle = SessionView(session_id="s-triage", url="u", status="running", status_detail="waiting_for_user", acus_consumed=1)
    done = SessionView(session_id="s-triage", url="u", status="running", status_detail="waiting_for_user", acus_consumed=2,
                       structured_output={"summary": "looked at superset/sql", "findings": [FINDING, {**FINDING, "title": "[security] second", "category": "security"}]})
    devin = TriageDevin([idle, done])
    filed_titles = []

    async def create_issue(title, body, labels):
        filed_titles.append((title, labels, body))
        return {"number": len(filed_titles) + 20, "html_url": "https://github.com/Edark94/superset/issues/x"}

    github.create_issue = create_issue
    filed = await run_triage(settings, devin, github, store, area="superset/sql", max_issues=3, auto=False, max_acu=5, dry_run=False, timeout_s=5, poll_s=0.01)
    assert len(filed) == 2 and devin.messages and "provide_structured_output" in devin.messages[0]
    title, labels, body = filed_titles[0]
    assert title.startswith("[code-quality]") and labels == ["devin:triage", "code-quality"] and "sentinel:triage:s-triage:1" in body
    assert filed_titles[1][0] == "[security] second"
    assert devin.created[1]["structured_output_schema"] is TRIAGE_OUTPUT_SCHEMA and devin.created[1]["max_acu_limit"] == 5
    kinds = [e.kind for e in store.list_events("triage:s-triage")]
    assert "triage.started" in kinds and "triage.finished" in kinds and kinds.count("triage.issue_filed") == 2


async def test_triage_auto_adds_remediation_label(settings, github, store):
    done = SessionView(session_id="s2", url="u", status="exit", status_detail="finished", acus_consumed=2, structured_output={"summary": "", "findings": [FINDING]})
    labels_seen = []

    async def create_issue(title, body, labels):
        labels_seen.append(labels)
        return {"number": 30, "html_url": "u"}

    github.create_issue = create_issue
    await run_triage(settings, TriageDevin([done]), github, store, area=None, max_issues=1, auto=True, max_acu=5, dry_run=False, timeout_s=5, poll_s=0.01)
    assert labels_seen == [["devin:triage", "code-quality", "devin:remediate"]]


def test_render_issue_prefixes_category_when_missing():
    view = SessionView(session_id="s", url="https://app.devin.ai/sessions/s", status="exit", status_detail="finished", acus_consumed=0)
    title, body, _labels = render_issue({**FINDING, "title": "no prefix"}, view, 1, "superset/sql")
    assert title == "[code-quality] no prefix" and "Found by a Devin triage session" in body and "`superset/sql`" in body
