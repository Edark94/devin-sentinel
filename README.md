# devin-sentinel

**Event-driven remediation for `apache/superset`, with Devin as the execution engine.**

A security scan or a labelled GitHub issue is the *event*. Devin is the *worker*. Sentinel is the
*control plane* in between: it turns events into bounded Devin sessions, keeps them moving without a
human in the loop, mirrors progress onto the issue, and answers the question every engineering leader
asks about automation: *is it actually working, and what is it costing me?*

```
 pip-audit / npm audit      GitHub issue labelled          cron sweep
 (scan-results trigger)     devin:remediate (webhook)      (scheduled trigger)
            │                        │                          │
            └──────────── issues ────┴──────── intake ──────────┘
                                          │
                                   ┌──────▼──────┐   POST /v3/organizations/{org}/sessions
                                   │  sentinel   │ ─────────────────────────────────────────►  Devin
                                   │ orchestrator│ ◄───────────────────────────────────────── (status,
                                   │ state machine│  GET session / POST message / DELETE        ACUs, PR,
                                   └──┬───────┬──┘                                             structured
                 labels + comments    │       │  SQLite audit trail                            output)
                 on the issue ◄───────┘       └──► dashboard · /api/summary · /metrics · /report
```

Target repository: **https://github.com/Edark94/superset** (fork of apache/superset).
Issues seeded and remediated by this system: see [`docs/issues.md`](docs/issues.md).

---

## What problem this solves

Every team has a queue of *known, well-specified, low-glamour* work: CVE bumps from the last
Dependabot/pip-audit run, lint findings after a tooling upgrade, mechanical refactors. It is exactly the
work that never gets scheduled, because each item is 30–90 minutes of an engineer's attention spread
over branch, fix, verify, PR, and follow-up. The findings pile up; the audit report goes red.

Sentinel makes that queue drain itself:

1. **Scan → issue.** `pip-audit` and `npm audit` run against the repo. Each fixable finding becomes a
   GitHub issue with the advisory, the manifest, the fixed version, and a verification recipe. For what
   scanners cannot see, a **triage session** has Devin read an area of the code and propose issues with
   file:line evidence, a remediation plan and a verification recipe; a human approves them with a label.
2. **Issue → Devin session.** The moment an issue carries the `devin:remediate` label (from the scanner,
   a human, or a sweep), Sentinel opens a Devin session with a hardened prompt, a structured-output
   contract, an ACU budget, and the fork pinned as the only allowed target.
3. **Session → PR.** Sentinel polls the session, nudges Devin if it stalls waiting for a human, retries
   once on failure with the failure context, and terminates on timeout. When a PR appears, the issue gets
   a comment with Devin's summary, verification steps, risk rating and ACU cost. Labels track every state.
4. **PR → merged.** PR state is reconciled (webhook or polling) so the task closes out when the team merges.
5. **Everything → dashboard.** Throughput, success rate, median time-to-PR, ACUs per PR, needs-human
   queue, and a per-task event timeline. Prometheus metrics for SREs, a Markdown report for leadership.

## Why Devin is the right primitive here

- The unit of work is *"understand this issue in a 10k-file repo, change the right pins/code, run the
  right subset of checks, open a reviewable PR"*. That needs a real environment, a shell, and judgment —
  not a templated diff. A scripted `pip install --upgrade` cannot decide that `paramiko` has no fixed
  version yet, or that `@deck.gl` needs a semver-major jump nobody asked for.
- Devin's API exposes exactly the control points an orchestrator needs: **create with budget and tags**,
  **poll status + `status_detail`** (working / waiting_for_user / finished), **message into a running
  session**, **structured output validated against a schema**, **terminate**, and **ACU consumption** per
  session. Sentinel uses all of them; nothing here depends on a person watching the Devin UI. (Personal API keys
are currently accepted by the v1 endpoints only; `DEVIN_API_VERSION=auto` detects that and falls back,
at the cost of ACU and `status_detail` data. See *Known limitations*.)
- Because Devin returns *structured* output, the outcome (`pr_opened` / `no_change_needed` / `blocked`)
  is machine-readable. That is what lets Sentinel close the loop on GitHub and compute real success rates
  instead of "a session ended".

## Repository layout

```
sentinel/
  main.py               FastAPI app: webhook receiver, background loop, dashboard, JSON API, /metrics
  orchestrator.py       the state machine (intake → admit → poll → nudge/retry/finish → reconcile PR)
  devin_client.py       Devin v3 client (sessions, messages, tags, terminate) with retry/backoff
  github_client.py      issues, labels, comments, PR state; dry-run aware
  prompts.py            the prompt contract + structured-output JSON schema
  models.py / store.py  Task, Event, TaskState; SQLite persistence (WAL) + webhook delivery dedupe
  triggers/webhook.py   HMAC verification, issues / pull_request events
  triggers/sweep.py     scheduled trigger: pick up labelled issues the webhook missed
  triggers/scanner.py   pip-audit + npm audit → grouped, deduplicated remediation issues
  triggers/triage.py    Devin-driven discovery: read-only session → structured findings → proposed issues
  observability/        Prometheus metrics, leadership summary, Markdown report, JSON logging
fake_devin/app.py       a scripted stand-in for the Devin v3 API (happy path, block, fail, no-change)
scripts/                simulate_webhook.sh (signed synthetic GitHub event), run_local.sh
tests/                  26 tests: state machine, retries, nudges, timeout, webhook auth/dedupe, scanner
docs/                   architecture.md, issues.md (seeded issues)
```

