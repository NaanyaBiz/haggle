#!/usr/bin/env bash
# Start the Haggle webapp. Run from anywhere; it cd's to its own directory.
#
# Env overrides:
#   HAGGLE_PORT      (default 8000)
#   HAGGLE_HOST      (default 0.0.0.0)
#   HAGGLE_DB        (default ~/.haggle/haggle.db)
#   HAGGLE_TZ_OFFSET minutes east of UTC (default 600 = AEST)
#
# First run: just point your browser at http://localhost:8000 — the wizard
# will appear and walk you through AGL login.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: uv not found on PATH." >&2
  echo "Install it: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

PORT="${HAGGLE_PORT:-8000}"
HOST="${HAGGLE_HOST:-0.0.0.0}"

echo "→ syncing dependencies (uv sync)…"
uv sync --quiet

echo
echo "  Haggle dashboard: http://${HOST/0.0.0.0/localhost}:${PORT}/"
echo "  DB:               ${HAGGLE_DB:-$HOME/.haggle/haggle.db}"
echo
echo "  First run? Open the URL above and use the on-screen wizard to log in to AGL."
echo

exec uv run uvicorn webapp.main:app --host "$HOST" --port "$PORT" "$@"
