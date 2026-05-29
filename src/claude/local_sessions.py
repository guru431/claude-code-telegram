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
    """Minimal metadata for a session discovered on disk."""

    session_id: str
    cwd: str
    timestamp: datetime
    jsonl_path: Path
    first_message: str = ""


def _claude_projects_dir() -> Path:
    """Return ``~/.claude/projects``."""
    return Path.home() / ".claude" / "projects"


# Maximum bytes we will read per JSONL line. A malformed or hostile file with
# a single multi-MB line could otherwise cause Python to load it whole into
# memory just to discard most of it. 4 MiB is comfortably above the largest
# legitimate Claude session prompts we observe.
_MAX_LINE_BYTES = 4 * 1024 * 1024


def _parse_session_head(jsonl_path: Path) -> Optional[dict]:
    """Read session metadata and the first user message from a JSONL file.

    Returns a dict with keys from the first line (``cwd``, ``timestamp``, …)
    plus an extra ``first_message`` key containing the beginning of the first
    user prompt (truncated to ~60 chars).
    """
    try:
        result: Optional[dict] = None
        first_message = ""
        # Open in binary mode and read up to _MAX_LINE_BYTES per line so a
        # single oversize line can't be used to DoS the bot.
        with open(jsonl_path, "rb") as fh:
            for raw in fh:
                if len(raw) > _MAX_LINE_BYTES:
                    # Skip pathologically long lines rather than abort the
                    # whole scan — other lines may still be useful.
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
                if not first_message and obj.get("type") == "user":
                    msg = obj.get("message", {})
                    content = msg.get("content", [])
                    if isinstance(content, str) and content.strip():
                        first_message = content.strip()[:60]
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "").strip()
                                if text:
                                    first_message = text[:60]
                                    break
                # Stop after we have both metadata and first message
                if result is not None and first_message:
                    break
        if result is not None:
            result["first_message"] = first_message
        return result
    except Exception:
        return None


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

    sessions: List[LocalSession] = []

    for entry in target_dir.iterdir():
        if entry.suffix != ".jsonl" or not entry.is_file():
            continue

        # Session ID is the file stem (UUID)
        session_id = entry.stem

        # Read session metadata and first user message
        first = _parse_session_head(entry)
        if not first:
            continue

        cwd = first.get("cwd", "")
        ts_str = first.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            ts = datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC)

        sessions.append(
            LocalSession(
                session_id=session_id,
                cwd=cwd,
                timestamp=ts,
                jsonl_path=entry,
                first_message=first.get("first_message", ""),
            )
        )

    # Sort newest first (by file modification time — more reliable than
    # the timestamp in the first line which is the session *creation* time).
    sessions.sort(key=lambda s: s.jsonl_path.stat().st_mtime, reverse=True)

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
    bot already knows about).
    """
    for session in find_local_sessions(working_directory):
        if exclude_ids and session.session_id in exclude_ids:
            continue
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

    all_sessions: List[LocalSession] = []

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for entry in project_dir.iterdir():
            if entry.suffix != ".jsonl" or not entry.is_file():
                continue

            session_id = entry.stem
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
            except (ValueError, AttributeError):
                ts = datetime.fromtimestamp(entry.stat().st_mtime, tz=UTC)

            all_sessions.append(
                LocalSession(
                    session_id=session_id,
                    cwd=cwd,
                    timestamp=ts,
                    jsonl_path=entry,
                    first_message=first.get("first_message", ""),
                )
            )

    # Sort by file modification time, newest first
    all_sessions.sort(key=lambda s: s.jsonl_path.stat().st_mtime, reverse=True)
    return all_sessions[:limit]