## Running it

### 0. Prerequisites
- Docker + Docker Compose.
- A Devin API key (`app.devin.ai → Settings → API keys`; personal `apk_user_*` or service `apk_*`). If `GET /v3/self` returns no `org_id` for your key (multi-org accounts), set `DEVIN_ORG_ID` too.
- Devin's GitHub integration installed on the fork (so it can push branches and open PRs).
- A GitHub token with `issues:write` on the fork (for comments/labels; the scanner also needs it).

### 1. Simulation mode — no Devin credits, no GitHub writes (2 minutes)

```bash
docker compose -f docker-compose.sim.yml up --build
# in another shell:
scripts/simulate_webhook.sh 101            # happy path → PR
scripts/simulate_webhook.sh 102 block      # Devin asks a question → sentinel nudges → PR
scripts/simulate_webhook.sh 103 fail       # first attempt errors → retried with context → PR
scripts/simulate_webhook.sh 104 nochange   # Devin reports nothing to change
open http://localhost:8080                 # dashboard (auto-refreshes)
curl -s localhost:8080/api/summary | jq    # what the dashboard is computed from
curl -s localhost:8080/report              # Markdown report for leadership
curl -s localhost:8080/metrics | grep sentinel_
```

The `sentinel-sim` service talks to `fake-devin` (a scripted implementation of the v3 endpoints Sentinel
uses) and runs GitHub in dry-run, so comments/labels are logged rather than posted. Everything else —
intake, admission, polling, nudging, retry, structured-output harvesting, PR reconciliation, metrics — is
the real code path.

### 2. Real mode — Devin remediates the fork

```bash
cp .env.example .env    # fill in DEVIN_API_KEY, GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET
docker compose up --build
```

Then choose your trigger(s):

| Trigger | How | Needs public URL? |
|---|---|---|
| **Webhook** (real-time) | Repo → Settings → Webhooks → `https://<host>/webhooks/github`, content type JSON, secret = `GITHUB_WEBHOOK_SECRET`, events: *Issues*, *Pull requests*. For a laptop use `ngrok http 8080` or `smee`. | yes |
| **Sweep** (scheduled) | Enabled by default (`ISSUE_SWEEP_INTERVAL_SECONDS=300`): every 5 min Sentinel lists open issues with `devin:remediate` and accepts any it does not track. | no |
| **Scanner** (scan results) | `docker compose --profile scan run --rm scanner` runs pip-audit + npm audit against the fork and files labelled issues → picked up by webhook/sweep. `--dry-run` to preview. | no |
| **Manual** | `curl -X POST localhost:8080/api/tasks/from-issue/<n>` or just add the label in the GitHub UI. | no |
| **Triage** (Devin-driven discovery) | `python -m sentinel.triggers.triage --area superset/sql --max-issues 3` or `POST /api/triage?area=...`. One read-only Devin session explores the code and returns structured findings; Sentinel files them as issues labelled `devin:triage` for a human to approve with `devin:remediate` (`--auto` skips approval). | no |

Watch http://localhost:8080. Each task shows the Devin session link, attempt count, nudges, ACUs, and the
PR once it exists. The issue itself gets the same story as comments and labels
(`devin:queued → devin:in-progress → devin:pr-open → devin:done`, or `devin:needs-human` / `devin:failed`).

### 3. Local development

```bash
make venv && make test && make lint
scripts/run_local.sh          # uvicorn with reload, DB in ./data
```

## Policy knobs (all in `.env`)

| Variable | Default | Effect |
|---|---|---|
| `DEVIN_MAX_ACU_PER_SESSION` | 10 | Hard budget; passed as `max_acu_limit`, also stated in the prompt so Devin degrades gracefully (draft PR + `blocked`) instead of running out. |
| `MAX_CONCURRENT_SESSIONS` | 3 | Admission control. Extra tasks wait in `queued`. |
| `MAX_NUDGES` | 2 | Auto-replies to `waiting_for_user` before escalating to `needs_human`. |
| `MAX_RETRIES` | 1 | New session with the failure context on error/timeout/empty outcome. |
| `SESSION_TIMEOUT_MINUTES` | 90 | Terminate + retry/fail. |
| `POLL_INTERVAL_SECONDS` | 30 | Devin polling cadence. |
| `ISSUE_SWEEP_INTERVAL_SECONDS` | 300 | Scheduled trigger; 0 disables. |
| `DEVIN_MODE` | (org default) | `fast` / `lite` / `ultra` override (v3 only). |
| `DEVIN_API_VERSION` | auto | `auto` probes `/v3/self` and falls back to `v1`; or pin `v1` / `v3`. |
| `GITHUB_DRY_RUN` | 0 | Log GitHub writes instead of performing them. |

