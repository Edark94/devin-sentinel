"""Triage trigger: Devin as the *discovery* primitive, not only the remediation one.

    python -m sentinel.triggers.triage --area "superset/sql" --max-issues 3          # propose issues
    python -m sentinel.triggers.triage --area "superset/utils" --auto                # ...and label them for remediation
    python -m sentinel.triggers.triage --dry-run                                     # print, file nothing

Scanners find what has a CVE id. Devin finds what needs reading: dead branches, unsafe defaults,
copy-pasted helpers, missing tests around a risky function. This module starts one bounded Devin
session with a brief, waits for its structured findings, and files them as GitHub issues through
the same path every other trigger uses. By default the issues carry `devin:triage` and category
labels but NOT the remediation label, so a human approves each one by adding `devin:remediate`.
`--auto` skips that approval step and closes the loop fully.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from typing import Any

from ..config import Settings, load_settings
from ..devin_client import DevinAPIError, DevinClient, SessionView
from ..github_client import GitHubClient
from ..models import Event
from ..store import Store

log = logging.getLogger("sentinel.triage")

CATEGORIES = ("security", "code-quality", "dependencies", "frontend")

TRIAGE_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "additionalProperties": False,
    "required": ["findings", "summary"],
    "properties": {
        "summary": {"type": "string", "description": "What you looked at and how you chose the findings."},
        "findings": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "category", "files", "problem", "evidence", "remediation", "verification", "confidence"],
                "properties": {
                    "title": {"type": "string", "maxLength": 120},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                    "files": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "problem": {"type": "string", "description": "What is wrong and why it matters, 2-5 sentences."},
                    "evidence": {"type": "string", "description": "File:line references and a short code excerpt."},
                    "remediation": {"type": "string", "description": "Concrete steps; scoped to a single small PR."},
                    "verification": {"type": "string", "description": "Commands/tests that prove the fix."},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        },
    },
}


def build_triage_prompt(repo: str, default_branch: str, area: str | None, max_issues: int, max_acu: int) -> str:
    scope = f"Focus on `{area}`." if area else "Choose one or two cohesive areas of the backend (`superset/`) rather than skimming everything."
    return f"""You are triaging the repository https://github.com/{repo} (a fork of apache/superset, branch `{default_branch}`).

## Goal
Find up to {max_issues} concrete, well-scoped engineering issues that an automated scanner would NOT catch and that
an autonomous agent could fix in a single small pull request. {scope}

Good findings:
- a security-relevant default, missing validation, or unsafe construction in real code paths
- dead or unreachable code, duplicated helpers, misleading names or comments that hide behaviour
- a risky function with no unit test, where a focused test is cheap
- a lint/type suppression (`# noqa`, `# type: ignore`, `# pylint: disable`) that hides a real problem
Not wanted: style nits, broad refactors, dependency bumps (a scanner already covers those), anything needing a product decision.

## Hard rules
1. Read-only: do not modify the repository, do not push branches, do not open pull requests or issues.
2. Every finding must cite exact file paths and line numbers you actually read, with a short excerpt.
3. Each finding must be fixable in isolation: one PR, limited to the files you list.
4. Rank by value; drop anything you are not confident is real. Fewer, solid findings beat many weak ones.
5. Budget: about {max_acu} ACUs. Stop early if you have {max_issues} solid findings.

## Output
When done, call provide_structured_output with: summary, findings[] (title, category, files, problem, evidence,
remediation, verification, confidence). Titles should read like good issue titles, e.g.
"[code-quality] SQL parsing helper silently swallows ParseError in superset/sql/parse.py".
"""


def render_issue(f: dict[str, Any], session: SessionView, index: int, area: str | None) -> tuple[str, str, list[str]]:
    """Issue text in the register a colleague would use: the problem, the evidence, what to do, how to check."""
    title = f["title"].strip()
    tag = f"[{f['category']}]"
    if not title.lower().startswith("["):
        title = f"{tag} {title}"
    marker = f"<!-- sentinel:triage:{session.session_id}:{index} -->"
    files = ", ".join(f"`{p}`" for p in f["files"])
    body = f"""{marker}
{f['problem'].strip()}

{_as_block(f['evidence'])}

To fix ({files}): {f['remediation'].strip()}

To check: {f['verification'].strip()}

