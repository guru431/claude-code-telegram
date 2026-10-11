"""Tests for GitIntegration: path validation, the command gate, status parsing."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bot.features.git_integration import GitError, GitIntegration
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


async def test_status_parses_nul_terminated_entries(tmp_path: Path) -> None:
    """``git status -z`` output: verbatim paths, renames as two entries.

    Regressions covered: the output used to be ``strip()``-ed, which ate the
    index column of a leading " M" entry and with it the first character of the
    path; and a rename was split on " -> ", which a file may contain literally.
    """
    git = _git(tmp_path)
    status = (
        " M README.md\0"
        "M  src/staged.py\0"
        "R  new name.py\0old name.py\0"
        'A  with "quote".txt\0'
        " D a -> b.txt\0"
        "?? notes.txt\0"
    )
    git.execute_git_command = AsyncMock(  # type: ignore[method-assign]
        side_effect=[("main\n", ""), (status, ""), ("1\t2\n", "")]
    )

    result = await git.get_status(tmp_path)

    assert result.modified == ["README.md", "src/staged.py", "new name.py"]
    assert result.added == ['with "quote".txt']
    assert result.deleted == ["a -> b.txt"]
    assert result.untracked == ["notes.txt"]
    assert (result.ahead, result.behind) == (1, 2)


class TestCommandGate:
    """``execute_git_command`` is the last check before git is spawned."""

    @pytest.fixture
    def git(self, tmp_path: Path) -> GitIntegration:
        return GitIntegration(SimpleNamespace(approved_directory=tmp_path))  # type: ignore[arg-type]

    @pytest.fixture
    def spawn(self, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
        """Stand-in for the git subprocess (exit code 0, empty output)."""
        process = SimpleNamespace(
            returncode=0, communicate=AsyncMock(return_value=(b"", b""))
        )
        spawn = AsyncMock(return_value=process)
        monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
        return spawn

    @pytest.mark.parametrize(
        "command",
        [
            ["git"],
            ["git", "push"],
            # Reads any two files on the host, repository or not.
            ["git", "diff", "--no-index", "/etc/shadow", "/dev/null"],
            # Writes the result to an arbitrary path.
            ["git", "log", "--output=/tmp/x"],
            ["git", "show", "--output", "/tmp/x"],
            # Git accepts unambiguous prefixes of long options.
            ["git", "diff", "--no-ind", "a", "b"],
            ["git", "log", "--outp=/tmp/x"],
            # branch/remote are listings only.
            ["git", "branch", "-D", "main"],
            ["git", "branch", "new-branch"],
            ["git", "remote", "remove", "origin"],
            ["git", "remote", "update"],
        ],
    )
    async def test_rejects_unsafe_forms_of_allowed_subcommands(
        self, git: GitIntegration, spawn: AsyncMock, tmp_path: Path, command: list[str]
    ) -> None:
        with pytest.raises(SecurityError):
            await git.execute_git_command(command, tmp_path)

        spawn.assert_not_awaited()

    @pytest.mark.parametrize(
        "command",
        [
            ["git", "branch", "--show-current"],
            ["git", "remote", "-v"],
            ["git", "status", "--porcelain=v1", "-z"],
            ["git", "diff", "--staged", "--no-color", "--minimal"],
            # After ``--`` it is a pathspec, whatever it looks like.
            ["git", "diff", "--", "--output=notes.txt"],
            ["git", "log", "--max-count=10", "--numstat", "--", "."],
        ],
    )
    async def test_allows_the_read_only_forms_in_use(
        self, git: GitIntegration, spawn: AsyncMock, tmp_path: Path, command: list[str]
    ) -> None:
        assert await git.execute_git_command(command, tmp_path) == ("", "")

    async def test_git_refusal_is_not_wrapped_twice(
        self,
        git: GitIntegration,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A non-zero exit is an expected answer, not an error to log and rewrap."""
        process = SimpleNamespace(
            returncode=128,
            communicate=AsyncMock(return_value=(b"", b"fatal: no upstream")),
        )
        monkeypatch.setattr(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=process)
        )

        with pytest.raises(GitError) as raised:
            await git.execute_git_command(["git", "rev-list", "HEAD"], tmp_path)

        assert str(raised.value) == "Git command failed: fatal: no upstream"
        assert caplog.records == []
