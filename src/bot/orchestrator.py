"""Message orchestrator — single entry point for all Telegram updates.

Routes messages based on agentic vs classic mode. In agentic mode, provides
a minimal conversational interface (3 commands, no inline keyboards). In
classic mode, delegates to existing full-featured handlers.
"""

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)
from uuid import uuid4

import structlog
from structlog.contextvars import bind_contextvars, unbind_contextvars
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..claude.exceptions import ClaudeProcessError, ClaudeTimeoutError
from ..claude.sdk_integration import StreamUpdate
from ..config.settings import Settings
from ..projects import PrivateTopicsUnavailableError
from ..security.secret_patterns import redact_secrets
from .features.file_handler import FileTooLargeError
from .middleware.rate_limit import estimate_message_cost
from .utils.claude_run import persist_interaction
from .utils.draft_streamer import DraftStreamer, generate_draft_id
from .utils.html_format import escape_html
from .utils.image_extractor import (
    MAX_IMAGES_PER_RESPONSE,
    ImageAttachment,
    open_validated,
    should_send_as_animation,
    should_send_as_photo,
    validate_image_path,
)
from .utils.upload_limits import exceeds_upload_limit

if TYPE_CHECKING:
    from .utils.formatting import FormattedMessage

logger = structlog.get_logger()

_MEDIA_TYPE_MAP = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
    "Skill": "\U0001f9e9",
    "AskUserQuestion": "\u2753",
    "EnterPlanMode": "\U0001f4cb",
    "ExitPlanMode": "\U0001f4cb",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


# Leading newline + spaces that indent a session preview under its list row.
_PREVIEW_INDENT = "\n   "


