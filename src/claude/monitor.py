"""Bash directory boundary enforcement for Claude tool calls."""

import os
import re
import shlex
from pathlib import Path
from typing import NamedTuple, Optional, Set, Tuple

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
    # Change directory like cd: unclassified, ``pushd /etc`` passed unchecked.
    "pushd",
    "popd",
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

# Commands whose positional operand is a *program*, not a filesystem path:
# ``awk '{print $1}' file``, ``sed -n '1,5p' file``. Two consequences:
#   * the script must not be boundary-checked — ``sed '/^foo/d'`` looks like an
#     absolute path and would be refused as an escape;
#   * the script is code. awk's ``system()`` and command pipes, and GNU sed's
#     ``e``/``w``/``r`` commands, run shell commands and touch files no path
#     check ever sees. That is the same hole ``_INLINE_CODE_FLAGS`` closes for
#     python/node/perl, and it was open here because the script resolved to a
#     harmless-looking relative name inside the working directory.
_SCRIPT_PROGRAM_COMMANDS: Set[str] = {"awk", "gawk", "nawk", "mawk", "sed"}

# Flags carrying a program string (``sed -e 's/a/b/'``), a program *file*
# (``-f prog.sed`` — a real path, left to the normal boundary check), and flags
# whose value is neither (``awk -v n=1``). All three are listed so the operand
# is consumed and not mistaken for the positional script.
_SCRIPT_PROGRAM_FLAGS: Set[str] = {"-e", "--expression"}
_SCRIPT_FILE_FLAGS: Set[str] = {"-f", "--file", "--source"}
_SCRIPT_VALUE_FLAGS: Set[str] = {"-v", "--assign", "-F", "--field-separator"}

# Executing / file-touching constructs inside an awk program.
_AWK_EXEC_RE = re.compile(
    r"\bsystem\s*\("  # system("cmd")
    r'|\|\s*&?\s*"'  # print … | "cmd", … |& "cmd"
    r'|"\s*\|\s*&?\s*getline'  # "cmd" | getline
    r'|\b(?:print|printf)\b[^;{}\n]*>>?\s*"'  # print … > "file"
)

# The GNU sed ``e`` (execute), ``w``/``W`` (write) and ``r``/``R`` (read)
# commands at a command position — start of script, ``;``, ``}`` or newline,
# optionally behind a line/regex address — plus the ``e``/``w`` flags on
# ``s///``. Ordinary ``s/a/b/g``, ``1,5p``, ``$d`` and ``y/…/…/`` do not match.
_SED_EXEC_RE = re.compile(
    r"(?:^|[;}\n])\s*!?\s*"
    r"(?:[0-9$]+(?:\s*~\s*[0-9]+)?(?:\s*,\s*[0-9$+~]+)?|/(?:\\.|[^/\n])*/[IM]*)?"
    r"\s*!?\s*[eEwWrR]\b"
    r"|s(?P<d>[^\w\s])(?:\\.|(?!(?P=d)).)*(?P=d)(?:\\.|(?!(?P=d)).)*(?P=d)"
    r"[gpiImMnN0-9]*[ewW]"
)


def _script_program_operands(cmd_tokens: list[str]) -> dict[int, str]:
    """Map token index -> program text for an awk/sed invocation.

    Follows the way both tools parse their arguments: ``-e``/``--expression``
    supplies a script, ``-f``/``--file`` supplies a script *file* (a genuine
    path, so it is left for the boundary check), and the first positional
    operand is the script only when neither flag already gave one.
    """
    programs: dict[int, str] = {}
    script_given = False
    seen_double_dash = False
    idx = 1
    while idx < len(cmd_tokens):
        token = cmd_tokens[idx]
        if not seen_double_dash and token == "--":
            seen_double_dash = True
            idx += 1
            continue
        if not seen_double_dash and token.startswith("-") and token != "-":
            name, sep, inline = token.partition("=")
            if name in _SCRIPT_PROGRAM_FLAGS:
                script_given = True
                if sep:
                    programs[idx] = inline
                elif idx + 1 < len(cmd_tokens):
                    idx += 1
                    programs[idx] = cmd_tokens[idx]
            elif name in _SCRIPT_FILE_FLAGS:
                script_given = True
                if not sep and idx + 1 < len(cmd_tokens):
                    idx += 1  # a path — leave it to the boundary check
            elif name in _SCRIPT_VALUE_FLAGS and not sep:
                if idx + 1 < len(cmd_tokens):
                    idx += 1
            idx += 1
            continue
        if not script_given:
            programs[idx] = token
            script_given = True
        idx += 1
    return programs


