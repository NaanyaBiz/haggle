"""The release.yml symlink guard, executed as the literal workflow text (#246).

release.yml is tag-triggered, so CI can never exercise the guard before a
release. This test extracts the actual `run:` block of the "Build release
artifact" step — not a copy of it — and executes the guard portion in a
sandbox, so a workflow edit that breaks the guard fails here first.
"""

from __future__ import annotations

import pathlib
import subprocess

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release.yml"


def _guard_script() -> str:
    """The build step's run block, truncated before the zip invocation."""
    doc = yaml.safe_load(_WORKFLOW.read_text())
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == "Build release artifact":
                run = step["run"]
                lines = []
                for line in run.splitlines():
                    # Cut at the zip INVOCATION, not any mention of "zip -r"
                    # (the guard's own comments discuss the flag).
                    if line.lstrip().startswith("(cd custom_components/haggle"):
                        break
                    lines.append(line)
                script = "\n".join(lines)
                assert "find custom_components/haggle -type l" in script, (
                    "guard not found in the build step — test needs updating"
                )
                return script
    raise AssertionError("Build release artifact step not found in release.yml")


def _run_guard(tmp_path: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-e", "-c", _guard_script()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_guard_fails_build_when_symlink_present(tmp_path: pathlib.Path) -> None:
    tree = tmp_path / "custom_components" / "haggle"
    tree.mkdir(parents=True)
    (tree / "manifest.json").write_text("{}")
    (tmp_path / "outside.txt").write_text("outside the tree")
    (tree / "evil.json").symlink_to("../../outside.txt")

    result = _run_guard(tmp_path)

    assert result.returncode == 1
    assert "refusing to build" in result.stdout + result.stderr
    assert "evil.json" in result.stdout + result.stderr


def test_guard_passes_clean_tree(tmp_path: pathlib.Path) -> None:
    tree = tmp_path / "custom_components" / "haggle"
    tree.mkdir(parents=True)
    (tree / "manifest.json").write_text("{}")

    result = _run_guard(tmp_path)

    assert result.returncode == 0


def test_guard_fails_when_tree_missing(tmp_path: pathlib.Path) -> None:
    """find on a missing dir must abort the build, not silently pass."""
    result = _run_guard(tmp_path)

    assert result.returncode == 1
