#!/usr/bin/env bash
# PostToolUse hook: validate manifest.json / hacs.json after edits.
# Catches HACS-breaking typos (missing keys, bad iot_class, non-semver
# version) at edit time, before they reach CI.
set -euo pipefail

payload="$(cat)"
file_path="$(printf '%s' "$payload" | python3 -c 'import json, sys; d=json.load(sys.stdin); print(d.get("tool_input",{}).get("file_path",""))' 2>/dev/null || true)"

case "$(basename "${file_path:-}")" in
    manifest.json|hacs.json)
        if command -v uv >/dev/null 2>&1; then
            uv run python scripts/validate_manifest.py "$file_path" || {
                echo "manifest validation failed: $file_path" >&2
                exit 2   # block via non-zero exit (Claude Code surfaces stderr)
            }
        fi
        ;;
esac
exit 0