def _script_program_denial(base_command: str, program: str) -> Optional[str]:
    """Return a reason if *program* executes a command or names a file."""
    if base_command == "sed":
        if _SED_EXEC_RE.search(program):
            return (
                "it uses a sed e/w/r command, which executes a shell command or "
                "reads/writes a file the boundary check never sees"
            )
        return None
    if _AWK_EXEC_RE.search(program):
        return (
            "it uses awk system()/command-pipe/redirect, which executes a shell "
            "command or writes a file the boundary check never sees"
        )
    return None


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

# Fetch tools whose positional operand is always a URL: curl/wget read a
# scheme-less ``example.com/x`` or ``localhost:8080`` as ``http://…``, so the
# scheme check alone misses it and the token resolves to a harmless-looking
# relative path inside the working dir. Only operands spelled as paths
# (``./x``, ``../x``, ``/x``, ``~/x``) go on to the boundary check; anything
# else is treated as a remote host and denied.
_FETCH_COMMANDS: Set[str] = {"curl", "wget", "fetch"}
_PATH_SHAPED_PREFIXES: Tuple[str, ...] = ("/", "./", "../", "~")

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
    # Shell reserved words that prefix a command the same way: ``{ rm -rf /etc;
    # }``, ``if …; then rm -rf /etc; fi``, ``! cat /etc/shadow``. Left as the
    # base command they are just another unknown name, and the command behind
    # them ran unchecked.
    "{",
    "!",
    "time",
    "if",
    "then",
    "elif",
    "else",
    "while",
    "until",
    "do",
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
    # Long spellings included: ``sudo --user nobody cat /etc/shadow`` must drop
    # ``nobody`` exactly as ``sudo -u nobody …`` does.
    "sudo": {
        "-u",
        "-g",
        "-C",
        "-D",
        "-h",
        "-p",
        "-R",
        "-r",
        "-t",
        "-T",
        "-U",
        "-c",
        "--user",
        "--group",
        "--close-from",
        "--chdir",
        "--host",
        "--prompt",
        "--chroot",
        "--role",
        "--type",
        "--command-timeout",
        "--other-user",
        "--login-class",
    },
    "doas": {"-u", "-C"},
    "time": {"-f", "--format"},
    "env": {"-u", "--unset", "-C", "--chdir"},
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

# Wrapper flags whose operand is a *shell command string*, not a value to drop.
# ``env -S 'rm -rf /etc'`` executes the string; dropping flag and operand left
# an empty chain, which the checker waved through as "nothing to validate".
# Deny them outright, exactly as ``_INLINE_CODE_FLAGS`` does for interpreters.
_WRAPPER_CODE_STRING_FLAGS: dict[str, Set[str]] = {
    "env": {"-S", "--split-string"},
}

# Wrapper flags whose operand is a real filesystem path: ``xargs -a FILE`` reads
# FILE. It was being dropped unchecked, so ``xargs -a /etc/shadow …`` read
# outside the root without tripping any check. Boundary-check it before
# dropping it from the command position.
_WRAPPER_PATH_OPERAND_FLAGS: dict[str, Set[str]] = {
    "xargs": {"-a", "--arg-file"},
    "time": {"-o", "--output"},
}


class _StrippedCommand(NamedTuple):
    """Result of :func:`_strip_launcher_wrappers`."""

    tokens: list[str]
    denial: Optional[str]
    path_operands: list[str]


