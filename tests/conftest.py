from __future__ import annotations

import os

import httpx
import pytest

from fake_devin.app import app as fake_devin_app
from sentinel.config import Settings
from sentinel.devin_client import DevinClient
from sentinel.github_client import GitHubClient
from sentinel.orchestrator import Orchestrator
from sentinel.store import Store


class RecordingGitHub(GitHubClient):
    """GitHub client that records writes instead of performing them and serves canned PR state."""

    def __init__(self):
        super().__init__(token="", repo="Edark94/superset", dry_run=True)
        self.comments: list[tuple[int, str]] = []
        self.labels: dict[int, set[str]] = {}
        self.pr_states: dict[str, str] = {}

    async def comment(self, number, body):
        self.comments.append((number, body))

    async def add_labels(self, number, labels):
        self.labels.setdefault(number, set()).update(labels)

    async def remove_label(self, number, label):
        self.labels.setdefault(number, set()).discard(label)

    async def pr_state_from_url(self, pr_url):
        return self.pr_states.get(pr_url, "open")

    async def list_issues_with_label(self, label, state="open"):
        return []


@pytest.fixture
def settings(tmp_path) -> Settings:
    os.environ["FAKE_DEVIN_SPEED"] = "0.2"
    return Settings(
        DEVIN_API_KEY="fake", DEVIN_API_BASE="http://fake-devin", GITHUB_TOKEN="", GITHUB_REPO="Edark94/superset",
        GITHUB_WEBHOOK_SECRET="s3cret", GITHUB_DRY_RUN=True, SENTINEL_DB_PATH=str(tmp_path / "t.db"),
        MAX_CONCURRENT_SESSIONS=2, MAX_RETRIES=1, MAX_NUDGES=2, SESSION_TIMEOUT_MINUTES=90, ISSUE_SWEEP_INTERVAL_SECONDS=0,
    )


@pytest.fixture
async def devin(settings) -> DevinClient:
    import fake_devin.app as fd

    fd.SPEED = 0.2
    fd._sessions.clear()
    fd._attempts.clear()
    client = DevinClient(settings.devin_api_key, settings.devin_api_base)
    client._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake_devin_app), base_url="http://fake-devin",
                                       headers={"Authorization": "Bearer fake"})
    yield client
    await client.aclose()


@pytest.fixture
def github() -> RecordingGitHub:
    return RecordingGitHub()


@pytest.fixture
def store(settings) -> Store:
    return Store(settings.db_path)


@pytest.fixture
def orchestrator(settings, store, devin, github) -> Orchestrator:
    return Orchestrator(settings, store, devin, github)


def issue(number: int, title: str = "Bump python-multipart", labels=("security", "dependencies", "devin:remediate"), body: str = "do it") -> dict:
    return {
        "number": number, "title": title, "body": body,
        "html_url": f"https://github.com/Edark94/superset/issues/{number}",
        "labels": [{"name": lbl} for lbl in labels],
    }
