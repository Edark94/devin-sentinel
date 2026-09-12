"""FastAPI entrypoint: webhook receiver, background orchestration loop, dashboard, JSON API, metrics."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import __version__
from .config import Settings, load_settings
from .devin_client import DevinClient
from .github_client import GitHubClient
from .observability import metrics
from .observability.logging import configure_logging
from .observability.report import render as render_report
from .orchestrator import LABEL_DEFINITIONS, Orchestrator
from .store import Store
from .triggers.sweep import sweep_once
from .triggers.webhook import handle_event, verify_signature

log = logging.getLogger("sentinel.main")
TEMPLATES = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape())


def build_app(cfg: Settings | None = None, *, run_background: bool = True) -> FastAPI:
    cfg = cfg or load_settings()
    configure_logging(cfg.log_level)
    store = Store(cfg.db_path)
    devin = DevinClient(cfg.devin_api_key, cfg.devin_api_base, cfg.devin_org_id, api_version=cfg.devin_api_version)
    gh = GitHubClient(cfg.github_token, cfg.github_repo, cfg.github_api_base, cfg.github_dry_run)
    orch = Orchestrator(cfg, store, devin, gh)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not cfg.github_webhook_secret:
            log.warning("GITHUB_WEBHOOK_SECRET is empty: webhook signatures are NOT verified (ok for local simulation only)")
        if not cfg.github_dry_run:
            try:
                await gh.ensure_labels(LABEL_DEFINITIONS)
            except RuntimeError as exc:
                log.warning("could not ensure labels: %s", exc)
        tasks: list[asyncio.Task] = []
        if run_background:
            tasks.append(asyncio.create_task(_orchestration_loop(orch, cfg)))
            if cfg.issue_sweep_interval_seconds > 0:
                tasks.append(asyncio.create_task(_sweep_loop(gh, orch, cfg)))
        log.info("devin-sentinel %s started", __version__, extra={"repo": cfg.github_repo, "devin_api": cfg.devin_api_base, "dry_run": cfg.github_dry_run})
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await devin.aclose()
            await gh.aclose()

    app = FastAPI(title="devin-sentinel", version=__version__, lifespan=lifespan)
    app.state.cfg, app.state.store, app.state.orchestrator, app.state.github = cfg, store, orch, gh

    # ---------------------------------------------------------------- triggers
    @app.post("/webhooks/github")
    async def github_webhook(
        request: Request,
        x_github_event: str = Header(default=""),
        x_github_delivery: str = Header(default=""),
        x_hub_signature_256: str | None = Header(default=None),
    ):
        body = await request.body()
        if not verify_signature(cfg.github_webhook_secret, body, x_hub_signature_256):
            metrics.WEBHOOKS_TOTAL.labels(event=x_github_event, result="bad_signature").inc()
            raise HTTPException(401, "invalid signature")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid json") from None
        result = await handle_event(x_github_event, x_github_delivery, payload, orchestrator=orch, store=store, trigger_label=cfg.trigger_label)
        return JSONResponse(result)

    @app.post("/api/tasks/from-issue/{number}")
    async def manual_trigger(number: int):
        """Manual trigger: enqueue an issue by number (same path as the webhook)."""
        issue = await gh.get_issue(number)
        task = await orch.accept_issue(issue, trigger="manual")
        return {"status": "accepted" if task else "already_tracked", "task": task.to_api() if task else None}

    @app.post("/api/sweep")
    async def manual_sweep():
        return {"accepted": await sweep_once(gh, orch, cfg.trigger_label)}

    @app.post("/api/tick")
    async def manual_tick():
        await orch.tick()
        return {"status": "ok"}

    # ---------------------------------------------------------------- observability
    @app.get("/api/tasks")
    async def list_tasks():
        return [t.to_api() for t in store.list_tasks()]

    @app.get("/api/tasks/{task_id:path}/events")
    async def task_events(task_id: str):
        return [e.to_api() for e in store.list_events(task_id)]

    @app.get("/api/events")
    async def all_events(limit: int = 100):
        return [e.to_api() for e in store.list_events(limit=limit)]

    @app.get("/api/summary")
    async def api_summary():
        return metrics.summary(store)

    @app.get("/report", response_class=PlainTextResponse)
    async def report():
        return render_report(store)

    @app.get("/metrics")
    async def prom_metrics():
        metrics.refresh_gauges(store)
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        tpl = TEMPLATES.get_template("dashboard.html")
        return tpl.render(
            summary=metrics.summary(store),
            tasks=[t.to_api() for t in store.list_tasks()],
            events=[e.to_api() for e in store.list_events(limit=40)],
            cfg={"repo": cfg.github_repo, "trigger_label": cfg.trigger_label, "devin_api": cfg.devin_api_base,
                 "max_concurrent": cfg.max_concurrent_sessions, "acu_budget": cfg.devin_max_acu_per_session, "dry_run": cfg.github_dry_run},
            version=__version__,
        )

    return app


async def _orchestration_loop(orch: Orchestrator, cfg: Settings) -> None:
    while True:
        try:
            await orch.tick()
        except Exception:
            log.exception("orchestration tick failed")
        await asyncio.sleep(cfg.poll_interval_seconds)


async def _sweep_loop(gh: GitHubClient, orch: Orchestrator, cfg: Settings) -> None:
    await asyncio.sleep(5)
    while True:
        try:
            await sweep_once(gh, orch, cfg.trigger_label)
        except Exception:
            log.exception("issue sweep failed")
        await asyncio.sleep(cfg.issue_sweep_interval_seconds)


# Served with `uvicorn sentinel.main:build_app --factory` so importing this module has no side effects.
