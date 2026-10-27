"""Git integration for safe repository operations."""

import asyncio
import contextlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set, Tuple

from src.config.settings import Settings
from src.exceptions import SecurityError

logger = logging.getLogger(__name__)

# Turn off core.quotePath for every invocation. With it on (git's default),
# non-ASCII paths come back C-quoted ("\320\244\320\260\320\271\320\273"), so
# status and diff showed users mangled filenames that matched nothing on disk.
# Passing it through GIT_CONFIG_* rather than "git -c ..." keeps the command
# array — and therefore the SAFE_COMMANDS/DANGEROUS_PATTERNS checks below —
# unchanged. Git before 2.31 ignores these variables, i.e. degrades to today's
# behaviour rather than failing.
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.quotePath",
    "GIT_CONFIG_VALUE_0": "false",
}


class GitError(Exception):
    """Git operation error."""


@dataclass
class GitStatus:
    """Git repository status."""

    branch: str
    modified: List[str]
    added: List[str]
    deleted: List[str]
    untracked: List[str]
    ahead: int
    behind: int

    @property
    def is_clean(self) -> bool:
        """Check if working directory is clean."""
        return not any([self.modified, self.added, self.deleted, self.untracked])


@dataclass
class CommitInfo:
    """Git commit information."""

    hash: str
    author: str
    date: datetime
    message: str
    files_changed: int
    insertions: int
    deletions: int