def _strip_launcher_wrappers(cmd_tokens: list[str]) -> _StrippedCommand:
    """Strip leading launcher wrappers (and their own flags/operands).

    Handles chained wrappers (``nohup timeout 5 nice -n 5 rm -rf /etc``) by
    looping until the leading token is no longer a recognized wrapper, so
    ``tokens`` begins with the command that actually runs.

    A wrapper's own operands are not all inert: ``denial`` is set when one of
    them is a shell command string the checker cannot analyze, and
    ``path_operands`` collects the ones that name a file, for the caller to
    boundary-check.
    """
    path_operands: list[str] = []
    while cmd_tokens and Path(cmd_tokens[0]).name in _LAUNCHER_WRAPPERS:
        wrapper = Path(cmd_tokens[0]).name
        code_flags = _WRAPPER_CODE_STRING_FLAGS.get(wrapper, set())
        path_flags = _WRAPPER_PATH_OPERAND_FLAGS.get(wrapper, set())
        operand_flags = _WRAPPER_OPERAND_FLAGS.get(wrapper, set())
        cmd_tokens = cmd_tokens[1:]
        # Skip the wrapper's own option flags (``-n``, ``-c2``, ``-f``, …) and
        # any standalone value that follows a separated flag (``-n 10``).
        while cmd_tokens and cmd_tokens[0].startswith("-"):
            flag = cmd_tokens[0]
            cmd_tokens = cmd_tokens[1:]
            name, sep, inline = flag.partition("=")
            # A short flag may carry its value in the same token
            # (``-S'rm -rf /'``, ``-a/etc/shadow``); normalise that form.
            if name not in code_flags | path_flags and len(flag) > 2:
                for short in code_flags | path_flags:
                    if len(short) == 2 and flag.startswith(short):
                        name, sep, inline = short, "=", flag[2:]
                        break

            if name in code_flags:
                return _StrippedCommand(
                    [],
                    f"Inline-code execution via '{wrapper} {name}' cannot be "
                    "validated against the directory boundary and is not allowed",
                    path_operands,
                )

            if name in path_flags or name in operand_flags:
                if sep:
                    value: Optional[str] = inline
                elif cmd_tokens and not cmd_tokens[0].startswith("-"):
                    value = cmd_tokens[0]
                    cmd_tokens = cmd_tokens[1:]
                else:
                    value = None
                if value and name in path_flags:
                    path_operands.append(value)
                continue

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
        # ``timeout``/``chrt`` take a bare positional operand (``timeout 5``,
        # ``chrt 99`` rare) before the command; drop a single leading numeric.
        if (
            wrapper in _WRAPPERS_WITH_NUMERIC_OPERAND
            and cmd_tokens
            and re.match(r"^\d+(\.\d+)?[a-zA-Z]?$", cmd_tokens[0])
        ):
            cmd_tokens = cmd_tokens[1:]
    return _StrippedCommand(cmd_tokens, None, path_operands)


# Bash command separators. Parentheses count: ``(rm -rf /etc)`` and the body of
# ``f() { …; }`` start a command just as ``;`` does, and glued to the word they
# used to hide it (``(rm`` is an unknown name).
_COMMAND_SEPARATORS: Set[str] = {"&&", "||", ";", "|", "&", "(", ")"}

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
    "(",
    ")",
)

# Prefix the normalizer puts on every operator it finds *outside quotes*, so the
# tokens ``shlex`` hands back can be told apart from an argument that merely
# spells one. In ``cat ';' /etc/passwd`` the ``;`` is a file name; read as a
# separator it cut ``/etc/passwd`` off from ``cat`` and left it unchecked. NUL
# cannot occur in a command line, so the mark cannot be forged from inside one.
_OPERATOR_MARK = "\x00"


class _Operator(str):
    """A shell operator that stood unquoted in the command."""


