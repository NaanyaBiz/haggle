#!/usr/bin/env python3
"""Validate manifest.json and hacs.json against HA/HACS schema rules.

Used by the validate-manifest Claude hook and can be run standalone:
    python scripts/validate_manifest.py custom_components/haggle/manifest.json
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def _error(msg: str) -> None:
    print(f"[validate_manifest] ERROR: {msg}", file=sys.stderr)


def _ok(msg: str) -> None:
    print(f"[validate_manifest] OK: {msg}")


SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
DOMAIN_RE = re.compile(r"^[a-z0-9_]+$")
CODEOWNER_RE = re.compile(r"^@[A-Za-z0-9_\-]+$")

REQUIRED_MANIFEST_KEYS = {
    "domain",
    "name",
    "codeowners",
    "documentation",
    "issue_tracker",
    "version",
    "iot_class",
}

VALID_IOT_CLASSES = {
    "assumed_state",
    "cloud_polling",
    "cloud_push",
    "local_polling",
    "local_push",
    "calculated",
}


def _check_manifest_fields(data: dict) -> list[str]:  # type: ignore[type-arg]
    errors: list[str] = []

    missing = REQUIRED_MANIFEST_KEYS - set(data)
    errors.extend(f"missing required key: {k!r}" for k in sorted(missing))

    if (domain := data.get("domain")) and not DOMAIN_RE.match(domain):
        errors.append(f"domain {domain!r} must match [a-z0-9_]+")

    if (version := data.get("version")) and not SEMVER_RE.match(str(version)):
        errors.append(f"version {version!r} must be semver (X.Y.Z)")

    codeowners = data.get("codeowners")
    if codeowners is not None and not isinstance(codeowners, list):
        errors.append("codeowners must be a list")
    elif isinstance(codeowners, list):
        errors.extend(
            f"codeowner {o!r} must start with @"
            for o in codeowners
            if not CODEOWNER_RE.match(o)
        )

    if (iot_class := data.get("iot_class")) and iot_class not in VALID_IOT_CLASSES:
        errors.append(f"iot_class {iot_class!r} not in {sorted(VALID_IOT_CLASSES)}")

    return errors


def validate_manifest(path: Path) -> bool:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        _error(f"cannot read {path}: {exc}")
        return False

    errors = _check_manifest_fields(data)
    if errors:
        for err in errors:
            _error(f"{path.name}: {err}")
        return False

    _ok(f"{path.name} is valid")
    return True


def validate_hacs(path: Path) -> bool:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        _error(f"cannot read {path}: {exc}")
        return False

    errors = [
        f"missing required key: {k!r}"
        for k in ("name", "homeassistant")
        if k not in data
    ]
    if errors:
        for err in errors:
            _error(f"{path.name}: {err}")
        return False

    _ok(f"{path.name} is valid")
    return True


def main() -> int:
    paths = [Path(p) for p in sys.argv[1:]] if len(sys.argv) > 1 else []

    if not paths:
        paths = list(Path("custom_components").rglob("manifest.json"))
        paths += list(Path(".").glob("hacs.json"))

    if not paths:
        _error("no manifest.json or hacs.json found")
        return 1

    ok = True
    for path in paths:
        if path.name == "manifest.json":
            ok = validate_manifest(path) and ok
        elif path.name == "hacs.json":
            ok = validate_hacs(path) and ok
        else:
            _error(f"unknown file type: {path}")
            ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
