#!/usr/bin/env bash
# Run the orchestrator on the host (no Docker). Expects a .venv created via `make venv`.
set -euo pipefail
cd "$(dirname "$0")/.."
export SENTINEL_DB_PATH="${SENTINEL_DB_PATH:-./data/sentinel.db}"
exec .venv/bin/uvicorn sentinel.main:build_app --factory --host 0.0.0.0 --port "${PORT:-8080}" --reload
