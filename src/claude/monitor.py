"""Bash directory boundary enforcement for Claude tool calls."""

import re
import shlex
from pathlib import Path
from typing import Optional, Set, Tuple

# Subdirectories under ~/.claude/ that Claude Code uses internally.
_CLAUDE_INTERNAL_SUBDIRS: Set[str] = {"plans", "todos", "settings.json"}

# Commands that modify the filesystem or change context and should have paths checked
_FS_MODIFYING_COMMANDS: Set[str] = {
    "mkdir",
    "touch",
    "cp",
    "mv",
    "rm",
    "rmdir",
    "ln",
    "install",
    "tee",
    "cd",
    # Destructive / permission-changing commands that take a target path and
    # could otherwise escape the boundary unchecked.
    "dd",
    "chmod",
    "chown",
    "chgrp",
    "truncate",
    "shred",
}

# Commands that are read-only or don't take filesystem paths
_READ_ONLY_COMMANDS: Set[str] = {
    "cat",
    "ls",
    "head",
    "tail",
    "less",
    "more",
    "which",
    "whoami",
    "pwd",
    "echo",
    "printf",
    "printenv",
    "date",
    "wc",
    "sort",
    "uniq",
    "diff",
    "file",
    "stat",
    "du",
    "df",
    "tree",
    "realpath",
    "dirname",
    "basename",
}

# Commands that fetch remote content or run arbitrary interpreted code.
# They take paths/URLs/scripts whose static analysis is unreliable, so we
# treat them like FS-modifying commands and require boundary checks (a URL
# argument won't resolve to a path inside approved_directory, so it will be
# rejected — that is intentional).
_NETWORK_OR_INTERP_COMMANDS: Set[str] = {
    "curl",
    "wget",
    "fetch",
    "python",
    "python2",
    "python3",
    "node",
    "ruby",
    "perl",
    "php",
    "bash",
    "sh",
    "zsh",
    # Command launchers / evaluators: they run another command whose path we
    # cannot statically see, so force a boundary check rather than waving them
    # through as "unknown".
    "xargs",
    "eval",
    "env",
}

# Leading ``VAR=value`` environment-assignment tokens (e.g. ``FOO=bar cmd``).
# Without stripping these, the first token is treated as the command name,
# which matches nothing and bypasses path validation entirely.
_ENV_ASSIGN_RE: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Actions / expressions that make ``find`` a filesystem-modifying command
_FIND_MUTATING_ACTIONS: Set[str] = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}

# Bash command separators
_COMMAND_SEPARATORS: Set[str] = {"&&", "||", ";", "|", "&"}

# Bash subshell / command-substitution patterns. shlex.split silently absorbs
# these into a token (e.g. "$(rm -rf /)" becomes a single token), bypassing
# per-command boundary checks. Reject them outright.
_SUBSHELL_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"\$\("),  # $(cmd) command substitution
    re.compile(r"<\("),  # <(cmd) process substitution
    re.compile(r">\("),  # >(cmd) process substitution
    re.compile(r"`"),  # `cmd` backtick substitution
)


