#!/usr/bin/env bash
# Fire a synthetic GitHub "issues.labeled" webhook at the orchestrator, signed like GitHub does.
#
#   scripts/simulate_webhook.sh                 # default sample issue (#101)
#   scripts/simulate_webhook.sh 102 block       # issue 102, fake-devin scenario "block"
#   SENTINEL_URL=http://localhost:8080 GITHUB_WEBHOOK_SECRET=change-me scripts/simulate_webhook.sh 103 fail
#
# Scenario tags ([sim:block] / [sim:fail] / [sim:nochange]) only mean something to the bundled fake Devin.
set -euo pipefail
NUMBER="${1:-101}"
SCENARIO="${2:-ok}"
URL="${SENTINEL_URL:-http://localhost:8080}"
SECRET="${GITHUB_WEBHOOK_SECRET:-}"
REPO="${GITHUB_REPO:-Edark94/superset}"
TAG=""; [ "$SCENARIO" != "ok" ] && TAG=" [sim:${SCENARIO}]"

BODY=$(cat <<JSON
{
  "action": "labeled",
  "label": {"name": "devin:remediate"},
  "issue": {
    "number": ${NUMBER},
    "title": "[security] python-multipart 0.0.29 is vulnerable (CVE-2026-53540) — upgrade to 0.0.31${TAG}",
    "body": "## Finding\nAutomated scan found python-multipart 0.0.29 affected by CVE-2026-53538/53539/53540.\n\n## Remediation\n- [ ] bump the pin in requirements/development.txt to 0.0.31\n\n## Verification\npip-audit clean.${TAG}",
    "html_url": "https://github.com/${REPO}/issues/${NUMBER}",
    "labels": [{"name": "security"}, {"name": "dependencies"}, {"name": "devin:remediate"}]
  },
  "repository": {"full_name": "${REPO}"}
}
JSON
)
SIG_HEADER=()
if [ -n "$SECRET" ]; then
  SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.* //')
  SIG_HEADER=(-H "X-Hub-Signature-256: sha256=${SIG}")
fi
curl -sS -X POST "${URL}/webhooks/github" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: issues" \
  -H "X-GitHub-Delivery: sim-$(date +%s)-${NUMBER}" \
  "${SIG_HEADER[@]}" \
  --data "$BODY"
echo
