"""Message orchestrator — single entry point for all Telegram updates.

Routes messages based on agentic vs classic mode. In agentic mode, provides
a minimal conversational interface (3 commands, no inline keyboards). In
classic mode, delegates to existing full-featured handlers.
"""

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
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

from ..claude.sdk_integration import StreamUpdate
from ..config.settings import Settings
from ..projects import PrivateTopicsUnavailableError
from ..security.secret_patterns import redact_secrets
from .utils.draft_streamer import DraftStreamer, generate_draft_id
from .utils.html_format import escape_html
from .utils.image_extractor import (
    ImageAttachment,
    should_send_as_animation,
    should_send_as_photo,
    validate_image_path,
)

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
    "AskUserQuestion": "\u2753",
    "EnterPlanMode": "\U0001f4cb",
    "ExitPlanMode": "\U0001f4cb",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


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

        project_root = project.absolute_path
        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        context.user_data["current_directory"] = current_dir
        context.user_data["claude_session_id"] = state.get("claude_session_id")
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

        elapsed = time.time() - start_time
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
            return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
        if tool_name == "Task":
            desc = tool_input.get("description", "")
            if desc:
                return desc[:60]
        # Generic: show first key's value
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v[:60]
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
                        if img:
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
                    await draft_streamer.append_text(update_obj.content)

            # Throttle progress message edits to avoid Telegram rate limits
            if not draft_streamer and verbose_level >= 1:
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
                with open(img.path, "rb") as f:
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
                    if per_image_caption:
                        photo_caption: Optional[str] = per_image_caption
                        photo_parse_mode: Optional[str] = None
                    elif use_caption:
                        photo_caption = caption
                        photo_parse_mode = caption_parse_mode
                    else:
                        photo_caption = None
                        photo_parse_mode = None
                    with open(single.path, "rb") as f:
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
                            fh = stack.enter_context(open(img.path, "rb"))
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
            except Exception as e:
                logger.warning("Failed to send photo album", error=str(e))

        # Send SVGs / large files as documents (one by one — can't mix in album)
        for img in documents:
            try:
                with open(img.path, "rb") as f:
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

    async def agentic_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        user_id = update.effective_user.id
        message_text = update.message.text

        logger.info(
            "Agentic text message",
            user_id=user_id,
            message_length=len(message_text),
        )

        # Rate limiting was already enforced by rate_limit_middleware (group -1);
        # re-checking here would double-charge the token bucket. We only need the
        # rate_limiter reference later to record the run's actual cost.
        rate_limiter = context.bot_data.get("rate_limiter")

        chat = update.message.chat
        await chat.send_action("typing")

        verbose_level = self._get_verbose_level(context)

        # Stop button + interrupt event so the user can cancel a running request
        interrupt_event = asyncio.Event()
        stop_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Stop", callback_data=f"stop:{user_id}")]]
        )
        progress_msg = await update.message.reply_text(
            "Working...", reply_markup=stop_kb
        )

        # Register active request so the stop callback can find it
        self._active_requests[user_id] = ActiveRequest(
            user_id=user_id,
            interrupt_event=interrupt_event,
            progress_msg=progress_msg,
        )

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            self._active_requests.pop(user_id, None)
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration.",
                reply_markup=None,
            )
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        # --- Verbose progress tracking via stream callback ---
        tool_log: List[Dict[str, Any]] = []
        start_time = time.time()
        mcp_images: List[ImageAttachment] = []

        # Stream drafts (private chats only)
        draft_streamer: Optional[DraftStreamer] = None
        if self.settings.enable_stream_drafts and chat.type == "private":
            draft_streamer = DraftStreamer(
                bot=context.bot,
                chat_id=chat.id,
                draft_id=generate_draft_id(),
                message_thread_id=update.message.message_thread_id,
                throttle_interval=self.settings.stream_draft_interval,
            )

        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            start_time,
            mcp_images=mcp_images,
            approved_directory=self.settings.approved_directory,
            draft_streamer=draft_streamer,
            reply_markup=stop_kb,
            interrupt_event=interrupt_event,
        )

        # Independent typing heartbeat — stays alive even with no stream events
        heartbeat = self._start_typing_heartbeat(chat)

        success = True
        try:
            claude_response = await claude_integration.run_command(
                prompt=message_text,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                interrupt_event=interrupt_event,
            )

            # New session created successfully — clear the one-shot flag
            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["claude_session_id"] = claude_response.session_id

            # The run completed without raising but the SDK flagged an error
            # (e.g. no ResultMessage / budget cap). Surface it explicitly and
            # don't charge cost for a run that produced no usable result.
            if claude_response.is_error:
                success = False
                from .handlers.message import _format_error_message
                from .utils.formatting import FormattedMessage

                formatted_messages = [
                    FormattedMessage(
                        _format_error_message(
                            claude_response.error_type or "Claude returned an error."
                        ),
                        parse_mode="HTML",
                    )
                ]
            else:
                # Charge the real cost so claude_max_cost_per_user is enforced.
                if rate_limiter:
                    await rate_limiter.record_actual_cost(user_id, claude_response.cost)

                # Track directory changes
                from .handlers.message import (
                    _update_working_directory_from_claude_response,
                )

                _update_working_directory_from_claude_response(
                    claude_response, context, self.settings, user_id
                )

                # Store interaction
                storage = context.bot_data.get("storage")
                if storage:
                    try:
                        await storage.save_claude_interaction(
                            user_id=user_id,
                            session_id=claude_response.session_id,
                            prompt=message_text,
                            response=claude_response,
                            ip_address=None,
                        )
                    except Exception as e:
                        logger.warning("Failed to log interaction", error=str(e))

                # Format response (no reply_markup — strip keyboards)
                from .utils.formatting import ResponseFormatter

                formatter = ResponseFormatter(self.settings)

                # Redact secrets from the response body before it leaves the
                # bot — tool OUTPUT (e.g. a printenv dump) can land here.
                response_content = redact_secrets(claude_response.content or "")
                if claude_response.interrupted:
                    response_content = response_content + "\n\n_(Interrupted by user)_"

                formatted_messages = formatter.format_claude_response(response_content)

        except Exception as e:
            success = False
            logger.error("Claude integration failed", error=str(e), user_id=user_id)
            from .handlers.message import _format_error_message
            from .utils.formatting import FormattedMessage

            formatted_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]
        finally:
            heartbeat.cancel()
            self._active_requests.pop(user_id, None)
            if draft_streamer:
                try:
                    await draft_streamer.flush()
                except Exception:
                    logger.debug("Draft flush failed in finally block", user_id=user_id)

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls)
        images: List[ImageAttachment] = mcp_images

        # Try to combine text + images in one message when possible
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

        # Send text messages (skip if caption was already embedded in photos)
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
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)
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

            # Send images separately if caption wasn't used
            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=success,
            )

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
            valid, error = security_validator.validate_filename(document.file_name)
            if not valid:
                await update.message.reply_text(f"File rejected: {error}")
                return

        # Size check (file_size is Optional — Telegram may omit it)
        max_size = 10 * 1024 * 1024
        file_size = document.file_size or 0
        if file_size > max_size:
            await update.message.reply_text(
                f"File too large ({file_size / 1024 / 1024:.1f}MB). Max: 10MB."
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
            except Exception:
                file_handler = None

        if not file_handler:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()
            try:
                content = file_bytes.decode("utf-8")
                if len(content) > 50000:
                    content = content[:50000] + "\n... (truncated)"
                caption = update.message.caption or "Please review this file:"
                prompt = (
                    f"{caption}\n\n**File:** `{document.file_name}`\n\n"
                    f"```\n{content}\n```"
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
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)

        # Stop button + interrupt event so long media-derived runs can be
        # cancelled too (same mechanism as agentic_text).
        interrupt_event = asyncio.Event()
        stop_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Stop", callback_data=f"stop:{user_id}")]]
        )
        try:
            await progress_msg.edit_reply_markup(reply_markup=stop_kb)
        except Exception:
            pass
        self._active_requests[user_id] = ActiveRequest(
            user_id=user_id,
            interrupt_event=interrupt_event,
            progress_msg=progress_msg,
        )

        tool_log: List[Dict[str, Any]] = []
        mcp_images_media: List[ImageAttachment] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_media,
            approved_directory=self.settings.approved_directory,
            reply_markup=stop_kb,
            interrupt_event=interrupt_event,
        )

        heartbeat = self._start_typing_heartbeat(chat)
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
        except Exception:
            # Clear the dead "Working..." progress message (with its now-useless
            # Stop button) before the caller reports the error — otherwise it
            # lingers forever in the chat.
            try:
                await progress_msg.delete()
            except Exception:
                logger.debug("Failed to delete progress message, ignoring")
            raise
        finally:
            heartbeat.cancel()
            self._active_requests.pop(user_id, None)

        if force_new:
            context.user_data["force_new_session"] = False

        context.user_data["claude_session_id"] = claude_response.session_id

        from .handlers.message import _update_working_directory_from_claude_response
        from .utils.formatting import ResponseFormatter

        # The run completed without raising but the SDK flagged an error
        # (e.g. no ResultMessage / budget cap). Surface it and skip the cost
        # charge for a 'no_result_message' run that produced nothing usable.
        if claude_response.is_error:
            from .handlers.message import _format_error_message

            try:
                await progress_msg.delete()
            except Exception:
                logger.debug("Failed to delete progress message, ignoring")
            await update.effective_message.reply_text(
                _format_error_message(
                    claude_response.error_type or "Claude returned an error."
                ),
                parse_mode="HTML",
            )
            return

        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            await rate_limiter.record_actual_cost(user_id, claude_response.cost)

        _update_working_directory_from_claude_response(
            claude_response, context, self.settings, user_id
        )

        formatter = ResponseFormatter(self.settings)
        # Redact secrets from the response body before it leaves the bot —
        # tool OUTPUT (e.g. a printenv dump) can land here.
        response_content = redact_secrets(claude_response.content or "")
        if claude_response.interrupted:
            response_content = response_content + "\n\n_(Interrupted by user)_"
        formatted_messages = formatter.format_claude_response(response_content)

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls).
        images: List[ImageAttachment] = mcp_images_media

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
                        reply_markup=None,
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
                    await update.message.reply_text(
                        message.text,
                        reply_markup=None,
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

    async def _handle_stop_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle stop: callbacks — interrupt a running Claude request."""
        query = update.callback_query
        target_user_id = int(query.data.split(":", 1)[1])

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
                existing = await claude_integration._find_resumable_session(
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
            return

        # No args — list repos
        try:
            entries = sorted(
                [
                    d
                    for d in base.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ],
                key=lambda d: d.name,
            )
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

        for d in entries:
            is_git = (d / ".git").is_dir()
            icon = "\U0001f4e6" if is_git else "\U0001f4c1"
            marker = " \u25c0" if d.name == current_name else ""
            lines.append(f"{icon} <code>{escape_html(d.name)}/</code>{marker}")

        # Build inline keyboard (2 per row)
        for i in range(0, len(entries), 2):
            row = []
            for j in range(2):
                if i + j < len(entries):
                    name = entries[i + j].name
                    row.append(InlineKeyboardButton(name, callback_data=f"cd:{name}"))
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
            await scheduler.remove_job(job_id)
            await update.message.reply_text(
                f"Removed job <code>{escape_html(str(job_id))}</code>.",
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
            existing = await claude_integration._find_resumable_session(
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
        """List recent Claude Code sessions (local + bot) with resume buttons."""
        from ..claude.local_sessions import list_all_local_sessions

        current_session_id = context.user_data.get("claude_session_id")

        # Scope to the approved directory: never list sessions whose working
        # directory lives outside it, or resuming one would move the bot's
        # current_directory past the approved root.
        # Synchronous JSONL filesystem scan — offload to a thread so it does not
        # block the event loop.
        local_sessions = await asyncio.to_thread(
            list_all_local_sessions,
            limit=15,
            within=self.settings.approved_directory,
        )

        if not local_sessions:
            await update.message.reply_text("No sessions found.")
            return

        lines: list[str] = []
        keyboard_rows: list[list[InlineKeyboardButton]] = []

        for i, sess in enumerate(local_sessions, 1):
            # Show relative path from approved_directory if possible
            try:
                display_path = Path(sess.cwd).relative_to(
                    self.settings.approved_directory
                )
            except ValueError:
                display_path = Path(sess.cwd).name or sess.cwd

            short_id = sess.session_id[:8]
            mtime = datetime.fromtimestamp(sess.mtime, tz=UTC)
            age = datetime.now(UTC) - mtime
            if age.days > 0:
                age_str = f"{age.days}d ago"
            elif age.seconds >= 3600:
                age_str = f"{age.seconds // 3600}h ago"
            else:
                age_str = f"{age.seconds // 60}m ago"

            marker = " \u25c0" if sess.session_id == current_session_id else ""
            preview = ""
            if sess.first_message:
                # Truncate to ~40 chars for display
                msg_preview = sess.first_message[:40]
                if len(sess.first_message) > 40:
                    msg_preview += "…"
                preview = f"\n   <i>{escape_html(msg_preview)}</i>"
            lines.append(
                f"{i}. <code>{escape_html(str(display_path))}/</code>"
                f" · <code>{short_id}</code> · {age_str}{marker}{preview}"
            )

            # callback_data max 64 bytes — uuid is 36 chars, prefix 7 = 43
            # Button text: show first words of the conversation
            btn_label = f"{short_id}"
            if sess.first_message:
                btn_msg = sess.first_message[:30]
                if len(sess.first_message) > 30:
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
        """Handle resume: callbacks — switch to a session from /sessions list."""
        query = update.callback_query
        await query.answer()

        _, session_id = query.data.split(":", 1)

        # Find the session's working directory from local storage
        from ..claude.local_sessions import (
            _claude_projects_dir,
            _encode_path,
            _is_within,
            _parse_session_head,
        )

        approved = self.settings.approved_directory
        # Encoded prefix for the approved root — Claude encodes a cwd by
        # replacing non-alphanumerics with "-", so any project dir inside the
        # approved root has an encoded name starting with this prefix.
        approved_prefix = _encode_path(approved.resolve())

        def _find_session_cwd() -> Optional[str]:
            projects_dir = _claude_projects_dir()
            if not projects_dir.is_dir():
                return None
            for project_dir in projects_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                # Scope to project dirs encoded under the approved root.
                if not project_dir.name.startswith(approved_prefix):
                    continue
                jsonl = project_dir / f"{session_id}.jsonl"
                if jsonl.is_file():
                    first = _parse_session_head(jsonl)
                    if not first:
                        return None
                    cwd = first.get("cwd")
                    # Confirm the recorded cwd really resolves inside approved.
                    if cwd and _is_within(Path(cwd), approved):
                        return cwd
                    return None
            return None

        # Synchronous directory iteration + JSONL parse — offload to a thread so
        # it does not block the event loop.
        cwd = await asyncio.to_thread(_find_session_cwd)

        # Fail closed: if we cannot confirm a cwd within the approved root
        # (missing/unscoped session, or first JSONL line had no cwd), refuse to
        # resume rather than silently moving the bot outside the approved root.
        if not cwd:
            await query.edit_message_text(
                "❌ That session's working directory could not be confirmed "
                "within the approved directory and cannot be resumed.",
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