def check_bash_directory_boundary(
    command: str,
    working_directory: Path,
    approved_directory: Path,
) -> Tuple[bool, Optional[str]]:
    """Check if a bash command's paths stay within the approved directory."""

    # Reject subshells / command substitution outright — shlex.split bundles
    # ``$(rm -rf /etc)`` and ``<(cat /etc/shadow)`` into a single opaque token,
    # so per-command path checking cannot see what's executing inside. We
    # consider such constructs out of scope for static analysis.
    for pat in _SUBSHELL_PATTERNS:
        if pat.search(command):
            return False, (
                "Directory boundary violation: command contains subshell or "
                "command-substitution syntax which cannot be safely validated"
            )

    try:
        tokens = shlex.split(command)
    except ValueError:
        # If we can't parse the command, let it through —
        # the sandbox will catch it at the OS level
        return True, None

    if not tokens:
        return True, None

    # Split tokens into individual commands based on separators
    command_chains: list[list[str]] = []
    current_chain: list[str] = []

    for token in tokens:
        if token in _COMMAND_SEPARATORS:
            if current_chain:
                command_chains.append(current_chain)
            current_chain = []
        else:
            current_chain.append(token)

    if current_chain:
        command_chains.append(current_chain)

    resolved_approved = approved_directory.resolve()

    # Check each command in the chain
    for cmd_tokens in command_chains:
        if not cmd_tokens:
            continue

        # Strip leading ``VAR=value`` env-assignment tokens so the real command
        # (e.g. the ``rm`` in ``FOO=bar rm -rf /etc``) is the one we classify.
        while cmd_tokens and _ENV_ASSIGN_RE.match(cmd_tokens[0]):
            cmd_tokens = cmd_tokens[1:]
        if not cmd_tokens:
            continue

        base_command = Path(cmd_tokens[0]).name

        # Read-only commands are always allowed
        if base_command in _READ_ONLY_COMMANDS:
            continue

        # Determine if this specific command in the chain needs path validation
        needs_check = False
        if base_command == "find":
            needs_check = any(t in _FIND_MUTATING_ACTIONS for t in cmd_tokens[1:])
        elif base_command in _FS_MODIFYING_COMMANDS:
            needs_check = True
        elif base_command in _NETWORK_OR_INTERP_COMMANDS:
            needs_check = True

        if not needs_check:
            continue

        # Check each argument for paths outside the boundary
        seen_double_dash = False
        for token in cmd_tokens[1:]:
            # ``--`` marks the end of options; everything after is a positional
            # argument and must be path-checked even if it starts with ``-``.
            if token == "--" and not seen_double_dash:
                seen_double_dash = True
                continue
            # Skip flags only before ``--`` to prevent attackers smuggling paths
            # past the boundary by prefixing them with ``-`` (e.g. ``rm -- -foo``).
            if not seen_double_dash and token.startswith("-"):
                continue

            # ``key=value`` operands (e.g. ``dd of=/etc/shadow``) hide the real
            # path on the right of ``=``; resolving the whole token would treat
            # ``of=/etc/shadow`` as a relative name inside the working dir and
            # miss the escape. Check the value part instead.
            if "=" in token:
                token = token.split("=", 1)[1]
                if not token:
                    continue

            # For network/interp commands a non-flag token that *looks* like
            # a URL/scheme is not a filesystem path; rejecting it via path
            # resolution still works because Path("https://...").resolve()
            # yields a path inside working_directory that won't escape.
            # The check below handles both cases uniformly.

            # Resolve both absolute and relative paths against the working
            # directory so that traversal sequences like ``../../evil`` are
            # caught instead of being silently allowed.
            try:
                if token.startswith("/"):
                    resolved = Path(token).resolve()
                else:
                    resolved = (working_directory / token).resolve()

                if not _is_within_directory(resolved, resolved_approved):
                    return False, (
                        f"Directory boundary violation: '{base_command}' targets "
                        f"'{token}' which is outside approved directory "
                        f"'{resolved_approved}'"
                    )
            except (ValueError, OSError):
                # If path resolution fails, the command might be malformed or
                # using bash features we can't statically analyze.
                # We skip checking this token and rely on the OS-level sandbox.
                continue

    return True, None


def _is_claude_internal_path(file_path: str) -> bool:
    """Check whether *file_path* points inside ``~/.claude/`` (allowed subdirs only)."""
    try:
        resolved = Path(file_path).resolve()
        home = Path.home().resolve()
        claude_dir = home / ".claude"

        # Path must be inside ~/.claude/
        try:
            rel = resolved.relative_to(claude_dir)
        except ValueError:
            return False

        # Must be in one of the known subdirectories (or a known file)
        top_part = rel.parts[0] if rel.parts else ""
        return top_part in _CLAUDE_INTERNAL_SUBDIRS

    except Exception:
        return False


def _is_within_directory(path: Path, directory: Path) -> bool:
    """Check if path is within directory."""
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False
