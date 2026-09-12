"""Markdown status report — what an engineering leader reads on Monday morning.

    python -m sentinel.observability.report            # prints to stdout
    python -m sentinel.observability.report --post 42  # posts as a comment on issue #42
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from ..config import load_settings
from ..github_client import GitHubClient
from ..models import TaskState
from ..store import Store
from .metrics import summary


def render(store: Store) -> str:
    s = summary(store)
    tasks = store.list_tasks(limit=200)
    lines = [
        f"## Devin remediation report — {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Tasks accepted | {s['tasks_total']} |",
        f"| Active now | {s['active']} |",
        f"| PRs opened | {s['prs_opened']} |",
        f"| PRs merged | {s['prs_merged']} |",
        f"| Needs human | {s['needs_human']} |",
        f"| Failed | {s['failed']} |",
        f"| Success rate (terminal tasks) | {_pct(s['success_rate'])} |",
        f"| Median time to PR | {s['median_time_to_pr_minutes'] or '-'} min |",
        f"| ACUs consumed | {s['acus_total']} (≈ {s['acus_per_pr'] or '-'} per PR) |",
        "",
        "### By category",
        "",
        "| Category | Tasks | PRs | Failed | Needs human |",
        "|---|---|---|---|---|",
    ]
    for cat, c in sorted(s["by_category"].items()):
        lines.append(f"| {cat} | {c['total']} | {c['prs']} | {c['failed']} | {c['needs_human']} |")
    lines += ["", "### Tasks", "", "| Issue | State | PR | ACUs | Duration | Session |", "|---|---|---|---|---|---|"]
    for t in tasks:
        dur = f"{t.duration_seconds / 60:.0f} min" if t.duration_seconds else "-"
        pr = f"[PR]({t.pr_url})" if t.pr_url else "-"
        sess = f"[session]({t.session_url})" if t.session_url else "-"
        lines.append(f"| [#{t.issue_number}]({t.issue_url}) {t.issue_title[:60]} | `{t.state.value}` | {pr} | {t.acus_consumed:.2f} | {dur} | {sess} |")
    stuck = [t for t in tasks if t.state in (TaskState.NEEDS_HUMAN, TaskState.FAILED)]
    if stuck:
        lines += ["", "### Needs attention", ""]
        for t in stuck:
            lines.append(f"- #{t.issue_number} `{t.state.value}`: {t.last_error or (t.structured_output or {}).get('summary', '')}")
    return "\n".join(lines)


def _pct(v: float | None) -> str:
    return f"{v * 100:.0f}%" if v is not None else "-"


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", type=int, metavar="ISSUE", help="post the report as a comment on this issue number")
    args = ap.parse_args()
    cfg = load_settings()
    md = render(Store(cfg.db_path))
    print(md)
    if args.post:
        gh = GitHubClient(cfg.github_token, cfg.github_repo, cfg.github_api_base, cfg.github_dry_run)
        await gh.comment(args.post, md)
        await gh.aclose()


if __name__ == "__main__":
    asyncio.run(_main())