def _truncate(text: str, limit: int) -> str:
    """Shorten *text* to *limit* characters, marking the cut with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


@dataclass
class _SessionEntry:
    """One row of the /sessions list, normalised across its two sources."""

    session_id: str
    cwd: str
    when: datetime
    preview: str = ""
    last_preview: str = ""
    is_local: bool = False


@dataclass
class ActiveRequest:
    """Tracks an in-flight Claude request so it can be interrupted."""

    user_id: int
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted: bool = False
    progress_msg: Any = None  # telegram Message object


class MessageOrchestrator:
    """Routes messages based on mode. Single entry point for all Telegram updates."""

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps
        self._known_commands: frozenset[str] = frozenset()
        self._active_requests: Dict[int, ActiveRequest] = {}
        # Per-user lock serializing in-flight requests so a second concurrent
        # request can't clobber the first's ActiveRequest (orphaning its
        # heartbeat/progress message and stealing its Stop button).
        self._request_locks: Dict[int, asyncio.Lock] = {}

    _MAX_REQUEST_LOCKS = 10_000

    def _get_request_lock(self, user_id: int) -> asyncio.Lock:
        """Return (creating if needed) the per-user request serialization lock."""
        lock = self._request_locks.get(user_id)
        if lock is None:
            if len(self._request_locks) >= self._MAX_REQUEST_LOCKS:
                # Evict provably-unheld locks before growing the map, so an
                # unbounded user population (ALLOW_ALL_USERS) can't leak locks.
                # Only removes locks whose ``locked()`` is False, so an in-flight
                # request never loses the lock serializing it.
                for uid in [
                    uid
                    for uid, existing in self._request_locks.items()
                    if not existing.locked()
                ]:
                    self._request_locks.pop(uid, None)
            lock = asyncio.Lock()
            self._request_locks[user_id] = lock
        return lock

    async def _acquire_request_lock(self, user_id: int) -> asyncio.Lock:
        """Acquire and return the *canonical* per-user request lock.

        ``_get_request_lock`` is synchronous, so lookup+insert cannot interleave.
        But acquiring is not: between getting the reference and awaiting it, an
        eviction sweep (triggered by another user hitting ``_MAX_REQUEST_LOCKS``)
        can drop this still-unlocked lock from the map, after which a second
        request for the same user would create a *different* lock and run
        concurrently — exactly what the lock exists to prevent. Re-check that
        the acquired lock is still the mapped one and retry if it is not.
        """
        while True:
            lock = self._get_request_lock(user_id)
            await lock.acquire()
            if self._request_locks.get(user_id) is lock:
                return lock
            lock.release()

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings
            context.user_data.pop("_thread_context", None)

            is_sync_bypass = handler.__name__ == "sync_threads"
            is_start_bypass = handler.__name__ in {"start_command", "agentic_start"}
            message_thread_id = self._extract_message_thread_id(update)
            should_enforce = self.settings.enable_project_threads

            if should_enforce:
                if self.settings.project_threads_mode == "private":
                    should_enforce = not is_sync_bypass and not (
                        is_start_bypass and message_thread_id is None
                    )
                else:
                    should_enforce = not is_sync_bypass

            if should_enforce:
                allowed = await self._apply_thread_routing_context(update, context)
                if not allowed:
                    return

            try:
                await handler(update, context)
            finally:
                if should_enforce:
                    self._persist_thread_state(context)

        return wrapped

    async def _apply_thread_routing_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Enforce strict project-thread routing and load thread-local state."""
        manager = context.bot_data.get("project_threads_manager")
        if manager is None:
            await self._reject_for_thread_mode(
                update,
                "❌ <b>Project Thread Mode Misconfigured</b>\n\n"
                "Thread manager is not initialized.",
            )
            return False

        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return False

        if self.settings.project_threads_mode == "group":
            if chat.id != self.settings.project_threads_chat_id:
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False
        else:
            if getattr(chat, "type", "") != "private":
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False

        message_thread_id = self._extract_message_thread_id(update)
        if not message_thread_id:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        project = await manager.resolve_project(chat.id, message_thread_id)
        if not project:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        state_key = f"{chat.id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})
        state = thread_states.get(state_key, {})

        project_root = project.absolute_path.resolve()
        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        context.user_data["current_directory"] = current_dir
        context.user_data["claude_session_id"] = state.get("claude_session_id")
        # Scope the one-shot /new flag per thread so a /new in one topic does
        # not force a new session (orphaning the resumable one) in another.
        context.user_data["force_new_session"] = bool(
            state.get("force_new_session", False)
        )
        context.user_data["_thread_context"] = {
            "chat_id": chat.id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": project.slug,
            "project_root": str(project_root),
            "project_name": project.name,
        }
        return True

    def _persist_thread_state(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist compatibility keys back into per-thread state."""
        thread_context = context.user_data.get("_thread_context")
        if not thread_context:
            return

        project_root = Path(thread_context["project_root"])
        current_dir = context.user_data.get("current_directory", project_root)
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        thread_states = context.user_data.setdefault("thread_state", {})
        thread_states[thread_context["state_key"]] = {
            "current_directory": str(current_dir),
            "claude_session_id": context.user_data.get("claude_session_id"),
            "project_slug": thread_context["project_slug"],
            "force_new_session": bool(
                context.user_data.get("force_new_session", False)
            ),
        }

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return True if path is within root."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_message_thread_id(update: Update) -> Optional[int]:
        """Extract topic/thread id from update message for forum/direct topics."""
        message = update.effective_message
        if not message:
            return None
        message_thread_id = getattr(message, "message_thread_id", None)
        if isinstance(message_thread_id, int) and message_thread_id > 0:
            return message_thread_id
        dm_topic = getattr(message, "direct_messages_topic", None)
        topic_id = getattr(dm_topic, "topic_id", None) if dm_topic else None
        if isinstance(topic_id, int) and topic_id > 0:
            return topic_id
        # Telegram omits message_thread_id for the General topic in forum
        # supergroups; its canonical thread ID is 1.
        chat = update.effective_chat
        if chat and getattr(chat, "is_forum", False):
            return 1
        return None

    async def _reject_for_thread_mode(self, update: Update, message: str) -> None:
        """Send a guidance response when strict thread routing rejects an update."""
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                pass
            if query.message:
                await query.message.reply_text(message, parse_mode="HTML")
            return

        if update.effective_message:
            await update.effective_message.reply_text(message, parse_mode="HTML")

    def register_handlers(self, app: Application) -> None:
        """Register handlers based on mode."""
        if self.settings.agentic_mode:
            self._register_agentic_handlers(app)
        else:
            self._register_classic_handlers(app)

    def _register_agentic_handlers(self, app: Application) -> None:
        """Register agentic handlers: commands + text/file/photo."""
        from .handlers import command

        # Commands
        handlers = [
            ("start", self.agentic_start),
            ("new", self.agentic_new),
            ("status", self.agentic_status),
            ("verbose", self.agentic_verbose),
            ("repo", self.agentic_repo),
            ("sessions", self.agentic_sessions),
            ("schedule", self.cmd_schedule),
            ("events", self.cmd_events),
            ("restart", command.restart_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        # Derive known commands dynamically — avoids drift when commands change
        self._known_commands = frozenset(cmd for cmd, _ in handlers)

        for cmd, handler in handlers:
            app.add_handler(
                CommandHandler(
                    cmd,
                    self._inject_deps(handler),
                    filters=filters.UpdateType.MESSAGE,
                )
            )

        # Text messages -> Claude. UpdateType.MESSAGE ignores edited_message
        # updates (which would crash handlers reading update.message.text).
        app.add_handler(
            MessageHandler(
                filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND,
                self._inject_deps(self.agentic_text),
            ),
            group=10,
        )

        # Unknown slash commands -> Claude (passthrough in agentic mode).
        # Registered commands are handled by CommandHandlers in group 0
        # (higher priority). This catches any /command not matched there and
        # forwards it to Claude, while skipping known commands to avoid
        # double-firing.
        app.add_handler(
            MessageHandler(
                filters.UpdateType.MESSAGE & filters.COMMAND,
                self._inject_deps(self._handle_unknown_command),
            ),
            group=10,
        )

        # File uploads -> Claude
        app.add_handler(
            MessageHandler(
                filters.UpdateType.MESSAGE & filters.Document.ALL,
                self._inject_deps(self.agentic_document),
            ),
            group=10,
        )

        # Photo uploads -> Claude
        app.add_handler(
            MessageHandler(
                filters.UpdateType.MESSAGE & filters.PHOTO,
                self._inject_deps(self.agentic_photo),
            ),
            group=10,
        )

        # Voice messages -> transcribe -> Claude
        app.add_handler(
            MessageHandler(
                filters.UpdateType.MESSAGE & filters.VOICE,
                self._inject_deps(self.agentic_voice),
            ),
            group=10,
        )

        # Stop button callback (priority — bypasses sequential lock)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_stop_callback),
                pattern=r"^stop:",
            )
        )

        # Only cd: callbacks (for project selection), scoped by pattern
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._agentic_callback),
                pattern=r"^cd:",
            )
        )

        # resume: callbacks (for session switching from /sessions)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._agentic_resume_callback),
                pattern=r"^resume:",
            )
        )

        logger.info("Agentic handlers registered")

    def _register_classic_handlers(self, app: Application) -> None:
        """Register full classic handler set (moved from core.py)."""
        from .handlers import callback, command, message

        handlers = [
            ("start", command.start_command),
            ("help", command.help_command),
            ("new", command.new_session),
            ("continue", command.continue_session),
            ("end", command.end_session),
            ("ls", command.list_files),
            ("cd", command.change_directory),
            ("pwd", command.print_working_directory),
            ("projects", command.show_projects),
            ("status", command.session_status),
            ("export", command.export_session),
            ("actions", command.quick_actions),
            ("git", command.git_command),
            ("restart", command.restart_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        for cmd, handler in handlers:
            app.add_handler(
                CommandHandler(
                    cmd,
                    self._inject_deps(handler),
                    filters=filters.UpdateType.MESSAGE,
                )
            )

        # UpdateType.MESSAGE ignores edited_message updates (which would crash
        # handlers reading update.message.text).
        app.add_handler(
            MessageHandler(
                filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND,
                self._inject_deps(message.handle_text_message),
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(
                filters.UpdateType.MESSAGE & filters.Document.ALL,
                self._inject_deps(message.handle_document),
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(
                filters.UpdateType.MESSAGE & filters.PHOTO,
                self._inject_deps(message.handle_photo),
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(
                filters.UpdateType.MESSAGE & filters.VOICE,
                self._inject_deps(message.handle_voice),
            ),
            group=10,
        )
        app.add_handler(
            CallbackQueryHandler(self._inject_deps(callback.handle_callback_query))
        )

        logger.info("Classic handlers registered (13 commands + full handler set)")

    async def get_bot_commands(self) -> list:  # type: ignore[type-arg]
        """Return bot commands appropriate for current mode."""
        if self.settings.agentic_mode:
            commands = [
                BotCommand("start", "Start the bot"),
                BotCommand("new", "Start a fresh session"),
                BotCommand("status", "Show session status"),
                BotCommand("verbose", "Set output verbosity (0/1/2)"),
                BotCommand("repo", "List repos / switch workspace"),
                BotCommand("sessions", "List sessions (local + bot)"),
                BotCommand("schedule", "Manage scheduled jobs (admin)"),
                BotCommand("events", "Show failed webhook events (admin)"),
                BotCommand("restart", "Restart the bot"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands
        else:
            commands = [
                BotCommand("start", "Start bot and show help"),
                BotCommand("help", "Show available commands"),
                BotCommand("new", "Clear context and start fresh session"),
                BotCommand("continue", "Explicitly continue last session"),
                BotCommand("end", "End current session and clear context"),
                BotCommand("ls", "List files in current directory"),
                BotCommand("cd", "Change directory (resumes project session)"),
                BotCommand("pwd", "Show current directory"),
                BotCommand("projects", "Show all projects"),
                BotCommand("status", "Show session status"),
                BotCommand("export", "Export current session"),
                BotCommand("actions", "Show quick actions"),
                BotCommand("git", "Git repository commands"),
                BotCommand("restart", "Restart the bot"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands

    # --- Agentic handlers ---

    async def agentic_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Brief welcome, no buttons."""
        user = update.effective_user
        sync_line = ""
        if (
            self.settings.enable_project_threads
            and self.settings.project_threads_mode == "private"
        ):
            if (
                not update.effective_chat
                or getattr(update.effective_chat, "type", "") != "private"
            ):
                await update.message.reply_text(
                    "🚫 <b>Private Topics Mode</b>\n\n"
                    "Use this bot in a private chat and run <code>/start</code> there.",
                    parse_mode="HTML",
                )
                return
            manager = context.bot_data.get("project_threads_manager")
            if manager:
                try:
                    result = await manager.sync_topics(
                        context.bot,
                        chat_id=update.effective_chat.id,
                    )
                    sync_line = (
                        "\n\n🧵 Topics synced"
                        f" (created {result.created}, reused {result.reused})."
                    )
                except PrivateTopicsUnavailableError:
                    await update.message.reply_text(
                        manager.private_topics_unavailable_message(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    sync_line = "\n\n🧵 Topic sync failed. Run /sync_threads to retry."
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = f"<code>{current_dir}/</code>"

        safe_name = escape_html(user.first_name)
        await update.message.reply_text(
            f"Hi {safe_name}! I'm your AI coding assistant.\n"
            f"Just tell me what you need — I can read, write, and run code.\n\n"
            f"Working in: {dir_display}\n"
            f"Commands: /new (reset) · /status"
            f"{sync_line}",
            parse_mode="HTML",
        )

    async def agentic_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Reset session, one-line confirmation."""
        context.user_data["claude_session_id"] = None
        context.user_data["session_started"] = True
        context.user_data["force_new_session"] = True

        await update.message.reply_text("Session reset. What's next?")

    async def agentic_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Compact one-line status, no buttons."""
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = str(current_dir)

        session_id = context.user_data.get("claude_session_id")
        session_status = "active" if session_id else "none"

        # Cost info
        cost_str = ""
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            try:
                user_status = rate_limiter.get_user_status(update.effective_user.id)
                cost_usage = user_status.get("cost_usage", {})
                current_cost = cost_usage.get("current", 0.0)
                cost_str = f" · Cost: ${current_cost:.2f}"
            except Exception:
                pass

        await update.message.reply_text(
            f"📂 {dir_display} · Session: {session_status}{cost_str}"
        )

    def _get_verbose_level(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Return effective verbose level: per-user override or global default."""
        user_override = context.user_data.get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    async def agentic_verbose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set output verbosity: /verbose [0|1|2]."""
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = self._get_verbose_level(context)
            labels = {0: "quiet", 1: "normal", 2: "detailed"}
            await update.message.reply_text(
                f"Verbosity: <b>{current}</b> ({labels.get(current, '?')})\n\n"
                "Usage: <code>/verbose 0|1|2</code>\n"
                "  0 = quiet (final response only)\n"
                "  1 = normal (tools + reasoning)\n"
                "  2 = detailed (tools with inputs + reasoning)",
                parse_mode="HTML",
            )
            return

        try:
            level = int(args[0])
            if level not in (0, 1, 2):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Please use: /verbose 0, /verbose 1, or /verbose 2"
            )
            return

        context.user_data["verbose_level"] = level
        labels = {0: "quiet", 1: "normal", 2: "detailed"}
        await update.message.reply_text(
            f"Verbosity set to <b>{level}</b> ({labels[level]})",
            parse_mode="HTML",
        )

    def _format_verbose_progress(
        self,
        activity_log: List[Dict[str, Any]],
        verbose_level: int,
        start_time: float,
    ) -> str:
        """Build the progress message text based on activity so far."""
        if not activity_log:
            return "Working..."

        elapsed = time.monotonic() - start_time
        lines: List[str] = [f"Working... ({elapsed:.0f}s)\n"]

        for entry in activity_log[-15:]:  # Show last 15 entries max
            kind = entry.get("kind", "tool")
            if kind == "text":
                # Claude's intermediate reasoning/commentary
                snippet = entry.get("detail", "")
                if verbose_level >= 2:
                    lines.append(f"\U0001f4ac {snippet}")
                else:
                    # Level 1: one short line
                    lines.append(f"\U0001f4ac {snippet[:80]}")
            else:
                # Tool call
                icon = _tool_icon(entry["name"])
                if verbose_level >= 2 and entry.get("detail"):
                    lines.append(f"{icon} {entry['name']}: {entry['detail']}")
                else:
                    lines.append(f"{icon} {entry['name']}")

        if len(activity_log) > 15:
            lines.insert(1, f"... ({len(activity_log) - 15} earlier entries)\n")

        return "\n".join(lines)

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a short summary of tool input for verbose level 2."""
        if not tool_input:
            return ""
        if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "")
            if path:
                # Show just the filename, not the full path
                return path.rsplit("/", 1)[-1]
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            if pattern:
                return pattern[:60]
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if cmd:
                return redact_secrets(cmd[:100])[:80]
        if tool_name in ("WebFetch", "WebSearch"):
            url = tool_input.get("url", "")
            if url:
                # URL query params often carry tokens that don't match the
                # generic secret patterns — mask every query-string value.
                url = re.sub(r"([?&][^=&\s]+=)[^&\s]+", r"\1***", url)
                return redact_secrets(url[:200])[:60]
            return redact_secrets(tool_input.get("query", "")[:200])[:60]
        if tool_name == "Task":
            desc = tool_input.get("description", "")
            if desc:
                return redact_secrets(desc[:200])[:60]
        # Generic: show first key's value
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return redact_secrets(v[:200])[:60]
        return ""

    @staticmethod
    def _start_typing_heartbeat(
        chat: Any,
        interval: float = 2.0,
    ) -> "asyncio.Task[None]":
        """Start a background typing indicator task.

        Sends typing every *interval* seconds, independently of
        stream events. Cancel the returned task in a ``finally``
        block.
        """

        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        await chat.send_action("typing")
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        return asyncio.create_task(_heartbeat())

    @staticmethod
    def _build_stop_kb(user_id: int) -> InlineKeyboardMarkup:
        """Build the inline 'Stop' keyboard that interrupts a running request."""
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("Stop", callback_data=f"stop:{user_id}")]]
        )

    @staticmethod
    def _error_with_ref(message: str, request_id: str) -> str:
        """Append a short correlation id to a user-facing error for log grep."""
        return f"{message}\n\n<code>ref: {request_id[:8]}</code>"

    @contextlib.asynccontextmanager
    async def _claude_run(
        self,
        *,
        user_id: int,
        chat: Any,
        progress_msg: Any,
    ) -> AsyncIterator[asyncio.Event]:
        """Own the lifecycle of one in-flight Claude run.

        Sets up on entry: the interrupt event (fed to ``run_command`` and the
        stream callback so the Stop button can cancel the run), the per-user
        serialization lock (so a second concurrent request can't clobber this
        one's ActiveRequest), the ActiveRequest registration (so the Stop
        callback can find and interrupt it) and the typing heartbeat. Tears all
        of it down on exit — heartbeat cancelled, ActiveRequest dropped, lock
        released — whether the body returns, raises or is cancelled. Yields the
        interrupt event.

        The Stop keyboard itself is built by the caller via ``_build_stop_kb``
        and shown on *progress_msg* before entry (created fresh with the button
        for the text path, edited onto an existing message for the media path),
        because those two paths differ in how the progress message is made.
        """
        interrupt_event = asyncio.Event()
        request_lock = await self._acquire_request_lock(user_id)
        self._active_requests[user_id] = ActiveRequest(
            user_id=user_id,
            interrupt_event=interrupt_event,
            progress_msg=progress_msg,
        )
        heartbeat = self._start_typing_heartbeat(chat)
        try:
            yield interrupt_event
        finally:
            heartbeat.cancel()
            self._active_requests.pop(user_id, None)
            request_lock.release()

    def _make_stream_callback(
        self,
        verbose_level: int,
        progress_msg: Any,
        tool_log: List[Dict[str, Any]],
        start_time: float,
        mcp_images: Optional[List[ImageAttachment]] = None,
        approved_directory: Optional[Path] = None,
        draft_streamer: Optional[DraftStreamer] = None,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        interrupt_event: Optional[asyncio.Event] = None,
    ) -> Optional[Callable[[StreamUpdate], Any]]:
        """Create a stream callback for verbose progress updates.

        When *mcp_images* is provided, the callback also intercepts
        ``send_image_to_user`` tool calls and collects validated
        :class:`ImageAttachment` objects for later Telegram delivery.

        When *draft_streamer* is provided, tool activity and assistant
        text are streamed to the user in real time via
        ``sendMessageDraft``.

        Returns None when verbose_level is 0 **and** no MCP image
        collection or draft streaming is requested.
        Typing indicators are handled by a separate heartbeat task.
        """
        need_mcp_intercept = mcp_images is not None and approved_directory is not None

        if verbose_level == 0 and not need_mcp_intercept and draft_streamer is None:
            return None

        last_edit_time = [0.0]  # mutable container for closure

        async def _on_stream(update_obj: StreamUpdate) -> None:
            # Stop all streaming activity after interrupt
            if interrupt_event is not None and interrupt_event.is_set():
                return

            # Intercept send_image_to_user MCP tool calls.
            # The SDK namespaces MCP tools as "mcp__<server>__<tool>",
            # so match both the bare name and the namespaced variant.
            if update_obj.tool_calls and need_mcp_intercept:
                for tc in update_obj.tool_calls:
                    tc_name = tc.get("name", "")
                    if tc_name == "send_image_to_user" or tc_name.endswith(
                        "__send_image_to_user"
                    ):
                        tc_input = tc.get("input", {})
                        file_path = tc_input.get("file_path", "")
                        caption = tc_input.get("caption", "")
                        img = validate_image_path(
                            file_path, approved_directory, caption
                        )
                        # Cap total collected images per run so a flood of
                        # send_image_to_user calls can't spawn unbounded
                        # sequential Telegram sends (animations/documents each
                        # sleep 0.5s) or trip flood limits.
                        if img and len(mcp_images) < MAX_IMAGES_PER_RESPONSE:
                            mcp_images.append(img)

            # Capture tool calls
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    name = tc.get("name", "unknown")
                    detail = self._summarize_tool_input(name, tc.get("input", {}))
                    if verbose_level >= 1:
                        tool_log.append(
                            {"kind": "tool", "name": name, "detail": detail}
                        )
                    if draft_streamer:
                        icon = _tool_icon(name)
                        line = (
                            f"{icon} {name}: {detail}" if detail else f"{icon} {name}"
                        )
                        await draft_streamer.append_tool(line)

            # Capture assistant text (reasoning / commentary)
            if update_obj.type == "assistant" and update_obj.content:
                text = update_obj.content.strip()
                if text:
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        # Redact secrets before this reasoning snippet reaches
                        # Telegram via the draft stream or verbose progress edit.
                        first_line = redact_secrets(first_line)
                        if verbose_level >= 1:
                            tool_log.append(
                                {"kind": "text", "detail": first_line[:120]}
                            )
                        if draft_streamer:
                            await draft_streamer.append_tool(
                                f"\U0001f4ac {first_line[:120]}"
                            )

            # Stream text to user via draft (prefer token deltas;
            # skip full assistant messages to avoid double-appending)
            if draft_streamer and update_obj.content:
                if update_obj.type == "stream_delta":
                    # Redact secrets from live token deltas — the final message
                    # is redacted too, but the draft must not leak first.
                    await draft_streamer.append_text(redact_secrets(update_obj.content))

            # Throttle progress message edits to avoid Telegram rate limits.
            # A streamer that disabled itself (draft rejected by Telegram) is
            # treated as absent, so the user still sees progress.
            if verbose_level >= 1 and (
                draft_streamer is None or not draft_streamer.enabled
            ):
                now = time.time()
                if (now - last_edit_time[0]) >= 2.0 and tool_log:
                    last_edit_time[0] = now
                    new_text = self._format_verbose_progress(
                        tool_log, verbose_level, start_time
                    )
                    try:
                        await progress_msg.edit_text(
                            new_text, reply_markup=reply_markup
                        )
                    except Exception:
                        pass

        return _on_stream

    async def _send_images(
        self,
        update: Update,
        images: List[ImageAttachment],
        reply_to_message_id: Optional[int] = None,
        caption: Optional[str] = None,
        caption_parse_mode: Optional[str] = None,
    ) -> bool:
        """Send extracted images as a media group (album) or documents.

        If *caption* is provided and fits (≤1024 chars), it is attached to the
        photo / first album item so text + images appear as one message.

        Returns True if the caption was successfully embedded in the photo message.
        """
        animations: List[ImageAttachment] = []
        photos: List[ImageAttachment] = []
        documents: List[ImageAttachment] = []
        for img in images:
            if should_send_as_animation(img.path):
                # GIFs must go through reply_animation() — reply_photo flattens
                # them into a static frame.
                animations.append(img)
            elif should_send_as_photo(img.path):
                photos.append(img)
            else:
                documents.append(img)

        # Telegram caption limit. Only embed the response-text caption in a
        # single photo (mixed animation/document batches send separately).
        use_caption = bool(
            caption
            and len(caption) <= 1024
            and photos
            and not documents
            and not animations
        )
        caption_sent = False

        # Send animations (GIFs) individually — they cannot be mixed into a
        # photo album.
        for img in animations:
            try:
                with open_validated(img) as f:
                    await update.message.reply_animation(
                        animation=f,
                        reply_to_message_id=reply_to_message_id,
                    )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(
                    "Failed to send animation",
                    path=str(img.path),
                    error=str(e),
                )

        # Send raster photos as a single album (Telegram groups 2-10 items)
        if photos:
            try:
                if len(photos) == 1:
                    # Prefer the per-image caption captured from the MCP
                    # send_image_to_user call when one was supplied; otherwise
                    # fall back to the response-text caption.
                    single = photos[0]
                    per_image_caption = (
                        single.original_reference
                        if single.original_reference
                        and single.original_reference != str(single.path)
                        else None
                    )
                    if per_image_caption and len(per_image_caption) > 1024:
                        # Telegram rejects captions over 1024 chars — truncate so
                        # the photo still delivers instead of silently failing.
                        per_image_caption = per_image_caption[:1021] + "…"
                    if per_image_caption:
                        photo_caption: Optional[str] = per_image_caption
                        photo_parse_mode: Optional[str] = None
                    elif use_caption:
                        photo_caption = caption
                        photo_parse_mode = caption_parse_mode
                    else:
                        photo_caption = None
                        photo_parse_mode = None
                    with open_validated(single) as f:
                        await update.message.reply_photo(
                            photo=f,
                            reply_to_message_id=reply_to_message_id,
                            caption=photo_caption,
                            parse_mode=photo_parse_mode,
                        )
                    caption_sent = use_caption and not per_image_caption
                else:
                    # ExitStack closes every handle on exit — including the
                    # case where open() fails partway through the loop, which a
                    # plain try/finally around send_media_group would leak.
                    with contextlib.ExitStack() as stack:
                        media = []
                        for idx, img in enumerate(photos[:10]):
                            fh = stack.enter_context(open_validated(img))
                            media.append(
                                InputMediaPhoto(
                                    media=fh,
                                    caption=(
                                        caption if use_caption and idx == 0 else None
                                    ),
                                    parse_mode=(
                                        caption_parse_mode
                                        if use_caption and idx == 0
                                        else None
                                    ),
                                )
                            )
                        await update.message.chat.send_media_group(
                            media=media,
                            reply_to_message_id=reply_to_message_id,
                        )
                        caption_sent = use_caption
                    if len(photos) > 10:
                        await update.message.reply_text(
                            f"Note: only the first 10 of {len(photos)} images "
                            "were sent (Telegram album limit)."
                        )
            except Exception as e:
                logger.warning("Failed to send photo album", error=str(e))

        # Send SVGs / large files as documents (one by one — can't mix in album)
        for img in documents:
            try:
                with open_validated(img) as f:
                    await update.message.reply_document(
                        document=f,
                        filename=img.path.name,
                        reply_to_message_id=reply_to_message_id,
                    )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(
                    "Failed to send document image",
                    path=str(img.path),
                    error=str(e),
                )

        return caption_sent

    async def _deliver_response(
        self,
        update: Update,
        formatted_messages: List["FormattedMessage"],
        images: List[ImageAttachment],
    ) -> None:
        """Send a formatted Claude response back to the user.

        Shared by the text and media agentic handlers. When there is a single
        message that fits, text + images are combined into one captioned photo;
        otherwise the text messages are sent (HTML, falling back to plain text
        then a delivery-error notice) followed by the images. The messages are
        assumed to already have secrets redacted by the caller.
        """
        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                try:
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,  # No keyboards in agentic mode
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                except Exception as send_err:
                    logger.warning(
                        "Failed to send HTML response, retrying as plain text",
                        error=str(send_err),
                        message_index=i,
                    )
                    try:
                        await update.message.reply_text(
                            message.text,
                            reply_markup=None,
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                    except Exception as plain_err:
                        await update.message.reply_text(
                            f"Failed to deliver response "
                            f"(Telegram error: {str(plain_err)[:150]}). "
                            f"Please try again.",
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

    async def _run_and_format(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
        user_id: int,
        progress_msg: Any,
        request_id: str,
        interrupt_event: asyncio.Event,
        mcp_images: List[ImageAttachment],
        stop_kb: Optional[InlineKeyboardMarkup] = None,
        images: Optional[List[Dict[str, str]]] = None,
        draft_streamer: Optional[DraftStreamer] = None,
    ) -> Tuple[List["FormattedMessage"], bool]:
        """Run one prompt through Claude and turn the outcome into messages.

        Single home for everything both agentic paths (text and media) must do
        identically: budget reservation, the run itself, the ``is_error`` branch,
        cost settlement, persistence and formatting. Keeping one copy is what
        stops the two paths from drifting on *when* a run is charged or stored.

        Must be called inside ``_claude_run`` (it needs that run's
        *interrupt_event*). Returns ``(formatted_messages, success)``. An empty
        message list means the outcome was already reported in place on
        *progress_msg* (integration missing, budget refused) — the caller must
        leave that message alone and stop.
        """
        from .handlers.message import (
            _format_error_message,
            _update_working_directory_from_claude_response,
        )
        from .utils.formatting import FormattedMessage, ResponseFormatter

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration.",
                reply_markup=None,
            )
            return [], False

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")
        # Check if /new was used — skip auto-resume for this first message. The
        # flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.monotonic(),
            mcp_images=mcp_images,
            approved_directory=self.settings.approved_directory,
            draft_streamer=draft_streamer,
            reply_markup=stop_kb,
            interrupt_event=interrupt_event,
        )

        # Rate limiting was already enforced by rate_limit_middleware (group -1);
        # re-checking here would double-charge the token bucket. The money for
        # this specific run is held below and released in the finally on every
        # path (success, soft error, exception, cancel).
        rate_limiter = context.bot_data.get("rate_limiter")
        reservation_id: Optional[str] = None
        actual_cost = 0.0
        if rate_limiter:
            reservation_id, reserve_error = await rate_limiter.reserve_cost(
                user_id, estimate_message_cost(update)
            )
            if reserve_error:
                await progress_msg.edit_text(
                    escape_html(reserve_error), reply_markup=None
                )
                return [], False

        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                images=images,
                interrupt_event=interrupt_event,
            )

            context.user_data["claude_session_id"] = claude_response.session_id

            # Charge whatever Claude reports, including on a run flagged
            # is_error: error_max_turns and the max_budget_usd cap both burn real
            # tokens before failing, and a budget that ignores them lets a
            # retry loop spend without moving the counter. A run that produced
            # no ResultMessage at all reports cost 0 and settles at 0.
            actual_cost = claude_response.cost

            # Persist every run, soft error included, so history, cost
            # reporting and the audit trail see the same set of runs whether the
            # prompt arrived as text or as media.
            await persist_interaction(
                context.bot_data.get("storage"),
                user_id,
                prompt,
                claude_response,
            )

            # The run completed without raising but the SDK flagged an error
            # (e.g. no ResultMessage / budget cap). Surface it explicitly and
            # keep force_new set so the next message still starts fresh.
            if claude_response.is_error:
                return [
                    FormattedMessage(
                        self._error_with_ref(
                            _format_error_message(
                                claude_response.error_type
                                or "Claude returned an error."
                            ),
                            request_id,
                        ),
                        parse_mode="HTML",
                    )
                ], False

            # New session produced a usable result — clear the one-shot flag now.
            if force_new:
                context.user_data["force_new_session"] = False

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            formatter = ResponseFormatter(self.settings)
            # Redact secrets from the response body before it leaves the bot —
            # tool OUTPUT (e.g. a printenv dump) can land here.
            response_content = redact_secrets(claude_response.content or "")
            if claude_response.interrupted:
                response_content = response_content + "\n\n_(Interrupted by user)_"
            return formatter.format_claude_response(response_content), True

        except Exception as e:
            logger.error(
                "Claude integration failed",
                error=str(e),
                user_id=user_id,
            )
            # A timeout or a dead CLI process leaves the resumed session in an
            # unknown state; resuming it on the next message just reproduces the
            # failure. Drop the stored id so the next message starts a fresh
            # session without the user having to run /new.
            if isinstance(e, (ClaudeTimeoutError, ClaudeProcessError)):
                context.user_data["claude_session_id"] = None
            return [
                FormattedMessage(
                    self._error_with_ref(_format_error_message(e), request_id),
                    parse_mode="HTML",
                )
            ], False
        finally:
            # Always release the hold, so a failed run cannot leave budget
            # blocked until the sweeper runs.
            if rate_limiter and reservation_id:
                await rate_limiter.settle_reservation(reservation_id, actual_cost)

    def _drafts_supported(self, chat: Any, message_thread_id: Optional[int]) -> bool:
        """Return True when draft streaming may be attempted for this chat.

        Private chats plus forum topics (the multi-project layout, where the
        user spends most of their time). A draft rejected by Telegram disables
        the streamer, and ``_make_stream_callback`` then falls back to verbose
        progress edits, so attempting it costs nothing.
        """
        if not self.settings.enable_stream_drafts:
            return False
        if chat.type == "private":
            return True
        return message_thread_id is not None and bool(getattr(chat, "is_forum", False))

    async def agentic_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        # Correlation id tying this message through the Claude run, storage and
        # reply together in the logs. Bound so every downstream log carries it;
        # unbound in the finally below.
        request_id = uuid4().hex
        bind_contextvars(request_id=request_id)
        try:
            user_id = update.effective_user.id
            message_text = update.message.text

            logger.info(
                "Agentic text message",
                user_id=user_id,
                message_length=len(message_text),
            )

            chat = update.message.chat
            await chat.send_action("typing")

            # Stop button lives on the progress message; the run lifecycle
            # (interrupt event, lock, ActiveRequest, heartbeat) is owned by the
            # _claude_run context manager.
            stop_kb = self._build_stop_kb(user_id)
            progress_msg = await update.message.reply_text(
                "Working...", reply_markup=stop_kb
            )

            success = True
            draft_streamer: Optional[DraftStreamer] = None
            mcp_images: List[ImageAttachment] = []
            formatted_messages: List["FormattedMessage"] = []
            try:
                async with self._claude_run(
                    user_id=user_id, chat=chat, progress_msg=progress_msg
                ) as interrupt_event:
                    # Drafts in private chats and in forum topics (the
                    # multi-project layout). A topic that rejects drafts falls
                    # back to verbose progress edits on its own.
                    if self._drafts_supported(chat, update.message.message_thread_id):
                        draft_streamer = DraftStreamer(
                            bot=context.bot,
                            chat_id=chat.id,
                            draft_id=generate_draft_id(),
                            message_thread_id=update.message.message_thread_id,
                            throttle_interval=self.settings.stream_draft_interval,
                        )

                    formatted_messages, success = await self._run_and_format(
                        update=update,
                        context=context,
                        prompt=message_text,
                        user_id=user_id,
                        progress_msg=progress_msg,
                        request_id=request_id,
                        interrupt_event=interrupt_event,
                        mcp_images=mcp_images,
                        stop_kb=stop_kb,
                        draft_streamer=draft_streamer,
                    )
                    if not formatted_messages:
                        # Outcome already reported in place on the progress
                        # message (integration missing, budget refused).
                        return
            finally:
                if draft_streamer:
                    try:
                        await draft_streamer.flush()
                    except Exception:
                        logger.debug(
                            "Draft flush failed in finally block", user_id=user_id
                        )

            try:
                await progress_msg.delete()
            except Exception:
                logger.debug("Failed to delete progress message, ignoring")

            # Use MCP-collected images (from send_image_to_user tool calls)
            await self._deliver_response(update, formatted_messages, mcp_images)

            # Audit log
            audit_logger = context.bot_data.get("audit_logger")
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user_id,
                    command="text_message",
                    args=[message_text[:100]],
                    success=success,
                )
        finally:
            unbind_contextvars("request_id")

    async def agentic_document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process file upload -> Claude, minimal chrome."""
        user_id = update.effective_user.id
        document = update.message.document

        logger.info(
            "Agentic document upload",
            user_id=user_id,
            filename=document.file_name,
        )

        # Security validation
        security_validator = context.bot_data.get("security_validator")
        if security_validator:
            # ``file_name`` is optional in Telegram and may be None for nameless
            # uploads — fall back to a placeholder so the typed validator never
            # receives None (matches the classic path in handlers/message.py).
            valid, error = security_validator.validate_filename(
                document.file_name or "document"
            )
            if not valid:
                await update.message.reply_text(f"File rejected: {error}")
                return

        # Size check against the declared metadata size. ``file_size`` is
        # Optional — Telegram may omit it — so an unknown size is treated as
        # "not yet verified" rather than 0, and the real byte length is
        # re-checked after download below.
        max_size = self.settings.max_file_upload_size_bytes
        max_mb = self.settings.max_file_upload_size_mb
        if exceeds_upload_limit(document.file_size, max_size):
            await update.message.reply_text(
                f"File too large ({document.file_size / 1024 / 1024:.1f}MB). "
                f"Max: {max_mb}MB."
            )
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        # Try enhanced file handler, fall back to basic
        features = context.bot_data.get("features")
        file_handler = features.get_file_handler() if features else None
        prompt: Optional[str] = None

        if file_handler:
            try:
                processed_file = await file_handler.handle_document_upload(
                    document,
                    user_id,
                    update.message.caption or "Please review this file:",
                )
                prompt = processed_file.prompt
            except FileTooLargeError as e:
                # Falling back would download the same over-limit file again.
                await progress_msg.edit_text(str(e))
                return
            except Exception as e:
                logger.warning(
                    "Enhanced file handler failed, falling back to basic",
                    error=str(e),
                    user_id=user_id,
                )
                file_handler = None

        if not file_handler:
            file = await document.get_file()
            # Re-check the resolved Telegram File metadata: the Document's
            # declared size may be absent or understated.
            if exceeds_upload_limit(getattr(file, "file_size", None), max_size):
                await progress_msg.edit_text(
                    f"File too large "
                    f"({getattr(file, 'file_size') / 1024 / 1024:.1f}MB). "
                    f"Max: {max_mb}MB."
                )
                return
            file_bytes = await file.download_as_bytearray()
            # Final hard cap on the bytes actually received, so a metadata size
            # that lied (or was missing) cannot push an over-limit payload
            # through to Claude.
            if exceeds_upload_limit(len(file_bytes), max_size):
                await progress_msg.edit_text(
                    f"File too large ({len(file_bytes) / 1024 / 1024:.1f}MB). "
                    f"Max: {max_mb}MB."
                )
                return
            try:
                content = file_bytes.decode("utf-8")
                if len(content) > 50000:
                    content = content[:50000] + "\n... (truncated)"
                caption = update.message.caption or "Please review this file:"
                # Pick a fence longer than any backtick run in the content so
                # embedded triple-backticks can't close the block early and
                # corrupt the prompt.
                longest_run = max(
                    (len(m) for m in re.findall(r"`+", content)), default=0
                )
                fence = "`" * max(3, longest_run + 1)
                prompt = (
                    f"{caption}\n\n**File:** `{document.file_name}`\n\n"
                    f"{fence}\n{content}\n{fence}"
                )
            except UnicodeDecodeError:
                await progress_msg.edit_text(
                    "Unsupported file format. Must be text-based (UTF-8)."
                )
                return

        # Process with Claude
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return

        # Route through the shared media handler: it owns the Stop keyboard,
        # ActiveRequest registration, interrupt event, secret redaction, and
        # is_error handling (so the document path inherits them, and a failed
        # run can no longer leave a dead "Working..." message behind).
        try:
            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
            )
        except Exception as e:
            from .handlers.message import _format_error_message

            # progress_msg may already be deleted at this point — reply fresh
            # instead of editing a deleted message (which would mask the error).
            await update.effective_message.reply_text(
                _format_error_message(e), parse_mode="HTML"
            )
            logger.error("Claude file processing failed", error=str(e), user_id=user_id)

    async def agentic_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process photo -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        image_handler = features.get_image_handler() if features else None

        if not image_handler:
            await update.message.reply_text("Photo processing is not available.")
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        try:
            photo = update.message.photo[-1]
            processed_image = await image_handler.process_image(
                photo, update.message.caption
            )
            fmt = (processed_image.metadata or {}).get("format", "png")
            images = [
                {
                    "data": processed_image.base64_data,
                    "media_type": _MEDIA_TYPE_MAP.get(fmt, "image/png"),
                }
            ]
            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_image.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
                images=images,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            # progress_msg may already be deleted by the media handler — reply
            # fresh instead of editing a deleted message (which masks the error).
            await update.effective_message.reply_text(
                _format_error_message(e), parse_mode="HTML"
            )
            logger.error(
                "Claude photo processing failed", error=str(e), user_id=user_id
            )

    async def agentic_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Transcribe voice message -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        voice_handler = features.get_voice_handler() if features else None

        if not voice_handler:
            await update.message.reply_text(self._voice_unavailable_message())
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Transcribing...")

        try:
            voice = update.message.voice
            processed_voice = await voice_handler.process_voice_message(
                voice, update.message.caption
            )

            await progress_msg.edit_text("Working...")
            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_voice.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            # progress_msg may already be deleted by the media handler — reply
            # fresh instead of editing a deleted message (which masks the error).
            await update.effective_message.reply_text(
                _format_error_message(e), parse_mode="HTML"
            )
            logger.error(
                "Claude voice processing failed", error=str(e), user_id=user_id
            )

    async def _handle_agentic_media_message(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
        progress_msg: Any,
        user_id: int,
        chat: Any,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        """Run a media-derived prompt through Claude and send responses."""
        # Correlation id tying this media message through the Claude run,
        # storage and reply together in the logs. Unbound in the finally below.
        request_id = uuid4().hex
        bind_contextvars(request_id=request_id)
        try:
            # Stop button on the existing progress message; the run lifecycle
            # (interrupt event, lock, ActiveRequest, heartbeat) is owned by the
            # _claude_run context manager (same mechanism as agentic_text).
            stop_kb = self._build_stop_kb(user_id)
            try:
                await progress_msg.edit_reply_markup(reply_markup=stop_kb)
            except Exception:
                pass

            mcp_images_media: List[ImageAttachment] = []
            formatted_messages: List["FormattedMessage"] = []

            async with self._claude_run(
                user_id=user_id, chat=chat, progress_msg=progress_msg
            ) as interrupt_event:
                # Same single implementation as the text path: reservation,
                # is_error handling, cost settlement, persistence and formatting.
                formatted_messages, _success = await self._run_and_format(
                    update=update,
                    context=context,
                    prompt=prompt,
                    user_id=user_id,
                    progress_msg=progress_msg,
                    request_id=request_id,
                    interrupt_event=interrupt_event,
                    mcp_images=mcp_images_media,
                    stop_kb=stop_kb,
                    images=images,
                )
                if not formatted_messages:
                    # Outcome already reported in place on the progress message.
                    return

            # Always clear the "Working..." message (with its Stop button)
            # before replying — a stale progress message next to an error
            # response is worse than no progress message at all.
            try:
                await progress_msg.delete()
            except Exception:
                logger.debug("Failed to delete progress message, ignoring")

            # Use MCP-collected images (from send_image_to_user tool calls).
            await self._deliver_response(update, formatted_messages, mcp_images_media)
        finally:
            unbind_contextvars("request_id")

    async def _handle_stop_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle stop: callbacks — interrupt a running Claude request."""
        query = update.callback_query
        # callback_data is client-supplied and the ``^stop:`` pattern accepts any
        # payload, so "stop:abc" would raise before the query is answered and
        # leave the user with a spinning button.
        try:
            target_user_id = int(query.data.split(":", 1)[1])
        except (IndexError, ValueError):
            await query.answer("Invalid stop request.", show_alert=False)
            return

        # Only the requesting user can stop their own request
        if query.from_user.id != target_user_id:
            await query.answer(
                "Only the requesting user can stop this.", show_alert=True
            )
            return

        active = self._active_requests.get(target_user_id)
        if not active:
            await query.answer("Already completed.", show_alert=False)
            return
        if active.interrupted:
            await query.answer("Already stopping...", show_alert=False)
            return

        active.interrupt_event.set()
        active.interrupted = True
        await query.answer("Stopping...", show_alert=False)

        try:
            await active.progress_msg.edit_text("Stopping...", reply_markup=None)
        except Exception:
            pass

    async def _handle_unknown_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Forward unknown slash commands to Claude in agentic mode.

        Known commands are handled by their own CommandHandlers (group 0);
        this handler fires for *every* COMMAND message in group 10 but returns
        immediately when the command is registered, preventing double execution.
        """
        msg = update.effective_message
        if not msg or not msg.text:
            return
        cmd = msg.text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd in self._known_commands:
            return  # let the registered CommandHandler take care of it
        # Forward unrecognised /commands to Claude as natural language
        await self.agentic_text(update, context)

    def _voice_unavailable_message(self) -> str:
        """Return provider-aware guidance when voice feature is unavailable."""
        return (
            "Voice processing is not available. "
            f"Set {self.settings.voice_provider_api_key_env} "
            f"for {self.settings.voice_provider_display_name} and install "
            'voice extras with: pip install "claude-code-telegram[voice]"'
        )

    def _navigation_root(self, context: ContextTypes.DEFAULT_TYPE) -> Path:
        """Root that /repo and ``cd:`` navigation is confined to.

        In project-thread mode each topic is pinned to its project root, and
        ``_persist_thread_state`` clamps any wider move back to it — so confine
        navigation to that root instead of the global approved directory. Using
        the approved root there would accept a switch that is then silently
        reverted, leaving the user with a misleading "Switched to X".
        """
        thread_context = context.user_data.get("_thread_context")
        if thread_context:
            return Path(thread_context["project_root"])
        return self.settings.approved_directory

    def _resolve_within_approved(
        self, name: str, base: Optional[Path] = None
    ) -> Optional[Path]:
        """Resolve *name* under *base* (the approved root by default), or
        ``None`` if it escapes it.

        Guards /repo and ``cd:`` navigation against ``..`` traversal, absolute
        paths (on Windows ``base / "C:/x"`` discards ``base`` entirely) and
        symlinks that resolve outside the root — any of which would otherwise
        move the working directory past the boundary. Returns the resolved path
        only when it is an existing directory inside the root.
        """
        base = base or self.settings.approved_directory
        candidate = (base / name).resolve()
        try:
            candidate.relative_to(base.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_dir() else None

    async def agentic_repo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List repos in workspace or switch to one.

        /repo          — list subdirectories with git indicators
        /repo <name>   — switch to that directory, resume session if available
        """
        args = update.message.text.split()[1:] if update.message.text else []
        base = self._navigation_root(context)
        current_dir = context.user_data.get("current_directory", base)

        if args:
            # Switch to named repo
            target_name = args[0]
            target_path = self._resolve_within_approved(target_name, base)
            if target_path is None:
                await update.message.reply_text(
                    f"Directory not found: <code>{escape_html(target_name)}</code>",
                    parse_mode="HTML",
                )
                return

            context.user_data["current_directory"] = target_path

            # Try to find a resumable session
            claude_integration = context.bot_data.get("claude_integration")
            session_id = None
            if claude_integration:
                existing = await claude_integration.find_resumable_session(
                    update.effective_user.id, target_path
                )
                if existing:
                    session_id = existing.session_id
            context.user_data["claude_session_id"] = session_id

            is_git = (target_path / ".git").is_dir()
            git_badge = " (git)" if is_git else ""
            session_badge = " · session resumed" if session_id else ""

            await update.message.reply_text(
                f"Switched to <code>{escape_html(target_name)}/</code>"
                f"{git_badge}{session_badge}",
                parse_mode="HTML",
            )

            audit_logger = context.bot_data.get("audit_logger")
            if audit_logger:
                await audit_logger.log_command(
                    user_id=update.effective_user.id,
                    command="repo",
                    args=[target_name],
                    success=True,
                )
            return

        # No args — list repos. The scan (iterdir + one stat per entry for the
        # git badge) runs off the event loop like the equivalent listings in
        # callback.py and file_handler.py: on a slow or networked filesystem it
        # would otherwise stall update processing for every user.
        def _scan_repos() -> List[Tuple[str, bool]]:
            return [
                (d.name, (d / ".git").is_dir())
                for d in sorted(base.iterdir(), key=lambda d: d.name)
                if d.is_dir() and not d.name.startswith(".")
            ]

        try:
            entries = await asyncio.to_thread(_scan_repos)
        except OSError as e:
            await update.message.reply_text(f"Error reading workspace: {e}")
            return

        if not entries:
            await update.message.reply_text(
                f"No repos in <code>{escape_html(str(base))}</code>.\n"
                'Clone one by telling me, e.g. <i>"clone org/repo"</i>.',
                parse_mode="HTML",
            )
            return

        lines: List[str] = []
        keyboard_rows: List[list] = []  # type: ignore[type-arg]
        current_name = current_dir.name if current_dir != base else None

        for name, is_git in entries:
            icon = "\U0001f4e6" if is_git else "\U0001f4c1"
            marker = " \u25c0" if name == current_name else ""
            lines.append(f"{icon} <code>{escape_html(name)}/</code>{marker}")

        # Build inline keyboard (2 per row). Telegram caps callback_data at
        # 64 UTF-8 bytes; a name that overflows would break the whole reply,
        # so omit its button (the name still shows in the text list above).
        for i in range(0, len(entries), 2):
            row = []
            for j in range(2):
                if i + j < len(entries):
                    name = entries[i + j][0]
                    if len(f"cd:{name}".encode("utf-8")) > 64:
                        continue
                    row.append(InlineKeyboardButton(name, callback_data=f"cd:{name}"))
            if row:
                keyboard_rows.append(row)

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        await update.message.reply_text(
            "<b>Repos</b>\n\n" + "\n".join(lines),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def cmd_schedule(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Manage scheduled jobs (admin only).

        /schedule list                       — list active jobs
        /schedule add <cron(5 fields)> <prompt...>  — add a cron job
        /schedule remove <job_id>            — remove a job
        """
        user_id = update.effective_user.id

        # Admin gate — scheduling is a privileged automation lever.
        if not self.settings.is_admin(user_id):
            await update.message.reply_text(
                "🔒 <b>Admin only</b>\n\nThis command is restricted to "
                "administrators.",
                parse_mode="HTML",
            )
            audit_logger = context.bot_data.get("audit_logger")
            if audit_logger:
                await audit_logger.log_security_violation(
                    user_id=user_id,
                    violation_type="unauthorized_admin_command",
                    details="/schedule denied (not an admin)",
                    severity="medium",
                )
            logger.warning("Unauthorized /schedule attempt", user_id=user_id)
            return

        scheduler = context.bot_data.get("scheduler")
        if scheduler is None:
            await update.message.reply_text(
                "Scheduler is disabled (set <code>ENABLE_SCHEDULER=true</code>).",
                parse_mode="HTML",
            )
            return

        usage = (
            "<b>Usage</b>\n"
            "<code>/schedule list</code>\n"
            "<code>/schedule add &lt;cron 5 fields&gt; &lt;prompt&gt;</code>\n"
            "<code>/schedule remove &lt;job_id&gt;</code>"
        )

        args = update.message.text.split()[1:] if update.message.text else []
        sub = args[0].lower() if args else ""

        if sub == "list":
            jobs = await scheduler.list_jobs()
            if not jobs:
                await update.message.reply_text("No scheduled jobs.")
            else:
                lines: List[str] = []
                for job in jobs:
                    job_id = job.get("job_id", "?")
                    job_name = job.get("job_name", "?")
                    cron_expr = job.get("cron_expression", "?")
                    next_run = self._scheduler_next_run(scheduler, job_id)
                    next_str = f" · next {next_run}" if next_run else ""
                    lines.append(
                        f"<code>{escape_html(str(job_id))}</code> · "
                        f"<b>{escape_html(str(job_name))}</b> · "
                        f"<code>{escape_html(str(cron_expr))}</code>{next_str}"
                    )
                await update.message.reply_text(
                    "<b>Scheduled jobs</b>\n\n" + "\n".join(lines),
                    parse_mode="HTML",
                )

        elif sub == "add":
            # /schedule add <m> <h> <dom> <mon> <dow> <prompt...>
            if len(args) < 7:
                await update.message.reply_text(usage, parse_mode="HTML")
                return
            cron_expression = " ".join(args[1:6])
            prompt = " ".join(args[6:]).strip()
            if not prompt:
                await update.message.reply_text(usage, parse_mode="HTML")
                return
            job_name = prompt.split()[0][:40] or "job"
            try:
                job_id = await scheduler.add_job(
                    job_name=job_name,
                    cron_expression=cron_expression,
                    prompt=prompt,
                    target_chat_ids=[update.effective_chat.id],
                    created_by=user_id,
                )
            except Exception as e:
                await update.message.reply_text(
                    f"Failed to add job: {escape_html(str(e))}",
                    parse_mode="HTML",
                )
                return
            await update.message.reply_text(
                f"Added job <code>{escape_html(str(job_id))}</code> "
                f"(<code>{escape_html(cron_expression)}</code>).",
                parse_mode="HTML",
            )

        elif sub == "remove":
            if len(args) < 2:
                await update.message.reply_text(usage, parse_mode="HTML")
                return
            job_id = args[1]
            # remove_job reports whether an active job actually existed, so a
            # typo'd id no longer gets a confident "Removed".
            if await scheduler.remove_job(job_id):
                await update.message.reply_text(
                    f"Removed job <code>{escape_html(str(job_id))}</code>.",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text(
                    f"No active job <code>{escape_html(str(job_id))}</code>.",
                    parse_mode="HTML",
                )

        else:
            await update.message.reply_text(usage, parse_mode="HTML")

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="schedule",
                args=[sub or "(none)"],
                success=True,
            )

    @staticmethod
    def _scheduler_next_run(scheduler: Any, job_id: str) -> Optional[str]:
        """Return the next run time of a live APScheduler job, if available."""
        try:
            job = scheduler._scheduler.get_job(job_id)
        except Exception:
            return None
        next_run_time = getattr(job, "next_run_time", None) if job else None
        if next_run_time is None:
            return None
        return next_run_time.strftime("%Y-%m-%d %H:%M %Z").strip()

    async def cmd_events(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show recent failed / retrying / dead-letter webhook deliveries (admin)."""
        user_id = update.effective_user.id

        if not self.settings.is_admin(user_id):
            await update.message.reply_text(
                "🔒 <b>Admin only</b>\n\nThis command is restricted to "
                "administrators.",
                parse_mode="HTML",
            )
            audit_logger = context.bot_data.get("audit_logger")
            if audit_logger:
                await audit_logger.log_security_violation(
                    user_id=user_id,
                    violation_type="unauthorized_admin_command",
                    details="/events denied (not an admin)",
                    severity="medium",
                )
            logger.warning("Unauthorized /events attempt", user_id=user_id)
            return

        storage = context.bot_data.get("storage")
        if storage is None:
            await update.message.reply_text("Storage is unavailable.")
            return

        try:
            async with storage.db_manager.get_connection() as conn:
                cursor = await conn.execute(
                    "SELECT provider, event_type, delivery_id, processed, "
                    "attempts, last_error FROM webhook_events "
                    "WHERE attempts > 0 OR processed = 2 "
                    "ORDER BY COALESCE(last_attempt_at, received_at) DESC LIMIT 15"
                )
                rows = [dict(r) for r in await cursor.fetchall()]
        except Exception:
            logger.exception("Failed to load webhook events for /events")
            await update.message.reply_text("Failed to load webhook events.")
            return

        if not rows:
            await update.message.reply_text("No failed or retrying webhook events.")
            return

        state_icon = {0: "⏳ retry", 1: "✅ recovered", 2: "💀 dead"}
        lines: List[str] = []
        for r in rows:
            icon = state_icon.get(int(r.get("processed") or 0), "?")
            delivery = str(r.get("delivery_id") or "?")[:16]
            etype = str(r.get("event_type") or "?")
            attempts = r.get("attempts") or 0
            err = str(r.get("last_error") or "")
            if len(err) > 120:
                err = err[:120] + "…"
            err_line = f"\n   {escape_html(err)}" if err else ""
            lines.append(
                f"{icon} · <b>{escape_html(etype)}</b> · "
                f"<code>{escape_html(delivery)}</code> · {attempts} att.{err_line}"
            )

        await update.message.reply_text(
            "<b>Webhook events</b> (failed / retrying / dead-letter)\n\n"
            + "\n".join(lines),
            parse_mode="HTML",
        )

        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id, command="events", args=[], success=True
            )

    async def _agentic_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cd: callbacks — switch directory and resume session if available."""
        query = update.callback_query
        await query.answer()

        data = query.data
        _, project_name = data.split(":", 1)

        new_path = self._resolve_within_approved(
            project_name, self._navigation_root(context)
        )

        if new_path is None:
            await query.edit_message_text(
                f"Directory not found: <code>{escape_html(project_name)}</code>",
                parse_mode="HTML",
            )
            return

        context.user_data["current_directory"] = new_path

        # Look for a resumable session instead of always clearing
        claude_integration = context.bot_data.get("claude_integration")
        session_id = None
        if claude_integration:
            existing = await claude_integration.find_resumable_session(
                query.from_user.id, new_path
            )
            if existing:
                session_id = existing.session_id
        context.user_data["claude_session_id"] = session_id

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""
        session_badge = " · session resumed" if session_id else ""

        await query.edit_message_text(
            f"Switched to <code>{escape_html(project_name)}/</code>"
            f"{git_badge}{session_badge}",
            parse_mode="HTML",
        )

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="cd",
                args=[project_name],
                success=True,
            )

    async def agentic_sessions(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List resumable sessions with resume buttons.

        A regular user only sees sessions the bot recorded against *their*
        Telegram ID. Sessions discovered on disk under ``~/.claude/projects``
        (CLI, VS Code, another operator's runs) carry no Telegram owner, so they
        are listed for admins only.
        """
        from ..claude.local_sessions import (
            _is_within,
            list_all_local_sessions,
            load_session_previews,
        )

        user_id = update.effective_user.id
        current_session_id = context.user_data.get("claude_session_id")

        # Scope to the root the caller is currently pinned to — the project
        # topic's root in thread mode, otherwise the approved directory. Every
        # row here is one tap away from becoming current_directory, so listing
        # anything outside that root would let a tap cross the boundary.
        root = self._navigation_root(context)
        limit = 15

        entries: List[_SessionEntry] = []
        by_id: Dict[str, _SessionEntry] = {}

        storage = context.bot_data.get("storage")
        if storage is not None:
            own = await storage.sessions.get_user_sessions(user_id, active_only=False)
            for sess in own:
                if len(entries) >= limit:
                    break
                if not sess.project_path or not _is_within(
                    Path(sess.project_path), root
                ):
                    continue
                when = sess.last_used
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                entry = _SessionEntry(
                    session_id=sess.session_id,
                    cwd=sess.project_path,
                    when=when,
                )
                by_id[sess.session_id] = entry
                entries.append(entry)

        if self.settings.is_admin(user_id):
            # Synchronous JSONL filesystem scan — offload to a thread so it does
            # not block the event loop.
            local_sessions = await asyncio.to_thread(
                list_all_local_sessions,
                limit=limit,
                within=root,
            )
            for local in local_sessions:
                known = by_id.get(local.session_id)
                if known is not None:
                    # Same session from both sources: keep the owned entry but
                    # borrow the JSONL previews, which storage does not record.
                    if not known.preview:
                        known.preview = local.first_message
                    if not known.last_preview:
                        known.last_preview = local.last_message
                    continue
                entries.append(
                    _SessionEntry(
                        session_id=local.session_id,
                        cwd=local.cwd,
                        when=datetime.fromtimestamp(local.mtime, tz=UTC),
                        preview=local.first_message,
                        last_preview=local.last_message,
                        is_local=True,
                    )
                )

        entries.sort(key=lambda e: e.when, reverse=True)
        entries = entries[:limit]

        if not entries:
            await update.message.reply_text("No sessions found.")
            return

        # Rows that came from storage carry no preview at all — an 8-hex id and
        # a path do not tell two sessions in the same project apart. The JSONL
        # for a known session is at a computable path, so filling them in is a
        # couple of bounded reads per row rather than a directory walk.
        needs_preview = [e for e in entries if not e.preview and not e.last_preview]
        if needs_preview:
            previews = await asyncio.to_thread(
                lambda rows=needs_preview: [
                    load_session_previews(Path(e.cwd), e.session_id) for e in rows
                ]
            )
            for entry, (first_msg, last_msg) in zip(needs_preview, previews):
                entry.preview = first_msg
                entry.last_preview = last_msg

        # Which session the *next* message in the current directory would
        # actually resume. Not necessarily the id in user_data: after a timeout
        # reset, or for a directory last used from another client, the resolver
        # can land on a different session (or none).
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        resumable_id: Optional[str] = None
        claude_integration = context.bot_data.get("claude_integration")
        if claude_integration is not None:
            try:
                resumable = await claude_integration.find_resumable_session(
                    user_id, Path(current_dir)
                )
                resumable_id = resumable.session_id if resumable else None
            except Exception:
                logger.debug("Resumable-session lookup failed for /sessions")

        lines: list[str] = []
        keyboard_rows: list[list[InlineKeyboardButton]] = []

        for i, sess in enumerate(entries, 1):
            # Show relative path from the scoping root if possible
            try:
                display_path = Path(sess.cwd).relative_to(root)
            except ValueError:
                display_path = Path(sess.cwd).name or sess.cwd

            short_id = sess.session_id[:8]
            age = datetime.now(UTC) - sess.when
            if age.days > 0:
                age_str = f"{age.days}d ago"
            elif age.seconds >= 3600:
                age_str = f"{age.seconds // 3600}h ago"
            else:
                age_str = f"{age.seconds // 60}m ago"

            # "active" is the session the next message in the current
            # directory actually resumes. The plain marker means "stored as
            # current" without being the one the resolver picked.
            if resumable_id and sess.session_id == resumable_id:
                marker = " ● active"
            elif sess.session_id == current_session_id:
                marker = " ◀"
            else:
                marker = ""
            origin = " · local" if sess.is_local else ""
            preview = ""
            if sess.preview:
                first_line = escape_html(_truncate(sess.preview, 40))
                preview += _PREVIEW_INDENT + f"<i>{first_line}</i>"
            if sess.last_preview and sess.last_preview != sess.preview:
                # The latest prompt is what identifies a long-running session;
                # its opening line usually does not.
                last_line = escape_html(_truncate(sess.last_preview, 40))
                preview += _PREVIEW_INDENT + f"↳ <i>{last_line}</i>"
            lines.append(
                f"{i}. <code>{escape_html(str(display_path))}/</code>"
                f" · <code>{short_id}</code> · {age_str}{origin}{marker}{preview}"
            )

            # callback_data max 64 bytes — uuid is 36 chars, prefix 7 = 43
            # Button text: show first words of the conversation
            btn_label = f"{short_id}"
            if sess.preview:
                btn_msg = sess.preview[:30]
                if len(sess.preview) > 30:
                    btn_msg += "…"
                btn_label = f"{short_id} {btn_msg}"
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        btn_label,
                        callback_data=f"resume:{sess.session_id}",
                    )
                ]
            )

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        await update.message.reply_text(
            "<b>Sessions</b>\n\n" + "\n".join(lines),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def _agentic_resume_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle resume: callbacks — switch to a session from /sessions list.

        callback_data is attacker-controlled (any allowed user can craft a
        ``resume:<uuid>``), so every check here re-derives authority from
        ``query.from_user.id`` server-side rather than trusting the payload.
        """
        query = update.callback_query
        await query.answer()

        _, session_id = query.data.split(":", 1)
        user_id = query.from_user.id
        root = self._navigation_root(context)

        from ..claude.local_sessions import (
            _claude_projects_dir,
            _encode_path,
            _is_within,
            _parse_session_head,
        )

        cwd: Optional[str] = None

        # 1. Sessions the bot recorded: resumable only by the user who owns them.
        storage = context.bot_data.get("storage")
        if storage is not None:
            record = await storage.sessions.get_session(session_id)
            if record is not None:
                if record.user_id != user_id:
                    logger.warning(
                        "Rejected cross-user session resume",
                        user_id=user_id,
                        owner_id=record.user_id,
                        session_id=session_id,
                    )
                    await query.edit_message_text(
                        "❌ That session belongs to another user "
                        "and cannot be resumed."
                    )
                    return
                cwd = record.project_path

        # 2. Sessions found on disk have no Telegram owner (CLI, VS Code,
        #    another operator), so only admins may reach into them.
        if cwd is None:
            if not self.settings.is_admin(user_id):
                logger.warning(
                    "Rejected local session resume for non-admin",
                    user_id=user_id,
                    session_id=session_id,
                )
                await query.edit_message_text(
                    "❌ That session is not available to you."
                )
                return

            # Encoded prefix for the scoping root — Claude encodes a cwd by
            # replacing non-alphanumerics with "-", so any project dir inside
            # the root has an encoded name starting with this prefix.
            root_prefix = _encode_path(root.resolve())

            def _find_session_cwd() -> Optional[str]:
                projects_dir = _claude_projects_dir()
                if not projects_dir.is_dir():
                    return None
                for project_dir in projects_dir.iterdir():
                    if not project_dir.is_dir():
                        continue
                    # Scope to project dirs encoded under the scoping root.
                    if not project_dir.name.startswith(root_prefix):
                        continue
                    jsonl = project_dir / f"{session_id}.jsonl"
                    if jsonl.is_file():
                        first = _parse_session_head(jsonl)
                        if not first:
                            return None
                        return first.get("cwd")
                return None

            # Synchronous directory iteration + JSONL parse — offload to a
            # thread so it does not block the event loop.
            cwd = await asyncio.to_thread(_find_session_cwd)

        # 3. Fail closed: the cwd must resolve inside the root the caller is
        #    currently pinned to (the project topic's root in thread mode,
        #    otherwise the approved directory). Without this a user could jump
        #    a topic to another project's session and bind the thread state to
        #    a session ID that does not belong to it.
        if not cwd or not _is_within(Path(cwd), root):
            await query.edit_message_text(
                "❌ That session's working directory could not be confirmed "
                "inside the current project root and cannot be resumed.",
            )
            return

        context.user_data["current_directory"] = Path(cwd)

        context.user_data["claude_session_id"] = session_id
        context.user_data.pop("force_new_session", None)

        short_id = session_id[:8]
        dir_display = f" in <code>{escape_html(cwd)}</code>"

        await query.edit_message_text(
            f"Resumed session <code>{short_id}…</code>{dir_display}",
            parse_mode="HTML",
        )

        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="resume",
                args=[short_id],
                success=True,
            )
