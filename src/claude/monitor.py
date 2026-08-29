"""Bash directory boundary enforcement for Claude tool calls."""

import os
import re
import shlex
from pathlib import Path
from typing import Optional, Set, Tuple

from src.security.validators import SecurityValidator

# Subdirectories under ~/.claude/ that Claude Code uses internally.
# NOTE: settings.json is intentionally excluded — writing ~/.claude/settings.json
# allows arbitrary hook execution, so it must fall through to validate_path.
_CLAUDE_INTERNAL_SUBDIRS: Set[str] = {"plans", "todos"}

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
    # Archive tool that creates/extracts files; its ``-C``/``-f`` path flags are
    # declared in ``_PATH_BEARING_FLAGS`` but stay dead unless ``tar`` reaches the
    # path-check loop, so classify it here (``tar xf a -C /outside`` must be
    # boundary-checked when the OS sandbox is disabled).
    "tar",
}

# Read-only commands that take filesystem-path arguments. They don't modify
# anything, but reading outside the approved root (e.g. ``cat /etc/passwd``,
# ``ls /root``) is still an information-disclosure escape if the OS sandbox is
# disabled or bypassed. Their path arguments are boundary-checked just like the
# FS-modifying commands.
_READ_ONLY_PATH_COMMANDS: Set[str] = {
    "cat",
    "ls",
    "head",
    "tail",
    "less",
    "more",
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
    # Text search / stream processors that take file-path operands — a classic
    # way to read files outside the root (e.g. ``grep x /etc/passwd``).
    "grep",
    "egrep",
    "fgrep",
    "rg",
    "ag",
    "sed",
    "awk",
    "gawk",
    "nawk",
    # Dump / slice / encode utilities that read a named file.
    "cut",
    "nl",
    "tac",
    "comm",
    "join",
    "paste",
    "fold",
    "fmt",
    "expand",
    "unexpand",
    "od",
    "xxd",
    "hexdump",
    "strings",
    "base64",
    # Checksums over a named file.
    "md5sum",
    "sha1sum",
    "sha256sum",
    "sha512sum",
    "cksum",
    "sum",
}

# Read-only commands that take no filesystem path (or only manipulate strings).
# Nothing to boundary-check, so they're always allowed.
_READ_ONLY_NO_PATH_COMMANDS: Set[str] = {
    "which",
    "whoami",
    "pwd",
    "echo",
    "printf",
    "printenv",
    "date",
    "dirname",
    "basename",
}

# Union kept for any external reference; the two subsets above drive the logic.
_READ_ONLY_COMMANDS: Set[str] = _READ_ONLY_PATH_COMMANDS | _READ_ONLY_NO_PATH_COMMANDS

# Commands that fetch remote content or run arbitrary interpreted code.
# They take paths/URLs/scripts whose static analysis is unreliable, so we
# treat them like FS-modifying commands and require boundary checks. A
# ``scheme://`` argument resolves to a literal subdir *inside* the working dir
# (so a naive path check would wrongly pass it); it is rejected explicitly via
# ``_URL_SCHEME_RE`` in the token loop below.
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
    # ``source``/``.`` read and execute a script file in the current shell. Its
    # path operand must stay inside the boundary (e.g. ``source /etc/passwd``).
    "source",
    ".",
    # ``eval`` runs a code string assembled at runtime; it is denied outright
    # below (see ``_CODE_STRING_COMMANDS``) rather than path-checked.
    "eval",
}

# Commands whose *positional operand* is a shell code string, not a path.
# Resolving that string against the working directory falsely "passes" the
# boundary check (``eval "rm -rf /etc"`` resolves to a literal name inside the
# approved dir), so an invocation carrying any operand is denied outright — the
# same reasoning as ``_INTERP_COMMANDS``/``_INLINE_CODE_FLAGS`` below.
_CODE_STRING_COMMANDS: Set[str] = {"eval"}

# Commands whose *explicit path flags* are boundary-checked, but whose
# positional operands are not paths (``git commit -m "…"``, ``npm install pkg``,
# ``poetry add pkg``). They are excluded from the OS sandbox by default (see
# ``sandbox_excluded_commands``), so their path-bearing flags — ``git -C DIR``,
# ``npm --prefix DIR`` — are the one static check standing between them and a
# write outside the approved root.
_FLAG_PATH_ONLY_COMMANDS: Set[str] = {
    "git",
    "npm",
    "npx",
    "yarn",
    "pnpm",
    "poetry",
    "pip",
    "pip3",
}

