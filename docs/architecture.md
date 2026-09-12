# Architecture

## Components

| Component | File | Role |
|---|---|---|
| Triggers | `sentinel/triggers/` | Turn external events into `Task`s. Webhook (`issues`, `pull_request`), scheduled sweep, scan-results (pip-audit / npm audit → issues), Devin triage (read-only session → proposed issues), manual API. |
| Orchestrator | `sentinel/orchestrator.py` | The state machine. One `tick()` = admit queued tasks → poll active sessions → react → reconcile PRs → refresh gauges. |
| Devin client | `sentinel/devin_client.py` | v3 org API with v1 fallback: create/get/list sessions, message, terminate, tags. Retries on 429/5xx with backoff; org id resolved from `/v3/self`; v1 `status_enum`/`pull_request` normalised into the v3 vocabulary. |
| GitHub client | `sentinel/github_client.py` | Issues, labels, comments, PR state. Dry-run aware. |
| Prompt contract | `sentinel/prompts.py` | The prompt Devin receives + the structured-output JSON schema. |
| Store | `sentinel/store.py` | SQLite (WAL). Tables: `tasks`, `events` (audit trail), `deliveries` (webhook dedupe). |
| Observability | `sentinel/observability/` | Prometheus metrics, leadership summary, Markdown report, JSON logs. |
| Web | `sentinel/main.py`, `templates/dashboard.html` | FastAPI: `/webhooks/github`, `/api/*`, `/`, `/report`, `/metrics`, `/healthz`. |
| Fake Devin | `fake_devin/app.py` | Scripted v3 API stand-in for tests and demos. |

## Two session types

| Session | Started by | Budget | Output | What Sentinel does with it |
|---|---|---|---|---|
| **Remediation** | a labelled issue | `DEVIN_MAX_ACU_PER_SESSION` | PR + structured outcome | labels/comments on the issue, PR reconciliation, KPIs |
| **Triage** | `POST /api/triage` / CLI (or a schedule) | `--max-acu` (default 5) | structured findings (title, category, files, evidence, remediation, verification, confidence) | files one issue per finding, labelled `devin:triage` + category; `--auto` adds `devin:remediate` so the remediation loop starts immediately |

Both are ordinary Devin sessions with a JSON schema for `structured_output`; the difference is the
prompt and what the orchestrator does with the result. Triage runs are recorded in the events table
under `triage:<session_id>` and show up in the dashboard feed.

## Task lifecycle

```
                         ┌──────────┐
   issue labelled ─────► │  queued  │ ◄───── sweep / scanner / manual
                         └────┬─────┘
             slot free        │  POST /sessions (budget, tags, schema, repo)
                         ┌────▼──────────┐
                         │session_created│
                         └────┬──────────┘
        GET /sessions/{id}    │
   ┌──────────────────────────┼──────────────────────────────┐
   │ status=running           │ status_detail=waiting_for_user│ status=error / timeout
   │ detail=working           ▼                               ▼
   │                     ┌─────────┐   POST /messages    ┌─────────────┐
   └──────────────────►  │ running │ ──────────────────► │   blocked   │ ── nudges > MAX ──► needs_human
                         └────┬────┘ ◄────────────────── └─────────────┘
                              │ finished / exit                        attempts ≤ MAX_RETRIES
                              │                                        ┌──────────────┐
          ┌───────────────────┼─────────────────────┐          error ─►│ retry (new    │
          ▼                   ▼                     ▼                  │ session with  │
   ┌───────────┐       ┌────────────┐        ┌────────────┐            │ failure ctx)  │
   │ pr_opened │       │ no_change  │        │needs_human │            └──────┬───────┘
   └─────┬─────┘       └────────────┘        └────────────┘                   │ exhausted
         │ PR merged (webhook or reconcile)                                   ▼
   ┌─────▼─────┐                                                        ┌──────────┐
   │  merged   │                                                        │  failed  │
   └───────────┘                                                        └──────────┘
```

Every transition writes an `events` row and updates the issue's `devin:*` status label. Comments are
posted at: session start (link), PR opened (summary/verification/risk/ACUs), no-change, needs-human,
failed.

## Mapping Devin status → task state

| Devin `status` / `status_detail` | Sentinel |
|---|---|
| `running` / `working` | `running` |
| `running` / `waiting_for_user`, `waiting_for_approval` | `blocked` → nudge (bounded) → `needs_human` |
| `running` / `finished`, `exit` | harvest `pull_requests[]` + `structured_output` → `pr_opened` / `no_change` / `needs_human` (outcome=blocked) / retry (no outcome) |
| `error`, `suspended` with a limit/error reason | retry once with context, then `failed` |
| wall-clock > `SESSION_TIMEOUT_MINUTES` | `DELETE /sessions/{id}` → retry / `failed` |

`acus_consumed` is copied on every poll, so cost is visible while the session runs (v3 only; v1 has no
cost field).

| v1 `status_enum` | normalised to |
|---|---|
| `working`, `resumed`, `*_requested*` | `running` / `working` |
| `blocked` | `running` / `waiting_for_user` |
| `finished` | `exit` / `finished` |
| `expired`, `suspended`, `stopped` | `suspended` / `inactivity` or `user_request` (retryable, not an error) |

## Idempotency and safety

- Task id = `owner/repo#issue`. An issue with an active or successful task is never re-run; a failed or
  needs-human task is re-run only when the trigger label is re-applied.
- Webhook deliveries are deduplicated by `X-GitHub-Delivery`; signatures are verified with a
  constant-time HMAC compare.
- The prompt forbids touching any repository other than the configured fork and names the branch.
- Budget (`max_acu_limit`), concurrency, nudges, retries and timeout are all capped; every cap ends in
  a visible terminal state.

## Observability surfaces

- `GET /` — dashboard (KPIs, tasks, category breakdown, pipeline, event feed). Auto-refresh 15 s.
- `GET /api/summary` — the KPI JSON.
- `GET /api/tasks`, `GET /api/tasks/{id}/events`, `GET /api/events` — raw data.
- `GET /report` — Markdown status report; `python -m sentinel.observability.report --post <issue>`
  posts it to GitHub.
- `GET /metrics` — Prometheus: `sentinel_tasks_total{trigger}`, `sentinel_state_transitions_total{to_state}`,
  `sentinel_devin_sessions_created_total`, `sentinel_nudges_total`, `sentinel_webhooks_total{event,result}`,
  `sentinel_devin_api_errors_total{op}`, `sentinel_active_tasks{state}`, `sentinel_acus_consumed_total`,
  `sentinel_time_to_pr_seconds` (histogram).
- Logs: one JSON line per decision (`task transition`, `devin api … retrying`, sweep results).