# ``$'…`` up to, not including, its closing quote, as bash reads it.
_ANSI_C_STRING_RE = re.compile(r"\$'(?:[^'\\]|\\.)*")


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

    Each operator found this way is prefixed with ``_OPERATOR_MARK`` (see
    :func:`_tokenize`). Raises ``ValueError`` — a denial, like a ``shlex``
    failure — on ``$'…\\'…'``: inside ANSI-C quoting ``\\'`` continues the
    string where ``shlex`` ends it, so from there on the two disagree about
    what is quoted and ``echo $'\\'' ; rm -rf /etc #'`` reads as one harmless
    ``echo`` here. ``$'\\t'`` and the like tokenize the same either way.
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
        if ch == "$" and line.startswith(("'", '"'), i + 1):
            ansi_c = _ANSI_C_STRING_RE.match(line, i)
            if ansi_c and "'" in ansi_c.group()[2:]:
                raise ValueError("escaped quote inside ANSI-C quoting")
            # ``$'…'`` and ``$"…"`` stand for the quoted text itself. Kept, the
            # ``$`` made ``cat $'/etc/passwd'`` a relative ``$/etc/passwd``.
            i += 1
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
                out.append(_OPERATOR_MARK + op)
                out.append(" ")
                i += len(op)
                break
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _tokenize(line: str) -> list[str]:
    """Split one command line into shell words.

    Operators that stood unquoted come back as :class:`_Operator`; a quoted or
    escaped one (``';'``, ``\\;``) is an ordinary word. Raises ``ValueError``
    when the line cannot be tokenized.
    """
    return [
        _Operator(token[1:]) if token.startswith(_OPERATOR_MARK) else token
        for token in shlex.split(_normalize_shell_operators(line))
    ]


# A heredoc whose body is plain data: ``cat`` or ``tee`` copying it to stdout or
# to a file. Only this exact shape counts — one simple command with plain
# arguments and ``>`` redirections; no quoting, expansion, comment, pipe, chain
# or second ``<`` anywhere on the line — so where the body starts and who reads
# it is never a matter of interpretation.
_DATA_HEREDOC_RE = re.compile(
    r"""^[ \t]*(?:cat|tee)(?=[ \t<>])
        [\w \t>./~+,:=@%^-]*
        <<(?P<strip>-?)[ \t]*
        (?P<quote>['"]?)(?P<delimiter>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)
        [\w \t>./~+,:=@%^-]*$
    """,
    re.VERBOSE,
)


