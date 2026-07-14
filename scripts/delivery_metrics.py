#!/usr/bin/env python3
"""Quarterly delivery metrics over GitHub release/issue data (CO-18.3).

Three one-screen outputs:

1. Release-record reconciliation — CHANGELOG version headings vs git tags
   vs GitHub releases, so phantom versions (e.g. the never-tagged 0.3.2)
   are caught instead of silently accumulating. A heading is an
   *acknowledged* non-release if its heading line carries a marker
   ("NEVER RELEASED", "YANKED", "not yet formally released").
2. Change-failure proxy — releases followed within 7 days by a corrective
   release (one whose CHANGELOG section has a "### Fixed" heading),
   reported over all releases and over stable (non-prerelease) releases.
3. Median bug-report -> fix-release latency — for closed `bug` issues
   (excluding not-planned), latency from issue creation to the first
   release published at-or-after the issue closed.

Time-to-restore is deliberately NOT measured: HACS distribution is
pull-based, there is no telemetry into user installs, and "restore"
happens per-household whenever the user upgrades. Accepted limitation —
see docs/delivery-metrics.md.

Usage: python3 scripts/delivery_metrics.py   (needs an authenticated gh)
"""

from __future__ import annotations

import itertools
import json
import re
import statistics
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
CORRECTIVE_WINDOW = timedelta(days=7)
HEADING = re.compile(r"^## \[(?P<ver>[^\]]+)\](?P<rest>[^\n]*)$", re.MULTILINE)
ACKNOWLEDGED = re.compile(
    r"NEVER RELEASED|YANKED|not yet formally released", re.IGNORECASE
)


def _run(*argv: str) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout


def _gh(*args: str) -> Any:
    return json.loads(_run("gh", *args))


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _changelog_sections() -> dict[str, tuple[str, str]]:
    """Map version -> (heading rest-of-line, section body)."""
    parts = HEADING.split(CHANGELOG.read_text())
    # split() yields [pre, ver, rest, body, ver, rest, body, ...]
    return {parts[i]: (parts[i + 1], parts[i + 2]) for i in range(1, len(parts) - 2, 3)}


def reconcile(
    sections: dict[str, tuple[str, str]],
    tags: set[str],
    releases: list[dict[str, Any]],
) -> list[str]:
    problems = []
    released = {r["tagName"] for r in releases}
    for ver, (rest, _body) in sections.items():
        if ver == "Unreleased" or ACKNOWLEDGED.search(rest):
            continue
        if f"v{ver}" not in tags:
            problems.append(f"PHANTOM: CHANGELOG [{ver}] has no git tag v{ver}")
        elif f"v{ver}" not in released:
            problems.append(f"NO RELEASE: tag v{ver} has no GitHub release")
    problems.extend(
        f"UNDOCUMENTED: tag {tag} has no CHANGELOG heading"
        for tag in sorted(tags)
        if tag.removeprefix("v") not in sections
    )
    return problems


def change_failure(
    releases: list[dict[str, Any]], sections: dict[str, tuple[str, str]]
) -> tuple[list[str], int, int, int]:
    failed = []
    stable_failed = 0
    stable_total = sum(1 for r in releases[:-1] if not r["isPrerelease"])
    for cur, nxt in itertools.pairwise(releases):
        gap = _dt(nxt["publishedAt"]) - _dt(cur["publishedAt"])
        _rest, body = sections.get(nxt["tagName"].removeprefix("v"), ("", ""))
        if gap <= CORRECTIVE_WINDOW and "### Fixed" in body:
            failed.append(f"{cur['tagName']} -> {nxt['tagName']} ({gap.days}d)")
            if not cur["isPrerelease"]:
                stable_failed += 1
    return failed, len(releases) - 1, stable_failed, stable_total


def bug_latency(releases: list[dict[str, Any]]) -> list[float]:
    issues = _gh(
        "issue",
        "list",
        "--state",
        "closed",
        "--label",
        "bug",
        "--limit",
        "500",
        "--json",
        "number,createdAt,closedAt,stateReason",
    )
    latencies = []
    for issue in issues:
        if issue["stateReason"] == "not_planned":
            continue
        fix = next(
            (r for r in releases if _dt(r["publishedAt"]) >= _dt(issue["closedAt"])),
            None,
        )
        if fix is not None:
            delta = _dt(fix["publishedAt"]) - _dt(issue["createdAt"])
            latencies.append(delta.total_seconds() / 86400)
    return latencies


def main() -> None:
    releases = _gh(
        "release",
        "list",
        "--limit",
        "500",
        "--json",
        "tagName,publishedAt,isPrerelease",
    )
    releases.sort(key=lambda r: str(r["publishedAt"]))
    tags = set(_run("git", "tag", "-l").split())
    sections = _changelog_sections()

    print(f"Delivery metrics — {datetime.now().date()} — {len(releases)} releases\n")

    problems = reconcile(sections, tags, releases)
    print("Release-record reconciliation:")
    for problem in problems:
        print(f"  !! {problem}")
    if not problems:
        print("  OK — CHANGELOG headings, git tags and GitHub releases agree")

    failed, total, stable_failed, stable_total = change_failure(releases, sections)
    print(f"\nChange-failure proxy (corrective release <= {CORRECTIVE_WINDOW.days}d):")
    print(f"  all releases:    {len(failed)}/{total}")
    print(f"  stable releases: {stable_failed}/{stable_total}")
    for pair in failed:
        print(f"    {pair}")

    latencies = bug_latency(releases)
    print(f"\nBug-report -> fix-release latency ({len(latencies)} closed bugs):")
    if latencies:
        print(
            f"  median {statistics.median(latencies):.1f}d, max {max(latencies):.1f}d"
        )

    print("\nTime-to-restore: not measured (pull-based HACS distribution;")
    print("accepted limitation — docs/delivery-metrics.md).")


if __name__ == "__main__":
    main()
