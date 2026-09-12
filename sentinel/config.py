"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Devin
    devin_api_key: str = Field(default="", alias="DEVIN_API_KEY")
    devin_org_id: str = Field(default="", alias="DEVIN_ORG_ID")
    devin_api_base: str = Field(default="https://api.devin.ai", alias="DEVIN_API_BASE")
    devin_max_acu_per_session: int = Field(default=10, alias="DEVIN_MAX_ACU_PER_SESSION")
    devin_mode: str = Field(default="", alias="DEVIN_MODE")

    # GitHub
    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    github_repo: str = Field(default="Edark94/superset", alias="GITHUB_REPO")
    github_default_branch: str = Field(default="master", alias="GITHUB_DEFAULT_BRANCH")
    github_webhook_secret: str = Field(default="", alias="GITHUB_WEBHOOK_SECRET")
    github_dry_run: bool = Field(default=False, alias="GITHUB_DRY_RUN")
    github_api_base: str = Field(default="https://api.github.com", alias="GITHUB_API_BASE")

    # Policy
    trigger_label: str = Field(default="devin:remediate", alias="TRIGGER_LABEL")
    max_concurrent_sessions: int = Field(default=3, alias="MAX_CONCURRENT_SESSIONS")
    poll_interval_seconds: int = Field(default=30, alias="POLL_INTERVAL_SECONDS")
    issue_sweep_interval_seconds: int = Field(default=300, alias="ISSUE_SWEEP_INTERVAL_SECONDS")
    session_timeout_minutes: int = Field(default=90, alias="SESSION_TIMEOUT_MINUTES")
    max_retries: int = Field(default=1, alias="MAX_RETRIES")
    max_nudges: int = Field(default=2, alias="MAX_NUDGES")

    # Service
    db_path: str = Field(default="./data/sentinel.db", alias="SENTINEL_DB_PATH")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def repo_owner(self) -> str:
        return self.github_repo.split("/")[0]

    @property
    def repo_name(self) -> str:
        return self.github_repo.split("/")[1]


def load_settings() -> Settings:
    return Settings()
