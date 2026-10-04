"""Tests for GitIntegration path validation."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.features.git_integration import GitIntegration
from src.exceptions import SecurityError


@pytest.fixture
def relative_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository addressed by a relative path, as a caller may pass it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(tmp_path)
    return Path("repo")


def _git(approved: Path) -> GitIntegration:
    git = GitIntegration(SimpleNamespace(approved_directory=approved))  # type: ignore[arg-type]
    git.execute_git_command = AsyncMock(return_value=("", ""))  # type: ignore[method-assign]
    return git


async def test_file_history_accepts_file_in_relative_repo(
    relative_repo: Path, tmp_path: Path
) -> None:
    """Regression: resolved file vs unresolved repo_path raised SecurityError."""
    git = _git(tmp_path)
    assert await git.get_file_history(relative_repo, ".") == []
    assert await git.get_file_history(relative_repo, "src/a.py") == []


async def test_diff_accepts_file_in_relative_repo(
    relative_repo: Path, tmp_path: Path
) -> None:
    git = _git(tmp_path)
    assert await git.get_diff(relative_repo, file_path="src/a.py") == (
        "No changes to show"
    )


async def test_file_outside_repo_still_rejected(
    relative_repo: Path, tmp_path: Path
) -> None:
    git = _git(tmp_path)
    with pytest.raises(SecurityError):
        await git.get_file_history(relative_repo, "../outside.txt")
    with pytest.raises(SecurityError):
        await git.get_diff(relative_repo, file_path="../outside.txt")
