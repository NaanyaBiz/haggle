"""The commit-msg provenance hook (`scripts/check_provenance_trailer.sh`, #283).

Executes the shipped script against sample messages. Any AI tool's
`Co-Authored-By` trailer passes, an explicit `AI-Assisted: none` passes,
a message with neither is rejected, and merge commits are skipped. The
hook is vendor-agnostic on purpose: a contributor using a different tool
must be able to declare it truthfully instead of bypassing the hook.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

SCRIPT = (
    pathlib.Path(__file__).parent.parent / "scripts" / "check_provenance_trailer.sh"
)


def _run(
    tmp_path: pathlib.Path, message: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    msg_file = tmp_path / "COMMIT_EDITMSG"
    msg_file.write_text(message)
    return subprocess.run(
        [str(SCRIPT), str(msg_file)],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        check=False,
    )


@pytest.mark.parametrize(
    "trailer",
    [
        "Co-Authored-By: Claude <noreply@anthropic.com>",
        "Co-Authored-By: DeepSeek <noreply@deepseek.com>",
        "co-authored-by: Codex <codex@openai.com>",
        "AI-Assisted: none",
        "ai-assisted: None",
    ],
)
def test_declared_provenance_passes(tmp_path: pathlib.Path, trailer: str) -> None:
    result = _run(tmp_path, f"fix: something\n\nBody.\n\n{trailer}\n")
    assert result.returncode == 0, result.stderr


def test_missing_trailer_is_rejected(tmp_path: pathlib.Path) -> None:
    result = _run(tmp_path, "fix: something\n\nBody with no trailer.\n")
    assert result.returncode == 1
    assert "no provenance trailer" in result.stderr
    assert "AI-Assisted: none" in result.stderr


@pytest.mark.parametrize(
    "bad",
    [
        "Co-Authored-By:",  # key with no value
        "Co-Authored-By:   ",
        "AI-Assisted: Claude",  # a tool belongs in Co-Authored-By, not here
        "AI-Assisted: yes",
        "Body mentions Co-Authored-By: Claude mid-line",  # not a trailer line
    ],
)
def test_malformed_declarations_are_rejected(tmp_path: pathlib.Path, bad: str) -> None:
    result = _run(tmp_path, f"fix: something\n\n{bad}\n")
    assert result.returncode == 1, result.stderr


def test_merge_commit_subject_is_skipped(tmp_path: pathlib.Path) -> None:
    result = _run(tmp_path, "Merge branch 'main' into feature\n")
    assert result.returncode == 0


def test_merge_reflog_action_is_skipped(tmp_path: pathlib.Path) -> None:
    result = _run(
        tmp_path, "resolved conflicts\n", env={"GIT_REFLOG_ACTION": "merge origin/main"}
    )
    assert result.returncode == 0


def test_script_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)
