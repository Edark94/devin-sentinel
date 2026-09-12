"""Minimal async GitHub REST client: issues, labels, comments, PR state.

All write operations honour `dry_run` so the whole pipeline can be exercised without touching the repo.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("sentinel.github")


class GitHubClient:
    def __init__(self, token: str, repo: str, base_url: str = "https://api.github.com", dry_run: bool = False):
        self.repo = repo
        self.dry_run = dry_run
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, timeout=30.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        r = await self._client.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"GitHub {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    # ---- reads ---------------------------------------------------------
    async def get_issue(self, number: int) -> dict[str, Any]:
        return await self._req("GET", f"/repos/{self.repo}/issues/{number}")

    async def list_issues_with_label(self, label: str, state: str = "open") -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = await self._req(
                "GET", f"/repos/{self.repo}/issues", params={"labels": label, "state": state, "per_page": 100, "page": page}
            )
            issues += [i for i in batch if "pull_request" not in i]
            if len(batch) < 100:
                return issues
            page += 1

    async def get_pull(self, number: int) -> dict[str, Any]:
        return await self._req("GET", f"/repos/{self.repo}/pulls/{number}")

    async def pr_state_from_url(self, pr_url: str) -> str | None:
        """Returns 'open' | 'merged' | 'closed' | None."""
        try:
            number = int(pr_url.rstrip("/").split("/pull/")[1].split("/")[0])
            repo = "/".join(pr_url.split("github.com/")[1].split("/")[:2])
        except (IndexError, ValueError):
            return None
        try:
            pr = await self._req("GET", f"/repos/{repo}/pulls/{number}")
        except RuntimeError as exc:
            log.warning("could not read PR %s: %s", pr_url, exc)
            return None
        if pr.get("merged_at"):
            return "merged"
        return pr.get("state")

    # ---- writes --------------------------------------------------------
    async def comment(self, number: int, body: str) -> None:
        if self.dry_run:
            log.info("[dry-run] comment on #%s:\n%s", number, body)
            return
        await self._req("POST", f"/repos/{self.repo}/issues/{number}/comments", json={"body": body})

    async def add_labels(self, number: int, labels: list[str]) -> None:
        if self.dry_run:
            log.info("[dry-run] add labels %s to #%s", labels, number)
            return
        await self._req("POST", f"/repos/{self.repo}/issues/{number}/labels", json={"labels": labels})

    async def remove_label(self, number: int, label: str) -> None:
        if self.dry_run:
            log.info("[dry-run] remove label %s from #%s", label, number)
            return
        r = await self._client.delete(f"/repos/{self.repo}/issues/{number}/labels/{label}")
        if r.status_code not in (200, 404):
            raise RuntimeError(f"GitHub remove label -> {r.status_code}: {r.text[:200]}")

    async def set_status_label(self, number: int, status_label: str, all_status_labels: list[str]) -> None:
        for lbl in all_status_labels:
            if lbl != status_label:
                await self.remove_label(number, lbl)
        await self.add_labels(number, [status_label])

    async def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        if self.dry_run:
            log.info("[dry-run] create issue %r labels=%s", title, labels)
            return {"number": 0, "html_url": "dry-run", "title": title}
        return await self._req("POST", f"/repos/{self.repo}/issues", json={"title": title, "body": body, "labels": labels})

    async def ensure_labels(self, labels: dict[str, tuple[str, str]]) -> None:
        """labels: name -> (color, description). Idempotent."""
        for name, (color, desc) in labels.items():
            if self.dry_run:
                continue
            r = await self._client.get(f"/repos/{self.repo}/labels/{name}")
            if r.status_code == 200:
                continue
            await self._req("POST", f"/repos/{self.repo}/labels", json={"name": name, "color": color, "description": desc})
