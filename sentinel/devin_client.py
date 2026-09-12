"""Thin async client for the Devin v3 organization API.

Only the endpoints the orchestrator needs. Reference: https://docs.devin.ai/api-reference/overview
(spec: https://docs.devin.ai/v3-openapi.yaml). Authentication is a bearer token; the org id is
resolved once from GET /v3/self when not configured.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("sentinel.devin")

# v3 session `status` values that mean the VM is no longer doing work.
TERMINAL_STATUSES = {"exit", "error", "suspended"}


class DevinAPIError(RuntimeError):
    def __init__(self, status: int, body: Any):
        super().__init__(f"Devin API {status}: {body}")
        self.status = status
        self.body = body


@dataclass
class SessionView:
    """Normalised view over the v3 SessionResponse."""

    session_id: str
    url: str
    status: str                       # new | claimed | running | exit | error | suspended | resuming
    status_detail: str | None         # working | waiting_for_user | waiting_for_approval | finished | <suspend reason>
    acus_consumed: float
    pull_requests: list[dict[str, Any]] = field(default_factory=list)
    structured_output: dict[str, Any] | None = None
    tags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def is_waiting_for_user(self) -> bool:
        return self.status == "running" and self.status_detail in {"waiting_for_user", "waiting_for_approval"}

    @property
    def is_finished(self) -> bool:
        """Devin declared the task complete (VM may still be up)."""
        return (self.status == "running" and self.status_detail == "finished") or self.status == "exit"

    @property
    def is_error(self) -> bool:
        return self.status == "error" or (self.status == "suspended" and self.status_detail not in {None, "inactivity", "user_request", "finished"})

    @property
    def first_pr_url(self) -> str | None:
        for pr in self.pull_requests:
            if pr.get("pr_url"):
                return pr["pr_url"]
        return None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SessionView:
        return cls(
            session_id=d["session_id"],
            url=d.get("url") or "",
            status=d.get("status") or "new",
            status_detail=d.get("status_detail"),
            acus_consumed=float(d.get("acus_consumed") or 0.0),
            pull_requests=list(d.get("pull_requests") or []),
            structured_output=d.get("structured_output"),
            tags=list(d.get("tags") or []),
            raw=d,
        )


class DevinClient:
    def __init__(self, api_key: str, base_url: str = "https://api.devin.ai", org_id: str = "", timeout: float = 30.0):
        self._org_id = org_id
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- low level -----------------------------------------------------
    async def _request(self, method: str, path: str, retries: int = 3, **kw: Any) -> Any:
        delay = 1.0
        for attempt in range(retries + 1):
            try:
                r = await self._client.request(method, path, **kw)
            except httpx.TransportError as exc:
                if attempt == retries:
                    raise DevinAPIError(0, f"transport error: {exc}") from exc
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                retry_after = float(r.headers.get("Retry-After", delay))
                log.warning("devin api %s %s -> %s, retrying in %.1fs", method, path, r.status_code, retry_after)
                await asyncio.sleep(retry_after)
                delay *= 2
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                except ValueError:
                    body = r.text
                raise DevinAPIError(r.status_code, body)
            if r.status_code == 204 or not r.content:
                return None
            return r.json()
        raise DevinAPIError(0, "unreachable")

    async def org_id(self) -> str:
        if not self._org_id:
            me = await self._request("GET", "/v3/self")
            self._org_id = me.get("org_id") or ""
            if not self._org_id:
                raise DevinAPIError(0, f"could not resolve org id from /v3/self: {me}")
            log.info("resolved devin org id %s (%s)", self._org_id, me.get("principal_type"))
        return self._org_id

    # ---- sessions ------------------------------------------------------
    async def create_session(
        self,
        prompt: str,
        *,
        title: str,
        tags: list[str],
        max_acu_limit: int | None,
        structured_output_schema: dict[str, Any] | None = None,
        repos: list[str] | None = None,
        devin_mode: str | None = None,
        idempotent: bool = True,
    ) -> SessionView:
        org = await self.org_id()
        body: dict[str, Any] = {
            "prompt": prompt,
            "title": title,
            "tags": tags,
            "unlisted": False,
        }
        if max_acu_limit:
            body["max_acu_limit"] = max_acu_limit
        if structured_output_schema:
            body["structured_output_schema"] = structured_output_schema
            body["structured_output_required"] = True
        if repos:
            body["repos"] = repos
        if devin_mode:
            body["devin_mode"] = devin_mode
        if idempotent:
            body["idempotent"] = True
        try:
            data = await self._request("POST", f"/v3/organizations/{org}/sessions", json=body)
        except DevinAPIError as exc:
            # Defensive: `repos` is an undocumented-shape field in the v3 spec. If the API rejects it,
            # fall back to relying on the repo URL in the prompt rather than failing the task.
            if exc.status == 422 and "repos" in body and "repos" in str(exc.body):
                log.warning("create_session rejected `repos`=%s, retrying without it: %s", body["repos"], exc.body)
                body.pop("repos")
                data = await self._request("POST", f"/v3/organizations/{org}/sessions", json=body)
            else:
                raise
        return SessionView.from_json(data)

    async def get_session(self, session_id: str) -> SessionView:
        org = await self.org_id()
        data = await self._request("GET", f"/v3/organizations/{org}/sessions/{session_id}")
        return SessionView.from_json(data)

    async def list_sessions(self, tags: list[str] | None = None, first: int = 100) -> list[SessionView]:
        org = await self.org_id()
        params: dict[str, Any] = {"first": first}
        if tags:
            params["tags"] = tags
        data = await self._request("GET", f"/v3/organizations/{org}/sessions", params=params)
        return [SessionView.from_json(x) for x in data.get("items", [])]

    async def send_message(self, session_id: str, message: str) -> None:
        org = await self.org_id()
        await self._request("POST", f"/v3/organizations/{org}/sessions/{session_id}/messages", json={"message": message})

    async def list_messages(self, session_id: str, first: int = 50) -> list[dict[str, Any]]:
        org = await self.org_id()
        data = await self._request("GET", f"/v3/organizations/{org}/sessions/{session_id}/messages", params={"first": first})
        return list(data.get("items", []))

    async def terminate_session(self, session_id: str) -> None:
        org = await self.org_id()
        try:
            await self._request("DELETE", f"/v3/organizations/{org}/sessions/{session_id}")
        except DevinAPIError as exc:
            # already exited -> fine
            if exc.status not in (404, 409):
                raise

    async def set_tags(self, session_id: str, tags: list[str]) -> None:
        org = await self.org_id()
        await self._request("PUT", f"/v3/organizations/{org}/sessions/{session_id}/tags", json={"tags": tags})