def _split_data_heredocs(lines: list[str]) -> Tuple[list[str], list[str]]:
    """Set aside heredoc bodies that are data, not commands.

    Returns ``(command_lines, expanded_lines)``: the lines to check as commands,
    and those plus every body bash still expands — an unquoted delimiter keeps
    ``$(…)`` live inside the body, a quoted one (``<<'EOF'``) makes it literal.

    Without this, each body line of ``cat > deploy.sh <<EOF`` was parsed as a
    command of its own: a line with an apostrophe failed to tokenize, a line
    naming ``curl https://…`` was refused as a fetch, and a quoted body holding
    ``$(…)`` as a substitution.

    Any ``<<`` outside the recognised shape turns the whole thing off (every
    line is a command again, as before): which later lines belong to *that*
    heredoc is not modelled, so a body recognised after it could be misplaced.
    """
    commands: list[str] = []
    expanded: list[str] = []
    position = 0
    while position < len(lines):
        line = lines[position]
        position += 1
        commands.append(line)
        expanded.append(line)
        match = _DATA_HEREDOC_RE.match(line)
        if match is None:
            if "<<" in line:
                return lines, lines
            continue
        while position < len(lines):
            body = lines[position]
            position += 1
            if (body.lstrip("\t") if match["strip"] else body) == match["delimiter"]:
                break
            if not match["quote"]:
                # Bash joins a backslash-newline in an expanding body, so the
                # terminator could be assembled from two lines we see apart.
                if body.endswith("\\"):
                    return lines, lines
                expanded.append(body)
    return commands, expanded


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

    unparseable = (
        False,
        "Directory boundary violation: command could not be parsed for "
        "path validation and is not allowed",
    )
    if _OPERATOR_MARK in command:
        return unparseable

    # Newlines are command separators in bash, but shlex.split collapses them
    # into ordinary whitespace — so ``echo a\nrm -rf /outside`` would be parsed
    # as a single ``echo``-led chain and skip path validation. Split on newlines
    # first and tokenize each physical line independently so each line becomes
    # its own command chain. Heredoc bodies that are data are not commands.
    command_lines, expanded_lines = _split_data_heredocs(
        command.replace("\r", "\n").split("\n")
    )

    # Reject subshells / command substitution outright — shlex.split bundles
    # ``$(rm -rf /etc)`` and ``<(cat /etc/shadow)`` into a single opaque token,
    # so per-command path checking cannot see what's executing inside. We
    # consider such constructs out of scope for static analysis.
    expanded = "\n".join(expanded_lines)
    for pat in _SUBSHELL_PATTERNS:
        if pat.search(expanded):
            return False, (
                "Directory boundary violation: command contains subshell or "
                "command-substitution syntax which cannot be safely validated"
            )

    tokens: list[str] = []
    for line in command_lines:
        if not line.strip():
            continue
        try:
            line_tokens = _tokenize(line)
        except ValueError:
            # A command we cannot tokenize (e.g. an unclosed quote) cannot be
            # boundary-checked at all. Deny by default rather than fail open —
            # otherwise any malformed command bypasses the check whenever the OS
            # sandbox is disabled.
            return unparseable
        if tokens and line_tokens:
            # Treat the line break as a separator between chains.
            tokens.append(_Operator(";"))
        tokens.extend(line_tokens)

    if not tokens:
        return True, None

    # Split tokens into individual commands based on separators
    command_chains: list[list[str]] = []
    current_chain: list[str] = []

    for token in tokens:
        if isinstance(token, _Operator) and token in _COMMAND_SEPARATORS:
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
        stripped = _strip_launcher_wrappers(cmd_tokens)
        if stripped.denial:
            return False, stripped.denial
        # A wrapper operand that names a file (``xargs -a FILE``) is a real path
        # read by the wrapper itself, so it gets the same checks as any other.
        for operand in stripped.path_operands:
            if _path_escapes(operand):
                return False, (
                    f"Directory boundary violation: '{operand}' is outside "
                    f"approved directory '{resolved_approved}'"
                )
            secret_reason = _secret_reason(operand)
            if secret_reason:
                return False, (
                    f"Directory boundary violation: '{operand}' is a secret "
                    f"file ({secret_reason})"
                )
        cmd_tokens = stripped.tokens
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
            if (
                isinstance(token, _Operator)
                and token in _REDIRECTION_OPERATORS
                and idx + 1 < len(cmd_tokens)
            ):
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

        # awk/sed take a *program*, not a path, as their positional operand.
        # Deny the constructs inside it that execute a shell command or name a
        # file, and remember which tokens are program text so the boundary loop
        # below skips them (``sed '/^foo/d'`` is a script, not an absolute path).
        script_programs: dict[int, str] = {}
        if base_command in _SCRIPT_PROGRAM_COMMANDS:
            script_programs = _script_program_operands(cmd_tokens)
            for program in script_programs.values():
                reason = _script_program_denial(base_command, program)
                if reason:
                    return False, (
                        f"'{base_command}' program '{program}' is not allowed: "
                        f"{reason}"
                    )

        # Check each argument for paths outside the boundary
        path_flags = _PATH_BEARING_FLAGS.get(base_command, {})
        seen_double_dash = False
        rest_tokens = cmd_tokens[1:]
        idx = 0
        path_value: Optional[str]
        while idx < len(rest_tokens):
            token = rest_tokens[idx]
            token_index = idx + 1
            idx += 1
            if token_index in script_programs:
                continue
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
                    bare_url = (
                        base_command in _FETCH_COMMANDS
                        and token.split("=", 1)[0] == "--url"
                        and not value.startswith(_PATH_SHAPED_PREFIXES)
                    )
                    if _URL_SCHEME_RE.match(value) or bare_url:
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
                    elif len(token) > 2 and token[1] != "-":
                        # Bundled form ``-tDIR``, and a getopt cluster where the
                        # path flag is not the first letter: ``-xf/etc/shadow``
                        # (value attached) or ``-xf ARCHIVE`` (value next).
                        for pos in range(1, len(token)):
                            if f"-{token[pos]}" in short_set:
                                path_value = token[pos + 1 :]
                                if not path_value and idx < len(rest_tokens):
                                    path_value = rest_tokens[idx]
                                    idx += 1
                                break
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
            if base_command in _FETCH_COMMANDS and not token.startswith(
                _PATH_SHAPED_PREFIXES
            ):
                return False, (
                    f"Directory boundary violation: '{base_command}' treats "
                    f"'{token}' as a remote URL, which cannot be validated "
                    f"against approved directory '{resolved_approved}'"
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
