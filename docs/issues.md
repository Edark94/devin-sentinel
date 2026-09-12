# Seeded issues on the fork

Fork: https://github.com/Edark94/superset (default branch `master`, forked 2026-09-12 at upstream
`3e7bdec5`). All issues carry `devin:remediate`, the trigger label.

Issues #4–#8 were filed by the scanner (`python -m sentinel.triggers.scanner --ref master`, which runs
`pip-audit` on `requirements/base.txt` + `requirements/development.txt` and `npm audit --package-lock-only`
on `superset-frontend`). Issue #3 was filed by hand from a `ruff check` run with the repo's own config.

| # | Category | Title | Source | Fix |
|---|---|---|---|---|
| [#3](https://github.com/Edark94/superset/issues/3) | code-quality | ruff B023: lambda in StarRocks dialect does not bind loop variable | `ruff check superset` (ruff 0.16.7) | rewrite binding in `superset/sql/dialects/starrocks.py` |
| [#4](https://github.com/Edark94/superset/issues/4) | security, dependencies | jaraco-context 6.0.1 Zip Slip (CVE-2026-23949) | pip-audit, `requirements/development.txt` | bump to 6.1.0 |
| [#5](https://github.com/Edark94/superset/issues/5) | security, dependencies | pytest 7.4.4 (CVE-2025-71176, `/tmp/pytest-of-*` local attack) | pip-audit, `requirements/development.txt` | bump to 9.0.3 (major; exercises the budget/`blocked` path) |
| [#6](https://github.com/Edark94/superset/issues/6) | security, dependencies | python-multipart 0.0.29 (CVE-2026-53538/53539/53540) | pip-audit, `requirements/development.txt` | bump to 0.0.31 |
| [#7](https://github.com/Edark94/superset/issues/7) | security, dependencies, frontend | brace-expansion 4.0.0–5.0.8 DoS (GHSA-rgw5-rvv9-x895, via nx) | npm audit, `superset-frontend/package-lock.json` | targeted `npm audit fix` |
| [#8](https://github.com/Edark94/superset/issues/8) | security, dependencies, frontend | smol-toml ≤1.7.0 DoS (GHSA-7w5x-hrqm-74c2) | npm audit, `superset-frontend/package-lock.json` | targeted `npm audit fix` |

## Proposed by a Devin triage session (2026-09-12, area `superset/sql`, 5 ACU budget, ~5 minutes)

Filed with `devin:triage` for human approval; add `devin:remediate` to hand one to the remediation loop.

| # | Title | Confidence |
|---|---|---|
| [#15](https://github.com/Edark94/superset/issues/15) | `SQLStatement.is_destructive` misses lower-case ALTER commands sqlglot parses as `exp.Command` (bypasses the destructive-DDL guard used by the MCP `execute_sql` tool) | high |
| [#16](https://github.com/Edark94/superset/issues/16) | `_SELECT_TRAILING_CLAUSES` uses the stale sqlglot arg name `from`, so trailing comments are re-attached before FROM | high |
| [#17](https://github.com/Edark94/superset/issues/17) | `process_jinja_sql` leaks `TypeError`/`IndexError` instead of `SupersetParseError` for bad partition-macro references | high |

## Remediation results

| Issue | PR | Time to PR |
|---|---|---|
| #3 | [#14](https://github.com/Edark94/superset/pull/14) | 5 min |
| #4 | [#13](https://github.com/Edark94/superset/pull/13) | 2 min |
| #5 | [#12](https://github.com/Edark94/superset/pull/12) | 4 min |
| #6 | [#9](https://github.com/Edark94/superset/pull/9) | 6 min (incl. the nudge bug, see README "Known limitations") |
| #7 | [#11](https://github.com/Edark94/superset/pull/11) | 4 min |
| #8 | [#10](https://github.com/Edark94/superset/pull/10) | 8 min |

Findings deliberately **not** filed:
- `paramiko 3.5.1` (CVE-2026-44405): no fixed release yet — the scanner reports it but files nothing.
- `@deck.gl/*`, `lerna` transitive advisories: only fixable via semver-major bumps of direct
  dependencies; out of scope for automated remediation (policy in `npm_findings`).
- 81 × `S101 assert` and 4 × `S704 Markup` from ruff 0.16: `S101` is ignored by the repo config; the
  `S704` sites already escape their inputs and only show up because the repo pins ruff 0.9.7.

Remediation results (PR links, ACUs, timings) are recorded live on each issue and on the dashboard;
`GET /report` produces the table for this document once the run has completed.
