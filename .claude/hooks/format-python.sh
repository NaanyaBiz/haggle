#!/usr/bin/env bash
# PostToolUse hook: ruff-format + ruff-check --fix any *.py the agent edits.
# Reads the tool-call payload from stdin (JSON) and acts only on .py files.
# Stays silent on success; ruff prints diagnostics on failure.
set -euo pipefail

payload="$(cat)"
file_path="$(printf '%s' "$payload" | python3 -c 'import json, sys; d=json.load(sys.stdin); print(d.get("tool_input",{}).get("file_path",""))' 2>/dev/null || true)"

if [[ -z "$file_path" || "$file_path" != *.py ]]; then
    exit 0
fi
if [[ ! -f "$file_path" ]]; then
    exit 0
fi

# Use uv run so we hit the project's pinned ruff.
if command -v uv >/dev/null 2>&1; then
    uv run ruff format "$file_path" >/dev/null 2>&1 || true
    uv run ruff check --fix --quiet "$file_path" >/dev/null 2>&1 || true
fi
exit 0