class GitIntegration:
    """Safe git integration for repositories."""

    # Safe git commands allowed
    SAFE_COMMANDS: Set[str] = {
        "status",
        "log",
        "diff",
        "branch",
        "remote",
        "show",
        "ls-files",
        "ls-tree",
        "rev-parse",
        "rev-list",
        "describe",
    }

    # Hard wall-clock limit per git invocation. Without it a hung git process
    # (e.g. blocked on a credential/network prompt) waits forever.
    COMMAND_TIMEOUT: float = 30.0

    # Dangerous patterns to block
    DANGEROUS_PATTERNS = [
        r"--exec",
        r"--upload-pack",
        r"--receive-pack",
        r"-c\s*core\.gitProxy",
        r"-c\s*core\.sshCommand",
    ]

    # Options that take an otherwise read-only subcommand outside the
    # repository: ``diff --no-index A B`` prints any two files on the host,
    # ``--output=FILE`` writes the result wherever it is told.
    #
    # Commands set in the repository's own .git/config (diff drivers, textconv,
    # clean filters, core.fsmonitor) run under these read-only forms too, with
    # or without ``--ext-diff``/``--textconv``: ``git diff`` applies them by
    # default and ``--no-ext-diff --no-textconv`` still leave the clean filter.
    # No option list closes that, so repo config is trusted exactly as a git
    # client in a terminal trusts it; a repository copied in together with a
    # foreign .git directory is as unsafe here as there.
    FORBIDDEN_OPTIONS = ("--no-index", "--output")

    # ``branch`` and ``remote`` only list with these arguments. Any other form
    # creates, renames or deletes a ref or a remote (``branch -D``, ``remote
    # remove``) or talks to the network (``remote update``).
    READ_ONLY_ARGUMENTS = {
        "branch": {
            "--show-current",
            "--list",
            "-l",
            "-a",
            "--all",
            "-r",
            "--remotes",
            "-v",
            "-vv",
            "--verbose",
        },
        "remote": {"-v", "--verbose"},
    }

    def __init__(self, settings: Settings):
        """Initialize git integration.

        Args:
            settings: Application settings
        """
        self.settings = settings
        # Resolved once: the boundary check below compares against a resolved
        # cwd, so a relative or symlinked APPROVED_DIRECTORY would otherwise
        # never match (SecurityValidator resolves both sides too).
        self.approved_dir = Path(settings.approved_directory).resolve()

    async def execute_git_command(
        self, command: List[str], cwd: Path
    ) -> Tuple[str, str]:
        """Execute safe git command.

        Args:
            command: Git command parts
            cwd: Working directory

        Returns:
            Tuple of (stdout, stderr)

        Raises:
            SecurityError: If command is unsafe
            GitError: If git command fails
        """
        # Validate command safety
        if not command or command[0] != "git":
            raise SecurityError("Only git commands allowed")

        subcommand = command[1] if len(command) > 1 else ""
        if subcommand not in self.SAFE_COMMANDS:
            raise SecurityError(f"Unsafe git command: {subcommand}")
        self._validate_arguments(subcommand, command[2:])

        # Check for dangerous patterns
        cmd_str = " ".join(command)
        for pattern in self.DANGEROUS_PATTERNS:
            if re.search(pattern, cmd_str, re.IGNORECASE):
                raise SecurityError(f"Dangerous pattern detected: {pattern}")

        # Validate working directory
        try:
            cwd = cwd.resolve()
        except Exception:
            raise SecurityError("Invalid repository path")
        if not cwd.is_relative_to(self.approved_dir):
            raise SecurityError("Repository outside approved directory")

        # Execute command
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=_GIT_ENV,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.COMMAND_TIMEOUT
            )

            if process.returncode != 0:
                raise GitError(f"Git command failed: {stderr.decode()}")

            return (
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            )

        except asyncio.TimeoutError:
            raise GitError("Git command timed out")
        except GitError:
            # Git's own refusal (non-zero exit, e.g. no upstream configured) is
            # an expected outcome: pass it on as raised instead of logging it
            # as an error and wrapping its message a second time.
            raise
        except Exception as e:
            logger.error(f"Git command error: {e}")
            raise GitError(f"Failed to execute git command: {e}")
        finally:
            # Any exit before git finished — the timeout, a cancelled task
            # (CancelledError is not an Exception), a failed communicate() —
            # would otherwise leave the git process running as an orphan.
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    def _validate_arguments(self, subcommand: str, args: List[str]) -> None:
        """Reject arguments that make an allowed subcommand unsafe.

        Raises:
            SecurityError: If an argument is not read-only for *subcommand*
        """
        read_only = self.READ_ONLY_ARGUMENTS.get(subcommand)
        if read_only is not None:
            for arg in args:
                if arg not in read_only:
                    raise SecurityError(f"Unsafe git {subcommand} argument: {arg}")
            return

        # Everything after ``--`` is a pathspec, not an option. Git accepts any
        # unambiguous prefix of a long option (``--no-ind``, ``--outp=FILE``),
        # so a forbidden option is matched by prefix, not by its full name.
        options = args[: args.index("--")] if "--" in args else args
        for option in options:
            name = option.split("=", 1)[0]
            if len(name) > 2 and any(
                forbidden.startswith(name) for forbidden in self.FORBIDDEN_OPTIONS
            ):
                raise SecurityError(f"Unsafe git option: {option}")

    async def get_status(self, repo_path: Path) -> GitStatus:
        """Get repository status.

        Args:
            repo_path: Repository path

        Returns:
            Git status information
        """
        # Get branch and tracking info
        branch_out, _ = await self.execute_git_command(
            ["git", "branch", "--show-current"], repo_path
        )
        branch = branch_out.strip() or "HEAD"

        # Get file status. ``-z`` ends every entry with NUL and prints paths
        # verbatim. The line format C-quotes a name holding a quote, backslash
        # or control character and writes a rename as "old -> new", which a
        # file literally named "a -> b" cannot be told apart from.
        status_out, _ = await self.execute_git_command(
            ["git", "status", "--porcelain=v1", "-z"], repo_path
        )

        modified = []
        added = []
        deleted = []
        untracked = []

        # Split on NUL only, never strip: a leading space is the index column
        # of the first entry (" M file"), and paths may end in whitespace.
        entries = status_out.split("\0")
        position = 0
        while position < len(entries):
            entry = entries[position]
            position += 1
            if not entry:
                continue

            # Porcelain v1: XY then a space then the path. X is the index
            # (staged) column, Y the worktree column.
            index_col = entry[0]
            worktree_col = entry[1]
            path = entry[3:]

            if index_col == "?" and worktree_col == "?":
                untracked.append(path)
            elif "R" in (index_col, worktree_col) or "C" in (
                index_col,
                worktree_col,
            ):
                # Rename or copy: tree changed — record the new path. Treated
                # as a modification so is_clean reports dirty. ``-z`` reverses
                # the line format's "old -> new": this entry holds the new path
                # and the original follows as an entry of its own; skip it.
                modified.append(path)
                position += 1
            elif "D" in (index_col, worktree_col):
                deleted.append(path)
            elif "A" in (index_col, worktree_col):
                added.append(path)
            elif "M" in (index_col, worktree_col):
                modified.append(path)
            else:
                # Catch-all for type-change (T), unmerged (U) and any other
                # non-space status so the tree is never silently "clean".
                modified.append(path)

        # Get ahead/behind counts
        ahead = behind = 0
        try:
            # Try to get upstream tracking info
            rev_out, _ = await self.execute_git_command(
                ["git", "rev-list", "--count", "--left-right", "HEAD...@{upstream}"],
                repo_path,
            )
            # With --count the output is two bare numbers, "ahead<TAB>behind";
            # the "<"/">" marks of --left-right belong to listed commits only.
            if rev_out.strip():
                parts = rev_out.strip().split("\t")
                if len(parts) == 2:
                    ahead = int(parts[0])
                    behind = int(parts[1])
        except GitError:
            # No upstream configured
            pass

        return GitStatus(
            branch=branch,
            modified=modified,
            added=added,
            deleted=deleted,
            untracked=untracked,
            ahead=ahead,
            behind=behind,
        )

    async def get_diff(
        self, repo_path: Path, staged: bool = False, file_path: Optional[str] = None
    ) -> str:
        """Get repository diff.

        Args:
            repo_path: Repository path
            staged: Show staged changes
            file_path: Specific file to diff

        Returns:
            Formatted diff output
        """
        command = ["git", "diff"]

        if staged:
            command.append("--staged")

        # Add formatting options
        command.extend(["--no-color", "--minimal"])

        if file_path:
            # Validate file path. Both sides resolved: a relative or symlinked
            # repo_path would otherwise reject every legitimate file.
            file_path_obj = (repo_path / file_path).resolve()
            if not file_path_obj.is_relative_to(repo_path.resolve()):
                raise SecurityError("File path outside repository")
            # ``--`` ends option parsing so a dashed file name (e.g.
            # ``--output=...``) is treated as a pathspec, not a git flag.
            command.append("--")
            command.append(file_path)

        diff_out, _ = await self.execute_git_command(command, repo_path)

        if not diff_out.strip():
            return "No changes to show"

        # Format diff with indicators
        lines = []
        for line in diff_out.split("\n"):
            if line.startswith("+") and not line.startswith("+++"):
                lines.append(f"➕ {line[1:]}")
            elif line.startswith("-") and not line.startswith("---"):
                lines.append(f"➖ {line[1:]}")
            elif line.startswith("@@"):
                lines.append(f"📍 {line}")
            else:
                lines.append(line)

        return "\n".join(lines)

    async def get_file_history(
        self, repo_path: Path, file_path: str, limit: int = 10
    ) -> List[CommitInfo]:
        """Get file commit history.

        Args:
            repo_path: Repository path
            file_path: File to get history for
            limit: Maximum commits to return

        Returns:
            List of commit information
        """
        # Validate file path (both sides resolved, as in get_diff)
        file_path_obj = (repo_path / file_path).resolve()
        if not file_path_obj.is_relative_to(repo_path.resolve()):
            raise SecurityError("File path outside repository")

        # Get commit log with stats
        log_out, _ = await self.execute_git_command(
            [
                "git",
                "log",
                f"--max-count={limit}",
                # \x1f (unit separator) instead of '|': a commit subject may
                # legitimately contain a pipe, which would make the field count
                # wrong and silently drop the commit from the history.
                "--pretty=format:%H%x1f%an%x1f%aI%x1f%s",
                "--numstat",
                "--",
                file_path,
            ],
            repo_path,
        )

        commits = []
        current_commit = None

        for line in log_out.strip().split("\n"):
            if not line:
                continue

            parts = line.split("\x1f", 3)
            if len(parts) == 4:
                # Commit info line

                if current_commit:
                    commits.append(current_commit)

                current_commit = CommitInfo(
                    hash=parts[0][:8],  # Short hash
                    author=parts[1],
                    date=datetime.fromisoformat(parts[2].replace("Z", "+00:00")),
                    message=parts[3],
                    files_changed=0,
                    insertions=0,
                    deletions=0,
                )
            elif current_commit and "\t" in line:
                # Numstat line
                parts = line.split("\t")
                if len(parts) == 3:
                    try:
                        insertions = int(parts[0]) if parts[0] != "-" else 0
                        deletions = int(parts[1]) if parts[1] != "-" else 0
                        current_commit.insertions += insertions
                        current_commit.deletions += deletions
                        current_commit.files_changed += 1
                    except ValueError:
                        pass

        if current_commit:
            commits.append(current_commit)

        return commits

    def format_status(self, status: GitStatus) -> str:
        """Format git status for display.

        Args:
            status: Git status object

        Returns:
            Formatted status string
        """
        lines = [f"🌿 Branch: {status.branch}"]

        # Add tracking info
        if status.ahead or status.behind:
            tracking = []
            if status.ahead:
                tracking.append(f"↑{status.ahead}")
            if status.behind:
                tracking.append(f"↓{status.behind}")
            lines.append(f"📊 Tracking: {' '.join(tracking)}")

        if status.is_clean:
            lines.append("✅ Working tree clean")
        else:
            if status.modified:
                lines.append(f"📝 Modified: {len(status.modified)} files")
                for f in status.modified[:5]:  # Show first 5
                    lines.append(f"  • {f}")
                if len(status.modified) > 5:
                    lines.append(f"  ... and {len(status.modified) - 5} more")

            if status.added:
                lines.append(f"➕ Added: {len(status.added)} files")
                for f in status.added[:5]:
                    lines.append(f"  • {f}")
                if len(status.added) > 5:
                    lines.append(f"  ... and {len(status.added) - 5} more")

            if status.deleted:
                lines.append(f"➖ Deleted: {len(status.deleted)} files")
                for f in status.deleted[:5]:
                    lines.append(f"  • {f}")
                if len(status.deleted) > 5:
                    lines.append(f"  ... and {len(status.deleted) - 5} more")

            if status.untracked:
                lines.append(f"❓ Untracked: {len(status.untracked)} files")
                for f in status.untracked[:5]:
                    lines.append(f"  • {f}")
                if len(status.untracked) > 5:
                    lines.append(f"  ... and {len(status.untracked) - 5} more")

        return "\n".join(lines)

    def format_history(self, commits: List[CommitInfo]) -> str:
        """Format commit history for display.

        Args:
            commits: List of commits

        Returns:
            Formatted history string
        """
        if not commits:
            return "No commit history found"

        lines = ["📜 Commit History:"]

        for commit in commits:
            lines.append(
                f"\n🔹 {commit.hash} - {commit.date.strftime('%Y-%m-%d %H:%M')}"
            )
            lines.append(f"   👤 {commit.author}")
            lines.append(f"   💬 {commit.message}")

            if commit.files_changed:
                stats = []
                if commit.insertions:
                    stats.append(f"+{commit.insertions}")
                if commit.deletions:
                    stats.append(f"-{commit.deletions}")
                lines.append(
                    f"   📊 {commit.files_changed} files changed, {' '.join(stats)}"
                )

        return "\n".join(lines)
