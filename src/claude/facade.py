"""High-level Claude Code integration facade.

Provides simple interface for bot handlers.
"""

import asyncio
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

from ..config.settings import Settings
from .exceptions import ClaudeProcessError, ClaudeTimeoutError
from .local_sessions import find_latest_local_session
from .sdk_integration import ClaudeResponse, ClaudeSDKManager, StreamUpdate
from .session import SessionManager

logger = structlog.get_logger()

# Substrings that indicate the resumed session no longer exists on Claude's
# side (expired/deleted), which is the only situation where silently retrying
# as a fresh session is safe — no tool side effects have run yet.
_SESSION_GONE_MARKERS = (
    "no conversation found",
    "no such session",
    "session not found",
    "could not resume",
    "session does not exist",
    "unknown session",
    "session expired",
    "invalid session",
)


def _is_session_gone_error(error: Exception) -> bool:
    """Return True if *error* means the resumed session is missing/expired.

    Timeouts and process crashes are NOT session-gone — replaying the prompt
    after a mutating tool already ran would duplicate side effects (or double
    the wait on timeout), so those must propagate instead of triggering a
    fresh-session restart.
    """
    if isinstance(error, (ClaudeTimeoutError, ClaudeProcessError)):
        return False
    text = str(error).lower()
    return any(marker in text for marker in _SESSION_GONE_MARKERS)