## How you know it is working

`/` (dashboard), `/api/summary`, `/report` and `/metrics` all derive from the same SQLite audit trail:

- **Throughput:** tasks accepted, PRs opened, PRs merged — overall and per category.
- **Quality:** success rate over terminal tasks; `needs_human` and `failed` counts with the reason.
- **Speed:** median time from session start to PR (`sentinel_time_to_pr_seconds` histogram).
- **Cost:** ACUs consumed in total and per PR, straight from Devin's `acus_consumed`.
- **Health:** `sentinel_devin_api_errors_total`, webhook results (`accepted / duplicate / bad_signature`),
  active tasks by state.
- **Forensics:** `/api/tasks/<id>/events` is the full timeline of one task (created → session → nudged →
  PR detected → structured output → PR merged), and every orchestrator decision is a JSON log line.

## Design decisions worth knowing

- **Issues are the single intake.** Scanner, webhook, sweep and manual triggers all produce the same
  thing: a labelled GitHub issue → `Task`. One audit trail, one idempotency key (`repo#issue`), and the
  team can always intervene with the tools they already use (edit the issue, drop the label).
- **The prompt is a contract, not a wish.** It pins the repo and branch naming, forbids touching
  upstream, states the budget, tells Devin to decide-and-document rather than wait, and requires the
  structured output. Category-specific guidance (security / dependencies / code-quality / frontend) is
  derived from issue labels.
- **Bounded autonomy.** Budget cap, concurrency cap, nudge cap, retry cap, wall-clock timeout. Every cap
  ends in a visible state on the issue and the dashboard — never a silently dead session.
- **Webhooks are treated as unreliable.** HMAC-verified, deduplicated by delivery id, and backed by the
  scheduled sweep, so a missed delivery costs minutes, not a finding.
- **Simulator is first-class.** The fake Devin implements the exact endpoints and status vocabulary the
  client uses, and scripts every branch of the state machine; the test suite runs the real orchestrator
  against it in-process.

## Extending this in a real engagement

- Feed it from the tools the customer already has: Dependabot alerts / GitHub code scanning webhooks,
  Snyk or Wiz findings, SonarQube quality gates, Jira tickets in a "ready for automation" column.
- Let Devin review Devin: a third session type that reviews the PR against the issue and CI results
  and posts a review; only PRs that pass go to a human.
- Schedule triage per subsystem (weekly, rotating areas) so discovery is continuous, and track the
  approval rate of proposed issues as a quality signal for the triage prompt.
- Post-merge verification: re-run the scanner after merge and auto-close the issue when the finding is gone.
- Budgets by category and repository; weekly `/report` posted to Slack; SLOs on time-to-PR.
- Multi-repo: the orchestrator is already keyed by `repo#issue`; add a repo→settings map and playbooks
  (`playbook_id`) per repo for build/test conventions.
- Move triggers into Devin Automations / Terraform once the policy is stable, keeping this service as
  the analytics and escalation layer.

## Known limitations

- **Personal API keys and v3.** At the time of writing an `apk_user_*` key returns 403 on `/v3/self`
  and every `/v3/organizations/...` route while `/v1/*` works. The client probes once and falls back to
  v1 (`DEVIN_API_VERSION=auto`). On v1 there is no `acus_consumed` and no `status_detail`, so the cost
  KPIs read 0 and blocked/working/finished is derived from `status_enum`. A service-user key
  (`apk_*`, created by an org admin) unlocks v3 and the ACU figures without any code change.
- **Consumption API.** `/v1/enterprise/consumption` answers "Contact support to enable the consumption
  API" on this org, so per-session cost cannot be backfilled from there either.
- **Devin's GitHub access** to the fork is a prerequisite Sentinel cannot verify through the API on v1;
  if a session cannot push, it ends up `needs_human` with Devin's explanation on the issue.

## Reference

- Devin API: https://docs.devin.ai/api-reference/overview (v3 OpenAPI: https://docs.devin.ai/v3-openapi.yaml)
- Endpoints used (v3): `GET /v3/self`, `POST/GET /v3/organizations/{org}/sessions`, `GET …/sessions/{id}`,
  `POST …/sessions/{id}/messages`, `DELETE …/sessions/{id}`, `PUT …/sessions/{id}/tags`.
- Endpoints used (v1 fallback): `POST/GET /v1/sessions`, `GET/DELETE /v1/sessions/{id}`,
  `POST /v1/sessions/{id}/message`, `PUT /v1/sessions/{id}/tags`.