# Interpreters that can execute an inline code string / opaque module supplied
# via a flag. The code is not a filesystem path, so resolving it against the
# working directory falsely "passes" the boundary check (e.g.
# ``python3 -c "import os; os.system(...)"`` resolves the code string to a name
# inside the approved dir). We deny these outright rather than relying solely on
# the OS sandbox.
_INTERP_COMMANDS: Set[str] = {
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
    "source",
    ".",
}

# Matches a ``scheme://`` prefix (http, https, ftp, file, …). Used to spot URL
# arguments to network commands, which are remote fetches rather than paths.
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")

# Flags that introduce inline code / opaque module execution across the
# interpreters above (python -c/-m, node -e/-p/--eval/--print,
# perl -e/-E/-n/-p, ruby -e, php -r/-R, bash/sh/zsh -c).
_INLINE_CODE_FLAGS: Set[str] = {
    "-c",
    "-e",
    "-E",
    "-n",
    "-p",
    "-r",
    "-R",
    "-m",
    "--eval",
    "--exec",
    "--print",
}

# Leading ``VAR=value`` environment-assignment tokens (e.g. ``FOO=bar cmd``).
# Without stripping these, the first token is treated as the command name,
# which matches nothing and bypasses path validation entirely.
_ENV_ASSIGN_RE: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Launcher wrappers that take *another command* as their operand (e.g.
# ``nohup rm -rf /etc``, ``timeout 5 cat /etc/passwd``, ``sudo cat
# /etc/shadow``). Classifying only the wrapper as the base command would leave
# the wrapped command (the one that actually touches the filesystem)
# unchecked, so we strip the wrapper and re-classify the next real command.
_LAUNCHER_WRAPPERS: Set[str] = {
    "nohup",
    "timeout",
    "nice",
    "command",
    "sudo",
    "doas",
    "stdbuf",
    "setsid",
    "ionice",
    "chrt",
    # ``exec <cmd>`` replaces the shell with the wrapped command; it has no
    # numeric/value operands, so strip it and re-classify the real command
    # (``exec cat /etc/passwd`` must check ``cat``'s path).
    "exec",
    # ``env``/``xargs`` also run *another* command. Classifying only the wrapper
    # left the wrapped command unchecked, so ``env python3 -c '…'`` and
    # ``xargs python3 -c '…'`` bypassed the inline-code denial that catches the
    # bare ``python3 -c '…'`` (and ``sudo python3 -c '…'``, which was caught only
    # because ``sudo`` is a wrapper). Strip them and classify what they launch.
    "env",
    "xargs",
}

# Wrappers that take a numeric/value operand of their own *before* the wrapped
# command (e.g. ``timeout 5 cmd``, ``nice -n 10 cmd``, ``ionice -c2 cmd``,
# ``chrt -f 99 cmd``). After consuming the wrapper's flags we also skip a
# single bare numeric operand so the real command lands as the base command.
_WRAPPERS_WITH_NUMERIC_OPERAND: Set[str] = {"timeout", "nice", "ionice", "chrt"}

# Separated flags that take an argument of their own (the next token) for
# wrappers *outside* _WRAPPERS_WITH_NUMERIC_OPERAND. Without dropping the
# operand too, ``sudo -u user cat /etc/passwd`` leaves ``user`` as the base
# command and the real ``cat /etc/passwd`` is never checked.
_WRAPPER_OPERAND_FLAGS: dict[str, Set[str]] = {
    "sudo": {"-u", "-g", "-C", "-h", "-p", "-r", "-t", "-U", "-c"},
    "doas": {"-u", "-C"},
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "xargs": {
        "-I",
        "-i",
        "-n",
        "-P",
        "-a",
        "-d",
        "-E",
        "-e",
        "-L",
        "-s",
        "--replace",
        "--max-args",
        "--max-procs",
        "--arg-file",
        "--delimiter",
        "--eof",
        "--max-lines",
        "--max-chars",
    },
}


def _strip_launcher_wrappers(cmd_tokens: list[str]) -> list[str]:
    """Strip leading launcher wrappers (and their own flags/operands).

    Handles chained wrappers (``nohup timeout 5 nice -n 5 rm -rf /etc``) by
    looping until the leading token is no longer a recognized wrapper, so the
    returned list begins with the command that actually runs.
    """
    while cmd_tokens and Path(cmd_tokens[0]).name in _LAUNCHER_WRAPPERS:
        wrapper = Path(cmd_tokens[0]).name
        cmd_tokens = cmd_tokens[1:]
        # Skip the wrapper's own option flags (``-n``, ``-c2``, ``-f``, …) and
        # any standalone value that follows a separated flag (``-n 10``).
        while cmd_tokens and cmd_tokens[0].startswith("-"):
            flag = cmd_tokens[0]
            cmd_tokens = cmd_tokens[1:]
            # A separated numeric/value operand (``-n 10``, ``-c 2``) is the
            # flag's argument, not the command — drop it too. Bundled forms
            # (``-c2``) carry the value in the same token and need no extra skip.
            if (
                wrapper in _WRAPPERS_WITH_NUMERIC_OPERAND
                and flag in {"-n", "-c", "-p", "-i", "-r", "-b", "-k", "-s"}
                and cmd_tokens
                and not cmd_tokens[0].startswith("-")
            ):
                cmd_tokens = cmd_tokens[1:]
            # For non-numeric wrappers (``sudo -u user cmd``), drop the operand
            # that follows an argument-taking flag so the *wrapped* command — not
            # the flag's value — becomes the base command.
            elif (
                flag in _WRAPPER_OPERAND_FLAGS.get(wrapper, set())
                and cmd_tokens
                and not cmd_tokens[0].startswith("-")
            ):
                cmd_tokens = cmd_tokens[1:]
        # ``timeout``/``chrt`` take a bare positional operand (``timeout 5``,
        # ``chrt 99`` rare) before the command; drop a single leading numeric.
        if (
            wrapper in _WRAPPERS_WITH_NUMERIC_OPERAND
            and cmd_tokens
            and re.match(r"^\d+(\.\d+)?[a-zA-Z]?$", cmd_tokens[0])
        ):
            cmd_tokens = cmd_tokens[1:]
    return cmd_tokens


# Bash command separators
_COMMAND_SEPARATORS: Set[str] = {"&&", "||", ";", "|", "&"}

# Redirection operators. The token *following* one of these is a filesystem
# path the command writes to / reads from (e.g. ``echo x > /etc/cron.d/y``),
# and must be boundary-checked even when the lead command takes no path of its
# own (``echo``).
_REDIRECTION_OPERATORS: Set[str] = {
    ">",
    ">>",
    "<",
    "<>",
    ">|",
    "&>",
    "&>>",
    # fd duplication (``2>&1``): the operand is a file descriptor number, which
    # resolves inside the working directory and passes; a filename operand
    # (``>& /etc/x``, legal bash) is caught.
    ">&",
    "<&",
}

# Shell metacharacters that ``shlex.split`` does *not* separate on its own.
# ``shlex`` in whitespace-split mode keeps ``>/etc/x``, ``2>``, and ``hi&&rm``
# as single words, so the redirection scan below (which looks for a standalone
# operator token) never fired for the no-space forms and the chain splitter
# never saw a separator glued to a word. Both are the common way these are
# written, so normalize by inserting whitespace around every operator before
# tokenizing. Longest match first: ``&&`` must not be read as ``&`` + ``&``,
# ``>>`` not as ``>`` + ``>``.
_SHELL_OPERATORS: Tuple[str, ...] = (
    "&>>",
    "<<<",
    "&&",
    "||",
    "&>",
    ">>",
    "<<",
    ">|",
    "<>",
    ">&",
    "<&",
    ">",
    "<",
    "|",
    ";",
    "&",
)

# Character devices that are legitimate redirection targets even though they sit
# outside the approved root. ``2>/dev/null`` appears in a large share of ordinary
# commands and discarding output is not a boundary escape; block devices and
# everything else under /dev stay denied.
_ALLOWED_DEVICE_TARGETS: Set[str] = {
    "/dev/null",
    "/dev/zero",
    "/dev/full",
    "/dev/random",
    "/dev/urandom",
    "/dev/stdin",
    "/dev/stdout",
    "/dev/stderr",
    "/dev/tty",
}


def _normalize_shell_operators(line: str) -> str:
    """Insert whitespace around unquoted shell operators.

    ``echo pwn >/etc/cron.d/x`` and ``echo hi&&rm -rf /etc`` are single words to
    ``shlex.split``; after normalization they tokenize as the spaced forms do, so
    the redirection scan and the chain splitter see them. Quoted and
    backslash-escaped operators are left alone (``grep 'a|b' f`` keeps its pipe
    inside the pattern). An unterminated quote is left as-is for ``shlex`` to
    reject, which the caller turns into a denial.
    """
    out: list[str] = []
    i = 0
    n = len(line)
    in_single = False
    in_double = False
    while i < n:
        ch = line[i]
        if in_single:
            out.append(ch)
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if ch == "\\" and i + 1 < n:
                out.append(ch)
                out.append(line[i + 1])
                i += 2
                continue
            out.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(line[i + 1])
            i += 2
            continue
        if ch == "'":
            in_single = True
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            out.append(ch)
            i += 1
            continue
        for op in _SHELL_OPERATORS:
            if line.startswith(op, i):
                out.append(" ")
                out.append(op)
                out.append(" ")
                i += len(op)
                break
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# Path-bearing option flags per command. Their value is a filesystem path the
# utility writes to / reads from, but it hides behind a ``-`` so the generic
# "skip anything starting with ``-``" rule would wave it through. We check the
# value in every form: separated (``-t DIR``), bundled (``-tDIR``), and long
# (``--target-directory=DIR`` / ``--target-directory DIR``).
_PATH_BEARING_FLAGS: dict[str, dict[str, str]] = {
    "cp": {"-t": "--target-directory"},
    "mv": {"-t": "--target-directory"},
    "ln": {"-t": "--target-directory"},
    "install": {"-t": "--target-directory"},
    "wget": {"-O": "--output-document", "-P": "--directory-prefix"},
    "curl": {"-o": "--output"},
    "tar": {"-C": "--directory", "-f": "--file"},
    "sort": {"-o": "--output"},
    # Project tooling excluded from the OS sandbox: their directory flags are the
    # only thing that can point the tool at a tree outside the approved root.
    "git": {"-C": "--git-dir", "--work-tree": "--work-tree"},
    "npm": {"--prefix": "--prefix"},
    "npx": {"--prefix": "--prefix"},
    "yarn": {"--cwd": "--cwd"},
    "pnpm": {"-C": "--dir"},
    "poetry": {"-C": "--directory"},
    "pip": {"-t": "--target"},
    "pip3": {"-t": "--target"},
}

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

    # Newlines are command separators in bash, but shlex.split collapses them
    # into ordinary whitespace — so ``echo a\nrm -rf /outside`` would be parsed
    # as a single ``echo``-led chain and skip path validation. Split on newlines
    # first and tokenize each physical line independently so each line becomes
    # its own command chain.
    tokens: list[str] = []
    for line in command.replace("\r", "\n").split("\n"):
        if not line.strip():
            continue
        try:
            line_tokens = shlex.split(_normalize_shell_operators(line))
        except ValueError:
            # A command we cannot tokenize (e.g. an unclosed quote) cannot be
            # boundary-checked at all. Deny by default rather than fail open —
            # otherwise any malformed command bypasses the check whenever the OS
            # sandbox is disabled.
            return False, (
                "Directory boundary violation: command could not be parsed for "
                "path validation and is not allowed"
            )
        if tokens and line_tokens:
            # Treat the line break as a separator between chains.
            tokens.append(";")
        tokens.extend(line_tokens)

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

    def _path_escapes(path_token: str) -> bool:
        """Return True if *path_token* resolves outside the approved directory."""
        if path_token in _ALLOWED_DEVICE_TARGETS:
            return False
        try:
            # Expand ``~`` / ``~user`` the way bash does before resolving — shlex
            # leaves the tilde literal, so without this ``> ~/.ssh/authorized_keys``
            # would resolve to a literal ``~`` subdir inside the boundary.
            expanded = os.path.expanduser(path_token)
            if expanded.startswith("/"):
                resolved = Path(expanded).resolve()
            else:
                resolved = (working_directory / expanded).resolve()
            return not _is_within_directory(resolved, resolved_approved)
        except (ValueError, OSError):
            # Unresolvable: rely on the OS-level sandbox rather than guessing.
            return False

    def _secret_reason(path_token: str) -> Optional[str]:
        """Return a denial reason if *path_token*'s basename is a secret file.

        Applied to in-boundary path operands so a bash command can't read/modify
        a secret (``.env``, ``id_rsa``, ``*.pem``) that the Read/Write/Edit tools
        block via ``is_forbidden_secret_file``.
        """
        try:
            name = Path(os.path.expanduser(path_token)).name
        except (ValueError, OSError):
            return None
        forbidden, reason = _is_forbidden_secret_basename(name)
        return reason if forbidden else None

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

        # Strip launcher wrappers (``nohup``, ``timeout 5``, ``sudo``, …) so the
        # wrapped command (``rm``/``cat`` in ``nohup rm -rf /etc``) is the one we
        # classify and path-check, rather than the always-unknown wrapper.
        cmd_tokens = _strip_launcher_wrappers(cmd_tokens)
        if not cmd_tokens:
            continue
        # A wrapper may be immediately followed by another ``VAR=value`` (e.g.
        # ``sudo FOO=bar rm -rf /etc``); strip those too before classifying.
        while cmd_tokens and _ENV_ASSIGN_RE.match(cmd_tokens[0]):
            cmd_tokens = cmd_tokens[1:]
        if not cmd_tokens:
            continue

        # ``Path(".").name`` is the empty string, which would never match the
        # interpreter sets — preserve the bare ``.`` (the POSIX "dot"/source
        # builtin) so ``. /etc/passwd`` is classified and path-checked.
        base_command = "." if cmd_tokens[0] == "." else Path(cmd_tokens[0]).name

        # Redirection targets are filesystem paths the command writes to / reads
        # from regardless of the lead command. Validate the token after each
        # redirection operator even for no-path commands like ``echo`` (so
        # ``echo x > /etc/cron.d/y`` is caught).
        for idx, token in enumerate(cmd_tokens):
            if token in _REDIRECTION_OPERATORS and idx + 1 < len(cmd_tokens):
                target = cmd_tokens[idx + 1]
                if _path_escapes(target):
                    return False, (
                        f"Directory boundary violation: redirection targets "
                        f"'{target}' which is outside approved directory "
                        f"'{resolved_approved}'"
                    )
                secret_reason = _secret_reason(target)
                if secret_reason:
                    return False, (
                        f"Directory boundary violation: redirection targets "
                        f"secret file '{target}' ({secret_reason})"
                    )

        # Read-only commands that take no filesystem path are always allowed.
        if base_command in _READ_ONLY_NO_PATH_COMMANDS:
            continue

        # Determine if this specific command in the chain needs path validation
        needs_check = False
        if base_command == "find":
            # ``find`` always takes a search path; check it so a read/list of an
            # external tree (and any -exec/-delete it performs there) is caught
            # even with no mutating action and a disabled sandbox.
            needs_check = True
        elif base_command in _READ_ONLY_PATH_COMMANDS:
            # Read-only but path-taking: check the paths so an out-of-root read
            # (e.g. ``cat /etc/passwd``) can't slip past a disabled sandbox.
            needs_check = True
        elif base_command in _FS_MODIFYING_COMMANDS:
            needs_check = True
        elif base_command in _NETWORK_OR_INTERP_COMMANDS:
            needs_check = True
        elif base_command in _FLAG_PATH_ONLY_COMMANDS:
            # Only their explicit directory flags are paths; the positional
            # operands are subcommands, package names and commit messages.
            needs_check = True

        if not needs_check:
            continue

        # ``eval <string>`` executes a shell string that no path check can
        # analyze — resolving it against the working directory lands it inside
        # the boundary and falsely passes. Deny any invocation with an operand.
        if base_command in _CODE_STRING_COMMANDS and len(cmd_tokens) > 1:
            return False, (
                f"Inline-code execution via '{base_command}' cannot be validated "
                "against the directory boundary and is not allowed"
            )

        # Interpreters invoked with an inline-code/opaque-module flag execute a
        # code string that no filesystem-path check can analyze. Resolving that
        # string as a path would falsely "pass" the boundary, so deny these
        # outright instead of trusting the OS sandbox alone (covers the
        # SANDBOX_ENABLED=false case).
        if base_command in _INTERP_COMMANDS:
            for token in cmd_tokens[1:]:
                flag = token.split("=", 1)[0]
                # Bundled short flags like ``-ec`` or ``-ic`` smuggle ``-c``/
                # ``-e`` past an exact match; flag the whole cluster.
                bundled_short = (
                    len(flag) > 1
                    and flag[0] == "-"
                    and flag[1] != "-"
                    and any(f"-{ch}" in _INLINE_CODE_FLAGS for ch in flag[1:])
                )
                if flag in _INLINE_CODE_FLAGS or bundled_short:
                    return False, (
                        f"Inline-code execution via '{base_command} {token}' "
                        "cannot be validated against the directory boundary "
                        "and is not allowed"
                    )

        # Check each argument for paths outside the boundary
        path_flags = _PATH_BEARING_FLAGS.get(base_command, {})
        seen_double_dash = False
        rest_tokens = cmd_tokens[1:]
        idx = 0
        path_value: Optional[str]
        while idx < len(rest_tokens):
            token = rest_tokens[idx]
            idx += 1
            # For flag-path-only tooling the positional operands are subcommands,
            # package names and commit messages, not paths — checking them would
            # deny ``git commit -m "/etc/hosts: fix"``. Only the directory flags
            # handled below are boundary-checked.
            if base_command in _FLAG_PATH_ONLY_COMMANDS and not token.startswith("-"):
                continue
            # ``--`` marks the end of options; everything after is a positional
            # argument and must be path-checked even if it starts with ``-``.
            if token == "--" and not seen_double_dash:
                seen_double_dash = True
                continue
            # Skip flags only before ``--`` to prevent attackers smuggling paths
            # past the boundary by prefixing them with ``-`` (e.g. ``rm -- -foo``).
            if not seen_double_dash and token.startswith("-"):
                # A long option can still carry a URL (``curl --url=https://…``);
                # check the value side before waving the flag through.
                if base_command in _NETWORK_OR_INTERP_COMMANDS and "=" in token:
                    value = token.split("=", 1)[1]
                    if _URL_SCHEME_RE.match(value):
                        return False, (
                            f"Directory boundary violation: '{base_command}' "
                            f"fetches remote URL '{value}', which cannot be "
                            f"validated against approved directory "
                            f"'{resolved_approved}'"
                        )
                # Path-bearing flags hide a target path inside/after the option.
                # Pull out the value in whichever form it appears and fall through
                # to the boundary check; otherwise the flag is skipped as usual.
                path_value = None
                if path_flags:
                    short_set = set(path_flags)
                    long_set = set(path_flags.values())
                    name = token.split("=", 1)[0]
                    if "=" in token and name in long_set:
                        # ``--target-directory=DIR``
                        path_value = token.split("=", 1)[1]
                    elif token in long_set or token in short_set:
                        # Separated form ``--target-directory DIR`` / ``-t DIR``:
                        # the value is the next token.
                        if idx < len(rest_tokens):
                            path_value = rest_tokens[idx]
                            idx += 1
                    elif len(token) > 2 and token[1] != "-" and token[:2] in short_set:
                        # Bundled form ``-tDIR``.
                        path_value = token[2:]
                if path_value and _path_escapes(path_value):
                    return False, (
                        f"Directory boundary violation: '{base_command}' "
                        f"targets '{path_value}' which is outside approved "
                        f"directory '{resolved_approved}'"
                    )
                if path_value:
                    secret_reason = _secret_reason(path_value)
                    if secret_reason:
                        return False, (
                            f"Directory boundary violation: '{base_command}' "
                            f"targets secret file '{path_value}' ({secret_reason})"
                        )
                continue

            # A ``scheme://`` argument to a network command (curl/wget/fetch …)
            # is a remote fetch, not a filesystem path. Resolving it against the
            # working dir lands it *inside* the boundary as a literal subdir
            # (e.g. ``cwd/https:/evil.com``) and would falsely pass, so reject
            # such commands outright — they can't be validated against the dir
            # boundary. Checked before the ``=`` split so query strings
            # (``?a=b``) don't strip the scheme.
            if base_command in _NETWORK_OR_INTERP_COMMANDS and _URL_SCHEME_RE.match(
                token
            ):
                return False, (
                    f"Directory boundary violation: '{base_command}' fetches "
                    f"remote URL '{token}', which cannot be validated against "
                    f"approved directory '{resolved_approved}'"
                )

            # ``key=value`` operands (e.g. ``dd of=/etc/shadow``) hide the real
            # path on the right of ``=``; resolving the whole token would treat
            # ``of=/etc/shadow`` as a relative name inside the working dir and
            # miss the escape. Check the value part instead.
            if "=" in token:
                token = token.split("=", 1)[1]
                # The value side may itself be a URL (e.g. ``url=https://…``);
                # re-apply the scheme check the whole-token match missed.
                if (
                    base_command in _NETWORK_OR_INTERP_COMMANDS
                    and _URL_SCHEME_RE.match(token)
                ):
                    return False, (
                        f"Directory boundary violation: '{base_command}' fetches "
                        f"remote URL '{token}', which cannot be validated against "
                        f"approved directory '{resolved_approved}'"
                    )
                if not token:
                    continue

            # Resolve both absolute and relative paths against the working
            # directory so that traversal sequences like ``../../evil`` are
            # caught instead of being silently allowed. Expand ``~``/``~user``
            # first (bash does, shlex doesn't) so ``cat ~/.ssh/id_rsa`` is not
            # treated as a literal ``~`` subdir inside the boundary.
            if token in _ALLOWED_DEVICE_TARGETS:
                continue
            try:
                expanded_token = os.path.expanduser(token)
                if expanded_token.startswith("/"):
                    resolved = Path(expanded_token).resolve()
                else:
                    resolved = (working_directory / expanded_token).resolve()

                if not _is_within_directory(resolved, resolved_approved):
                    return False, (
                        f"Directory boundary violation: '{base_command}' targets "
                        f"'{token}' which is outside approved directory "
                        f"'{resolved_approved}'"
                    )

                # In-boundary but a secret/credential file: deny so a bash
                # read/modify can't bypass the is_forbidden_secret_file gate.
                secret_reason = _secret_reason(token)
                if secret_reason:
                    return False, (
                        f"Directory boundary violation: '{base_command}' targets "
                        f"secret file '{token}' ({secret_reason})"
                    )
            except (ValueError, OSError):
                # If path resolution fails, the command might be malformed or
                # using bash features we can't statically analyze.
                # We skip checking this token and rely on the OS-level sandbox.
                continue

    return True, None


def _is_forbidden_secret_basename(name: str) -> Tuple[bool, Optional[str]]:
    """Match a basename against the secret/credential blocklist.

    Mirrors :meth:`SecurityValidator.is_forbidden_secret_file` (reusing the same
    ``FORBIDDEN_FILENAMES`` / ``DANGEROUS_FILE_PATTERNS`` data) so a bash
    read/modify of an in-boundary secret (``cat .env``, ``head .ssh/id_rsa``) is
    denied just like the Read/Write/Edit tools are — the bash path must not
    bypass the ``is_forbidden_secret_file`` gate.

    Only ever called on tokens the caller has already proved to be inside the
    approved directory, so ``SYSTEM_ONLY_FORBIDDEN_FILENAMES`` (``hosts``,
    ``passwd``, …) is deliberately not applied: those are ordinary project files
    here, and the ``/etc`` originals are stopped by the boundary check.
    """
    if not name:
        return False, None
    name = name.strip()
    if name.lower() in {n.lower() for n in SecurityValidator.FORBIDDEN_FILENAMES}:
        return True, f"Forbidden filename: {name}"
    for pattern in SecurityValidator.DANGEROUS_FILE_PATTERNS:
        if re.match(pattern, name, re.IGNORECASE):
            return True, f"File type not allowed: {name}"
    return False, None


def _is_claude_internal_path(file_path: str) -> bool:
    """Check whether *file_path* points inside ``~/.claude/`` (allowed subdirs only)."""
    try:
        # Expand ``~``/``~user`` first so ``~/.claude/plans/...`` is recognized
        # as an internal path (Path.resolve leaves the tilde literal).
        resolved = Path(os.path.expanduser(file_path)).resolve()
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