_Found by a Devin triage session{f' over `{area}`' if area else ''} ({session.url}), confidence {f['confidence']}. Add `devin:remediate` to hand it back to Devin._
"""
    return title, body, ["devin:triage", f["category"]]


def _as_block(evidence: str) -> str:
    """Evidence usually mixes file:line references with code excerpts; keep excerpts fenced, prose as prose."""
    text = evidence.strip()
    if "```" in text:
        return text
    lines = text.splitlines()
    prose = [ln for ln in lines if ln and not ln.startswith((" ", "\t"))]
    code = [ln for ln in lines if ln.startswith((" ", "\t"))]
    if not code:
        return text
    return "\n".join(prose) + "\n\n```python\n" + "\n".join(code) + "\n```"


async def wait_for_session(devin: DevinClient, session_id: str, timeout_s: float, poll_s: float) -> SessionView:
    deadline = time.monotonic() + timeout_s
    while True:
        view = await devin.get_session(session_id)
        if view.structured_output and view.structured_output.get("findings") is not None:
            return view
        if view.is_error or view.is_budget_exhausted:
            raise RuntimeError(f"triage session ended without findings: {view.status}/{view.status_detail}")
        if view.is_terminal or (view.is_waiting_for_user and not view.structured_output):
            # idle without output: ask once for the structured output, then keep waiting
            try:
                await devin.send_message(session_id, "(automated) Please finish by calling provide_structured_output with your findings now.")
            except DevinAPIError:
                pass
        if time.monotonic() > deadline:
            raise TimeoutError(f"triage session {session_id} produced no findings within {timeout_s:.0f}s")
        await asyncio.sleep(poll_s)


async def run_triage(
    cfg: Settings,
    devin: DevinClient,
    gh: GitHubClient,
    store: Store | None = None,
    *,
    area: str | None,
    max_issues: int,
    auto: bool,
    max_acu: int,
    dry_run: bool,
    timeout_s: float = 45 * 60,
    poll_s: float = 30,
) -> list[dict[str, Any]]:
    prompt = build_triage_prompt(cfg.github_repo, cfg.github_default_branch, area, max_issues, max_acu)
    session = await devin.create_session(
        prompt,
        title=f"[sentinel] triage {cfg.repo_name}{' ' + area if area else ''}",
        tags=["sentinel", "triage", cfg.repo_name],
        max_acu_limit=max_acu,
        structured_output_schema=TRIAGE_OUTPUT_SCHEMA,
        repos=[f"https://github.com/{cfg.github_repo}"],
        devin_mode=cfg.devin_mode or None,
    )
    log.info("triage session %s started: %s", session.session_id, session.url)
    if store:
        store.add_event(Event(task_id=f"triage:{session.session_id}", kind="triage.started", detail=f"{session.url} area={area or '*'}"))
    view = await wait_for_session(devin, session.session_id, timeout_s, poll_s)
    findings = list((view.structured_output or {}).get("findings") or [])
    log.info("triage returned %d finding(s): %s", len(findings), (view.structured_output or {}).get("summary", "")[:200])

    filed: list[dict[str, Any]] = []
    existing_markers: set[str] = set()
    if not dry_run:
        for i in await gh.list_issues_with_label("devin:triage"):
            existing_markers.update(line for line in (i.get("body") or "").splitlines() if line.startswith("<!-- sentinel:triage:"))
    for idx, f in enumerate(findings[:max_issues], start=1):
        title, body, labels = render_issue(f, view, idx, area)
        if auto:
            labels.append(cfg.trigger_label)
        if dry_run:
            print(f"\n=== {title}\nlabels: {labels}\n{body}")
            continue
        if body.splitlines()[0] in existing_markers:
            continue
        issue = await gh.create_issue(title, body, labels)
        filed.append({"number": issue.get("number"), "title": title, "url": issue.get("html_url"), "confidence": f["confidence"]})
        log.info("filed issue #%s: %s", issue.get("number"), title)
        if store:
            store.add_event(Event(task_id=f"triage:{session.session_id}", kind="triage.issue_filed", detail=f"#{issue.get('number')} {title}"))
    if store:
        store.add_event(Event(task_id=f"triage:{session.session_id}", kind="triage.finished", detail=f"{len(findings)} finding(s), {len(filed)} issue(s) filed, auto={auto}"))
    return filed


def main() -> None:
    ap = argparse.ArgumentParser(description="Start a Devin triage session and file its findings as issues")
    ap.add_argument("--area", default=None, help="path or subsystem to focus on, e.g. superset/sql")
    ap.add_argument("--max-issues", type=int, default=3)
    ap.add_argument("--max-acu", type=int, default=5)
    ap.add_argument("--auto", action="store_true", help="also add the remediation label so the pipeline picks the issues up immediately")
    ap.add_argument("--dry-run", action="store_true", help="print the issues instead of filing them")
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s %(message)s")
    cfg = load_settings()

    async def _run() -> None:
        devin = DevinClient(cfg.devin_api_key, cfg.devin_api_base, cfg.devin_org_id, api_version=cfg.devin_api_version)
        gh = GitHubClient(cfg.github_token, cfg.github_repo, cfg.github_api_base, cfg.github_dry_run)
        try:
            await gh.ensure_labels({"devin:triage": ("6f42c1", "Proposed by a Devin triage session; add devin:remediate to approve")})
            filed = await run_triage(cfg, devin, gh, Store(cfg.db_path), area=args.area, max_issues=args.max_issues, auto=args.auto,
                                     max_acu=args.max_acu, dry_run=args.dry_run)
            for f in filed:
                print(f"#{f['number']} [{f['confidence']}] {f['title']} {f['url']}")
        finally:
            await devin.aclose()
            await gh.aclose()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
