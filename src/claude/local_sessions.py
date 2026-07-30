"""Discover Claude Code sessions from local ~/.claude/projects/ storage.

Claude Code (CLI, VS Code, SDK) persists session logs as JSONL files under
``~/.claude/projects/<encoded-path>/<session-uuid>.jsonl``.  This module
reads those files so the Telegram bot can auto-resume sessions that were
started outside of the bot (e.g. in VS Code or the CLI).
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import List, Optional

import structlog

logger = structlog.get_logger()

# Claude Code encodes the working-directory path into a folder name by
# replacing every non-alphanumeric character (except ``-``) with ``-``.
_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9-]")


def _encode_path(path: Path) -> str:
    """Encode a working-directory path the same way Claude Code does."""
    return _NON_ALNUM_RE.sub("-", str(path))


@dataclass
class LocalSession:
    """Minimal metadata for a session discovered on disk.

    ``last_message`` is only populated by :func:`list_all_local_sessions` (the
    ``/sessions`` listing, where it is what actually tells two sessions apart).
    The auto-resume lookups do not need it and skip the extra tail read.
    """

    session_id: str
    cwd: str
    timestamp: datetime
    jsonl_path: Path
    first_message: str = ""
    mtime: float = 0.0
    last_message: str = ""


def _claude_projects_dir() -> Path:
    """Return ``~/.claude/projects``."""
    return Path.home() / ".claude" / "projects"


# Maximum bytes we will read per JSONL line. A malformed or hostile file with
# a single multi-MB line could otherwise cause Python to load it whole into
# memory just to discard most of it. 4 MiB is comfortably above the largest
# legitimate Claude session prompts we observe.
_MAX_LINE_BYTES = 4 * 1024 * 1024


# How much of a JSONL tail we scan for the most recent user message. The last
# prompt of a session sits within a few KB of the end, so reading a whole
# (possibly many-MB) transcript to render one preview line is not worth it.
_TAIL_SCAN_BYTES = 256 * 1024

# Preview length used for both the first and the last user message.
_PREVIEW_CHARS = 60


def _extract_user_text(obj: dict) -> str:
    """Return the text carried by a JSONL ``user`` entry, else "".

    Tool results also arrive as ``type: "user"`` entries, but they hold
    ``tool_result`` blocks rather than text — those yield "" so callers keep
    looking for a real prompt.
    """
    if obj.get("type") != "user":
        return ""
    message = obj.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content", [])
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    return text
    return ""


def _parse_session_tail(jsonl_path: Path) -> str:
    """Return the most recent user message in *jsonl_path*, truncated.

    Only the last ``_TAIL_SCAN_BYTES`` are read, so the cost does not grow with
    the length of the session. Returns "" when that window holds no user text
    (e.g. a long trailing tool loop) or the file cannot be read.
    """
    try:
        with open(jsonl_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            start = max(0, size - _TAIL_SCAN_BYTES)
            fh.seek(start)
            blob = fh.read()
        if start > 0:
            # The window almost certainly begins mid-line; drop that fragment.
            _, _, blob = blob.partition(b"\n")
        for raw in reversed(blob.splitlines()):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            text = _extract_user_text(obj)
            if text:
                return text[:_PREVIEW_CHARS]
    except OSError:
        return ""
    return ""


def load_session_previews(cwd: Path, session_id: str) -> tuple[str, str]:
    """Return ``(first_message, last_message)`` for one already-known session.

    Enriches rows the bot took from its own storage, where no directory scan
    happened: the JSONL path is fully determined by the session's cwd and id, so
    this is a couple of bounded reads rather than a walk.
    """
    path = _claude_projects_dir() / _encode_path(cwd) / f"{session_id}.jsonl"
    if not path.is_file():
        return "", ""
    head = _parse_session_head(path)
    first = head.get("first_message", "") if head else ""
    return first, _parse_session_tail(path)


def _parse_session_head(jsonl_path: Path) -> Optional[dict]:
    """Read session metadata and the first user message from a JSONL file.

    Returns a dict with keys from the first line (``cwd``, ``timestamp``, …)
    plus an extra ``first_message`` key containing the beginning of the first
    user prompt (truncated to ~60 chars).
    """
    try:
        result: Optional[dict] = None
        first_message = ""
        # Open in binary mode and read at most _MAX_LINE_BYTES per line so a
        # single oversize line can't be used to DoS the bot. ``readline(limit)``
        # caps the allocation: iterating ``for raw in fh`` would instead pull
        # the whole physical line (potentially multi-GB) into memory *before*
        # we could check its length.
        with open(jsonl_path, "rb") as fh:
            while True:
                raw = fh.readline(_MAX_LINE_BYTES + 1)
                if not raw:
                    break  # EOF
                if len(raw) > _MAX_LINE_BYTES:
                    # Oversize line: readline stopped at the cap mid-line (no
                    # trailing newline). Skip this line and drain the remainder
                    # in bounded chunks so we don't realloc the whole line, then
                    # continue scanning subsequent lines.
                    if not raw.endswith(b"\n"):
                        while True:
                            chunk = fh.readline(_MAX_LINE_BYTES + 1)
                            if not chunk or chunk.endswith(b"\n"):
                                break
                    continue
                try:
                    line = raw.decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if result is None:
                    result = obj
                # Look for the first user message
                if not first_message:
                    first_message = _extract_user_text(obj)[:_PREVIEW_CHARS]
                # Stop after we have both metadata and first message
                if result is not None and first_message:
                    break
        if result is not None:
            result["first_message"] = first_message
        return result
    except Exception:
        return None


def _build_local_session(entry: Path) -> Optional[LocalSession]:
    """Parse one JSONL file into a LocalSession (None if unparseable)."""
    first = _parse_session_head(entry)
    if not first:
        return None

    cwd = first.get("cwd", "")
    ts_str = first.get("timestamp", "")
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    except (ValueError, AttributeError):
        ts = datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC)

    return LocalSession(
        session_id=entry.stem,  # Session ID is the file stem (UUID)
        cwd=cwd,
        timestamp=ts,
        jsonl_path=entry,
        first_message=first.get("first_message", ""),
    )


def _entries_by_mtime(target_dir: Path) -> List[Path]:
    """Return ``.jsonl`` entries in *target_dir* sorted by mtime (newest first).

    Sorting on the cheap ``stat().st_mtime`` *before* parsing lets callers
    parse lazily (e.g. stop at the first non-excluded match) instead of reading
    every file up front.
    """
    # Pair each entry with its mtime, skipping files that vanish between
    # iterdir() and stat() (FileNotFoundError is an OSError subclass).
    entries: List[tuple[Path, float]] = []
    for e in target_dir.iterdir():
        if e.suffix != ".jsonl" or not e.is_file():
            continue
        try:
            mtime = e.stat().st_mtime
        except OSError:
            continue
        entries.append((e, mtime))
    entries.sort(key=lambda pair: pair[1], reverse=True)
    return [e for e, _ in entries]


def find_local_sessions(working_directory: Path) -> List[LocalSession]:
    """Find all Claude Code sessions on disk for *working_directory*.

    Returns a list sorted by modification time (newest first).
    """
    projects_dir = _claude_projects_dir()
    if not projects_dir.is_dir():
        return []

    encoded = _encode_path(working_directory)

    # Find the matching project folder
    target_dir = projects_dir / encoded
    if not target_dir.is_dir():
        logger.debug(
            "No local project dir found",
            encoded=encoded,
            projects_dir=str(projects_dir),
        )
        return []

    # Sort by cheap st_mtime first, then parse (newest first — more reliable
    # than the first-line timestamp which is the session *creation* time).
    sessions: List[LocalSession] = []
    for entry in _entries_by_mtime(target_dir):
        session = _build_local_session(entry)
        if session is not None:
            sessions.append(session)

    logger.debug(
        "Found local sessions",
        working_directory=str(working_directory),
        count=len(sessions),
    )
    return sessions


def find_latest_local_session(
    working_directory: Path,
    exclude_ids: Optional[set] = None,
) -> Optional[LocalSession]:
    """Return the most recently modified session for *working_directory*.

    Sessions whose ID is in *exclude_ids* are skipped (e.g. sessions the
    bot already knows about). Entries are ordered by the cheap ``st_mtime``
    first and parsed lazily, so the common case (newest session matches) reads
    only one file instead of parsing the whole directory up front.
    """
    projects_dir = _claude_projects_dir()
    if not projects_dir.is_dir():
        return None

    target_dir = projects_dir / _encode_path(working_directory)
    if not target_dir.is_dir():
        return None

    for entry in _entries_by_mtime(target_dir):
        if exclude_ids and entry.stem in exclude_ids:
            continue
        session = _build_local_session(entry)
        if session is not None:
            return session
    return None


def _is_within(path: Path, root: Path) -> bool:
    """Return True if *path* is inside *root* (after resolution)."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def list_all_local_sessions(
    limit: int = 20, within: Optional[Path] = None
) -> List[LocalSession]:
    """List recent sessions across all projects (for the /sessions command).

    Returns up to *limit* sessions sorted by modification time (newest first).
    When *within* is given, only sessions whose ``cwd`` resolves inside that
    directory are returned — this keeps the bot from listing (and later
    resuming into) sessions that live outside the approved directory.
    """
    projects_dir = _claude_projects_dir()
    if not projects_dir.is_dir():
        return []

    # Collect (path, mtime) for every JSONL across all projects using only the
    # cheap stat() call, then sort by mtime (newest first). This lets us parse
    # the head of just the top entries instead of every file up front.
    candidates: List[tuple[Path, float]] = []
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for entry in project_dir.iterdir():
            if entry.suffix != ".jsonl" or not entry.is_file():
                continue
            try:
                candidates.append((entry, entry.stat().st_mtime))
            except OSError:
                # File vanished between iterdir() and stat() — skip it.
                continue

    candidates.sort(key=lambda c: c[1], reverse=True)

    sessions: List[LocalSession] = []
    for entry, mtime in candidates:
        if len(sessions) >= limit:
            break

        first = _parse_session_head(entry)
        if not first:
            continue

        cwd = first.get("cwd", "")
        # Skip sessions outside the approved directory when scoping.
        if within is not None and (not cwd or not _is_within(Path(cwd), within)):
            continue
        ts_str = first.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
        except (ValueError, AttributeError):
            ts = datetime.fromtimestamp(mtime, tz=UTC)

        sessions.append(
            LocalSession(
                session_id=entry.stem,
                cwd=cwd,
                timestamp=ts,
                jsonl_path=entry,
                first_message=first.get("first_message", ""),
                mtime=mtime,
                # The newest prompt is what distinguishes two long sessions in
                # the same project; the opening one often does not.
                last_message=_parse_session_tail(entry),
            )
        )

    return sessions