class ClaudeIntegration:
    """Main integration point for Claude Code."""

    def __init__(
        self,
        config: Settings,
        sdk_manager: Optional[ClaudeSDKManager] = None,
        session_manager: Optional[SessionManager] = None,
    ):
        """Initialize Claude integration facade."""
        self.config = config
        self.sdk_manager = sdk_manager or ClaudeSDKManager(config)
        self.session_manager = session_manager

    async def run_command(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
        force_new: bool = False,
        allowed_tools_override: Optional[List[str]] = None,
        images: Optional[List[Dict[str, str]]] = None,
        interrupt_event: Optional["asyncio.Event"] = None,
    ) -> ClaudeResponse:
        """Run Claude Code command with full integration.

        *allowed_tools_override* restricts the tools the SDK may use for this
        run, overriding ``claude_allowed_tools`` (and ignoring
        ``DISABLE_TOOL_VALIDATION``). Used to run untrusted, unattended
        triggers (e.g. webhooks) with a read-only tool set.
        """
        logger.info(
            "Running Claude command",
            user_id=user_id,
            working_directory=str(working_directory),
            session_id=session_id,
            prompt_length=len(prompt),
            force_new=force_new,
        )

        # If no session_id provided, try to find an existing session for this
        # user+directory combination (auto-resume).
        # Skip auto-resume when force_new is set (e.g. after /new command).
        if not session_id and not force_new:
            existing_session = await self._find_resumable_session(
                user_id, working_directory
            )
            if existing_session:
                session_id = existing_session.session_id
                logger.info(
                    "Auto-resuming existing session for project",
                    session_id=session_id,
                    project_path=str(working_directory),
                    user_id=user_id,
                )

        # Get or create session
        session = await self.session_manager.get_or_create_session(
            user_id, working_directory, session_id
        )

        # Execute command
        try:
            # Continue session if we have an existing session with a real ID
            is_new = getattr(session, "is_new_session", False)
            should_continue = not is_new and bool(session.session_id)

            # For new sessions, don't pass session_id to Claude Code
            claude_session_id = session.session_id if should_continue else None

            try:
                response = await self._execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=claude_session_id,
                    continue_session=should_continue,
                    stream_callback=on_stream,
                    allowed_tools_override=allowed_tools_override,
                    images=images,
                    interrupt_event=interrupt_event,
                )
            except Exception as resume_error:
                # If resume failed *because the session is gone* (expired/missing
                # on Claude's side), retry as a fresh session. Do NOT restart on
                # timeouts or process crashes: a mutating tool may already have
                # run, so replaying the whole prompt would duplicate side effects
                # (and double the wait on timeout). Those propagate unchanged.
                if should_continue and _is_session_gone_error(resume_error):
                    logger.warning(
                        "Session resume failed, starting fresh session",
                        failed_session_id=claude_session_id,
                        error=str(resume_error),
                    )
                    # Clean up the stale session only if it has a real ID.
                    # A new session that crashed before getting an ID from
                    # Claude would have an empty session_id; calling
                    # remove_session("") would no-op against storage but
                    # still mutate the in-memory cache unexpectedly.
                    if session.session_id:
                        await self.session_manager.remove_session(session.session_id)

                    # Create a fresh session and retry
                    session = await self.session_manager.get_or_create_session(
                        user_id, working_directory
                    )
                    response = await self._execute(
                        prompt=prompt,
                        working_directory=working_directory,
                        session_id=None,
                        continue_session=False,
                        stream_callback=on_stream,
                        allowed_tools_override=allowed_tools_override,
                        images=images,
                        interrupt_event=interrupt_event,
                    )
                else:
                    raise

            # Update session (assigns real session_id for new sessions)
            await self.session_manager.update_session(session, response)

            # On resume Claude may fork to a *new* session_id. Don't clobber a
            # valid, differing response.session_id with the stale in-memory id —
            # re-key the session to the forked id so the conversation stays
            # resumable. Otherwise fall back to the session's stored id.
            if response.session_id and response.session_id != session.session_id:
                await self.session_manager.migrate_session_id(
                    session, response.session_id
                )
            else:
                response.session_id = session.session_id

            if not response.session_id:
                logger.warning(
                    "No session_id after execution; session cannot be resumed",
                    user_id=user_id,
                )

            logger.info(
                "Claude command completed",
                session_id=response.session_id,
                cost=response.cost,
                duration_ms=response.duration_ms,
                num_turns=response.num_turns,
                is_error=response.is_error,
            )

            return response

        except Exception as e:
            logger.error(
                "Claude command failed",
                error=str(e),
                user_id=user_id,
                session_id=session.session_id,
            )
            raise

    async def _execute(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable] = None,
        allowed_tools_override: Optional[List[str]] = None,
        images: Optional[List[Dict[str, str]]] = None,
        interrupt_event: Optional[asyncio.Event] = None,
    ) -> ClaudeResponse:
        """Execute command via SDK."""
        return await self.sdk_manager.execute_command(
            prompt=prompt,
            working_directory=working_directory,
            session_id=session_id,
            continue_session=continue_session,
            stream_callback=stream_callback,
            allowed_tools_override=allowed_tools_override,
            images=images,
            interrupt_event=interrupt_event,
        )

    async def _find_resumable_session(
        self,
        user_id: int,
        working_directory: Path,
    ) -> Optional["ClaudeSession"]:  # noqa: F821
        """Find the most recent resumable session for a user in a directory.

        First checks the bot's own SQLite storage. If nothing is found, falls
        back to scanning ``~/.claude/projects/`` for sessions started in
        VS Code or the CLI, so the user can seamlessly continue them via the
        bot.

        Returns the session if one exists that is non-expired and has a real
        (non-temporary) session ID from Claude. Returns None otherwise.
        """
        from .session import ClaudeSession

        sessions = await self.session_manager._get_user_sessions(user_id)

        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory
            and bool(s.session_id)
            and not s.is_expired(self.config.session_timeout_hours)
        ]

        if matching_sessions:
            return max(matching_sessions, key=lambda s: s.last_used)

        # Fallback: discover sessions from ~/.claude/projects/ (VS Code / CLI)
        known_ids = {s.session_id for s in sessions if s.session_id}
        local = find_latest_local_session(working_directory, exclude_ids=known_ids)
        # _encode_path() collapses distinct dirs to the same folder name, so the
        # folder match alone can return a *sibling* directory's session. Verify
        # the session's recorded cwd actually equals the requested directory
        # before resuming, or we'd resume cross-project history into the wrong
        # context. A blank/unresolvable cwd is rejected (fail closed).
        if local and local.cwd:
            try:
                same_dir = (
                    Path(local.cwd).resolve() == working_directory.resolve()
                )
            except (ValueError, OSError):
                same_dir = False
            if not same_dir:
                logger.info(
                    "Ignoring local session from a different directory",
                    session_id=local.session_id,
                    local_cwd=local.cwd,
                    requested=str(working_directory),
                )
                local = None
        elif local:
            local = None
        if local:
            logger.info(
                "Found local CLI/VS Code session to resume",
                session_id=local.session_id,
                cwd=local.cwd,
                source="~/.claude/projects",
            )
            # Wrap as a ClaudeSession so the rest of the flow works unchanged
            from datetime import UTC, datetime

            return ClaudeSession(
                session_id=local.session_id,
                user_id=user_id,
                project_path=working_directory,
                created_at=local.timestamp,
                last_used=datetime.now(UTC),
            )

        return None

    async def continue_session(
        self,
        user_id: int,
        working_directory: Path,
        prompt: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
    ) -> Optional[ClaudeResponse]:
        """Continue the most recent session."""
        logger.info(
            "Continuing session",
            user_id=user_id,
            working_directory=str(working_directory),
            has_prompt=bool(prompt),
        )

        # Get user's sessions
        sessions = await self.session_manager._get_user_sessions(user_id)

        # Find most recent session in this directory (exclude sessions without IDs)
        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory and bool(s.session_id)
        ]

        if not matching_sessions:
            logger.info("No matching sessions found", user_id=user_id)
            return None

        # Get most recent
        latest_session = max(matching_sessions, key=lambda s: s.last_used)

        # Continue session with default prompt if none provided
        # Claude CLI requires a prompt, so we use a placeholder
        return await self.run_command(
            prompt=prompt or "Please continue where we left off",
            working_directory=working_directory,
            user_id=user_id,
            session_id=latest_session.session_id,
            on_stream=on_stream,
        )

    async def get_session_info(
        self, session_id: str, user_id: int
    ) -> Optional[Dict[str, Any]]:
        """Get session information (scoped to requesting user)."""
        return await self.session_manager.get_session_info(session_id, user_id)

    async def get_user_sessions(self, user_id: int) -> List[Dict[str, Any]]:
        """Get all sessions for a user."""
        sessions = await self.session_manager._get_user_sessions(user_id)
        return [
            {
                "session_id": s.session_id,
                "project_path": str(s.project_path),
                "created_at": s.created_at.isoformat(),
                "last_used": s.last_used.isoformat(),
                "total_cost": s.total_cost,
                "message_count": s.message_count,
                "tools_used": s.tools_used,
                "expired": s.is_expired(self.config.session_timeout_hours),
            }
            for s in sessions
        ]

    async def cleanup_expired_sessions(self) -> int:
        """Clean up expired sessions."""
        return await self.session_manager.cleanup_expired_sessions()

    async def get_user_summary(self, user_id: int) -> Dict[str, Any]:
        """Get comprehensive user summary."""
        session_summary = await self.session_manager.get_user_session_summary(user_id)

        return {
            "user_id": user_id,
            **session_summary,
        }

    async def shutdown(self) -> None:
        """Shutdown integration and cleanup resources."""
        logger.info("Shutting down Claude integration")

        await self.cleanup_expired_sessions()

        logger.info("Claude integration shutdown complete")
