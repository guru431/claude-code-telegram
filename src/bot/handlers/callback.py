"""Handle inline keyboard callbacks."""

import asyncio
from pathlib import Path
from typing import Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import ContextTypes

from ...claude.facade import ClaudeIntegration
from ...config.settings import Settings
from ...security.audit import AuditLogger
from ...security.validators import SecurityValidator
from ...utils.constants import TELEGRAM_MAX_MESSAGE_LENGTH
from ..middleware.rate_limit import estimate_prompt_cost
from ..utils.claude_run import run_claude_for_user
from ..utils.html_format import escape_html, split_telegram_html, tg_len
from ..utils.session_control import terminate_user_session
from ..utils.upload_limits import format_file_size

# session_export is imported lazily inside the export callback to keep the
# command/callback dispatch path lightweight — this module is the largest
# handler file and is imported on every bot startup.

logger = structlog.get_logger()


def _first_chunk(text: str, notice: str = "\n\n<i>(Response truncated)</i>") -> str:
    """Return *text* trimmed to one Telegram message without breaking markup.

    Slicing HTML by character position can cut inside a tag or an entity and
    leave the message unbalanced, which Telegram rejects with a 400. Splitting
    on the tag structure instead gives a first chunk that is well-formed on its
    own; the notice is appended only when something was actually dropped, and
    is included in the budget so the result still fits.
    """
    chunks = split_telegram_html(text, TELEGRAM_MAX_MESSAGE_LENGTH - tg_len(notice))
    if len(chunks) <= 1:
        return text
    return chunks[0] + notice


def _is_within_root(path: Path, root: Path) -> bool:
    """Check whether path is within root directory."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _get_thread_project_root(
    settings: Settings, context: ContextTypes.DEFAULT_TYPE
) -> Optional[Path]:
    """Get thread project root when strict thread mode is active."""
    if not settings.enable_project_threads:
        return None
    thread_context = context.user_data.get("_thread_context")
    if not thread_context:
        return None
    return Path(thread_context["project_root"]).resolve()


def _relative_to_root(
    current_dir: Path, settings: Settings, context: ContextTypes.DEFAULT_TYPE
) -> Path:
    """Return ``current_dir`` relative to the root that applies to this chat.

    In thread mode the project root comes from projects.yaml and may live
    outside ``approved_directory``, so a plain
    ``relative_to(settings.approved_directory)`` raises ValueError and takes the
    whole button handler into the generic error path. ``handle_cd_callback``
    already picks the base correctly; every other handler shares that choice
    here. If the directory is under neither root, fall back to its own name
    rather than exposing the absolute host path.
    """
    base = _get_thread_project_root(settings, context) or settings.approved_directory
    try:
        return current_dir.relative_to(base)
    except ValueError:
        return Path(current_dir.name)


async def handle_callback_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route callback queries to appropriate handlers."""
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    # The contextual formatting buttons answer the query themselves (with a
    # toast) in their routed branch below; pre-answering here would make a
    # second ``query.answer()`` fail with BadRequest and wipe Claude's reply.
    if data not in ("explain", "save_code", "show_files", "debug"):
        await query.answer()  # Acknowledge the callback

    logger.info("Processing callback query", user_id=user_id, callback_data=data)

    try:
        # Parse callback data
        if ":" in data:
            action, param = data.split(":", 1)
        else:
            action, param = data, None

        # Route to appropriate handler
        handlers = {
            "cd": handle_cd_callback,
            "action": handle_action_callback,
            "confirm": handle_confirm_callback,
            "quick": handle_quick_action_callback,
            "followup": handle_followup_callback,
            "conversation": handle_conversation_callback,
            "git": handle_git_callback,
            "export": handle_export_callback,
        }

        handler = handlers.get(action)
        if handler:
            await handler(query, param, context)
        elif data == "continue":
            # Bare "continue" button emitted by the response-formatting
            # keyboards — wire it to the existing continue-session logic.
            await _handle_continue_action(query, context)
        elif data in ("explain", "save_code", "show_files", "debug"):
            # Contextual formatting buttons without a backend implementation.
            # Surface a brief toast instead of the generic "Unknown Action".
            await query.answer(
                "This action isn't available yet — "
                "send a message to ask Claude directly.",
                show_alert=False,
            )
        else:
            await query.edit_message_text(
                "❌ <b>Unknown Action</b>\n\n"
                "This button action is not recognized. "
                "The bot may have been updated since this message was sent.",
                parse_mode="HTML",
            )

    except Exception as e:
        logger.error(
            "Error handling callback query",
            error=str(e),
            user_id=user_id,
            callback_data=data,
        )

        try:
            await query.edit_message_text(
                "❌ <b>Error Processing Action</b>\n\n"
                "An error occurred while processing your request.\n"
                "Please try again or use text commands.",
                parse_mode="HTML",
            )
        except Exception:
            # If we can't edit the message, send a new one. ``query.message``
            # may be ``None``/``InaccessibleMessage`` for buttons older than
            # 48h, so fall back to sending directly to the user.
            text = (
                "❌ <b>Error Processing Action</b>\n\n"
                "An error occurred while processing your request."
            )
            try:
                if isinstance(query.message, Message):
                    await query.message.reply_text(text, parse_mode="HTML")
                else:
                    await context.bot.send_message(
                        query.from_user.id, text, parse_mode="HTML"
                    )
            except Exception as send_error:
                logger.error(
                    "Failed to deliver callback error fallback message",
                    error=str(send_error),
                    user_id=user_id,
                    callback_data=data,
                )


async def handle_cd_callback(
    query, project_name: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle directory change from inline keyboard."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    security_validator: SecurityValidator = context.bot_data.get("security_validator")
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")

    try:
        current_dir = context.user_data.get(
            "current_directory", settings.approved_directory
        )
        project_root = _get_thread_project_root(settings, context)
        directory_root = project_root or settings.approved_directory

        # Handle special paths
        if project_name == "/":
            new_path = directory_root
        elif project_name == "..":
            new_path = current_dir.parent
            if not _is_within_root(new_path, directory_root):
                new_path = directory_root
        else:
            if project_root:
                new_path = current_dir / project_name
            else:
                new_path = settings.approved_directory / project_name

        # Validate path if security validator is available
        if security_validator:
            # Pass the absolute path for validation
            valid, resolved_path, error = security_validator.validate_path(
                str(new_path), settings.approved_directory
            )
            if not valid:
                await query.edit_message_text(
                    f"❌ <b>Access Denied</b>\n\n{escape_html(error)}",
                    parse_mode="HTML",
                )
                return
            # Use the validated path
            new_path = resolved_path

        if project_root and not _is_within_root(new_path, project_root):
            await query.edit_message_text(
                "❌ <b>Access Denied</b>\n\n"
                "In thread mode, navigation is limited to the current project root.",
                parse_mode="HTML",
            )
            return

        # Check if directory exists
        if not new_path.exists() or not new_path.is_dir():
            await query.edit_message_text(
                f"❌ <b>Directory Not Found</b>\n\n"
                f"The directory <code>{escape_html(project_name)}</code> no longer exists or is not accessible.",
                parse_mode="HTML",
            )
            return

        # Update directory and resume session for that directory when available
        context.user_data["current_directory"] = new_path

        resumed_session_info = ""
        if claude_integration:
            existing_session = await claude_integration.find_resumable_session(
                user_id, new_path
            )
            if existing_session:
                context.user_data["claude_session_id"] = existing_session.session_id
                resumed_session_info = (
                    f"\n🔄 Resumed session <code>{escape_html(existing_session.session_id[:8])}...</code> "
                    f"({existing_session.message_count} messages)"
                )
            else:
                context.user_data["claude_session_id"] = None
                resumed_session_info = (
                    "\n🆕 No existing session. Send a message to start a new one."
                )
        else:
            context.user_data["claude_session_id"] = None
            resumed_session_info = "\n🆕 Send a message to start a new session."

        # Send confirmation with new directory info
        relative_base = project_root or settings.approved_directory
        relative_path = new_path.relative_to(relative_base)
        relative_display = "/" if str(relative_path) == "." else f"{relative_path}/"

        # Add navigation buttons
        keyboard = [
            [
                InlineKeyboardButton("📁 List Files", callback_data="action:ls"),
                InlineKeyboardButton(
                    "🆕 New Session", callback_data="action:new_session"
                ),
            ],
            [
                InlineKeyboardButton(
                    "📋 Projects", callback_data="action:show_projects"
                ),
                InlineKeyboardButton("📊 Status", callback_data="action:status"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"✅ <b>Directory Changed</b>\n\n"
            f"📂 Current directory: <code>{escape_html(str(relative_display))}</code>"
            f"{resumed_session_info}",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

        # Log successful directory change
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id, command="cd", args=[project_name], success=True
            )

    except Exception as e:
        await query.edit_message_text(
            f"❌ <b>Error changing directory</b>\n\n{escape_html(str(e))}",
            parse_mode="HTML",
        )

        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id, command="cd", args=[project_name], success=False
            )


async def handle_action_callback(
    query, action_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle general action callbacks."""
    handler = ACTION_HANDLERS.get(action_type)
    if handler:
        await handler(query, context)
    else:
        await query.edit_message_text(
            f"❌ <b>Unknown Action: {escape_html(action_type)}</b>\n\n"
            "This action is not implemented yet.",
            parse_mode="HTML",
        )


async def handle_confirm_callback(
    query, confirmation_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle confirmation dialogs."""
    if confirmation_type == "yes":
        await query.edit_message_text(
            "✅ <b>Confirmed</b>\n\nAction will be processed.",
            parse_mode="HTML",
        )
    elif confirmation_type == "no":
        await query.edit_message_text(
            "❌ <b>Cancelled</b>\n\nAction was cancelled.",
            parse_mode="HTML",
        )
    else:
        await query.edit_message_text(
            "❓ <b>Unknown confirmation response</b>",
            parse_mode="HTML",
        )


# Action handlers


async def _handle_help_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle help action."""
    help_text = (
        "🤖 <b>Quick Help</b>\n\n"
        "<b>Navigation:</b>\n"
        "• <code>/ls</code> - List files\n"
        "• <code>/cd &lt;dir&gt;</code> - Change directory\n"
        "• <code>/projects</code> - Show projects\n\n"
        "<b>Sessions:</b>\n"
        "• <code>/new</code> - New Claude session\n"
        "• <code>/status</code> - Session status\n\n"
        "<b>Tips:</b>\n"
        "• Send any text to interact with Claude\n"
        "• Upload files for code review\n"
        "• Use buttons for quick actions\n\n"
        "Use <code>/help</code> for detailed help."
    )

    keyboard = [
        [
            InlineKeyboardButton("📖 Full Help", callback_data="action:full_help"),
            InlineKeyboardButton("🏠 Main Menu", callback_data="action:main_menu"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        help_text, parse_mode="HTML", reply_markup=reply_markup
    )


async def _handle_full_help_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the same detailed help text as the /help command."""
    from .command import FULL_HELP_TEXT

    await query.edit_message_text(
        FULL_HELP_TEXT,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="action:main_menu")]]
        ),
    )


async def _handle_main_menu_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the main menu (same entry points as /start)."""
    settings: Settings = context.bot_data["settings"]
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = _relative_to_root(current_dir, settings, context)

    keyboard = [
        [
            InlineKeyboardButton(
                "📁 Show Projects", callback_data="action:show_projects"
            ),
            InlineKeyboardButton("❓ Get Help", callback_data="action:help"),
        ],
        [
            InlineKeyboardButton("🆕 New Session", callback_data="action:new_session"),
            InlineKeyboardButton("📊 Check Status", callback_data="action:status"),
        ],
    ]

    await query.edit_message_text(
        "🏠 <b>Main Menu</b>\n\n"
        f"📂 Working directory: <code>{escape_html(str(relative_path))}/</code>\n\n"
        "Pick an action below, or just send a message to start coding.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _handle_show_projects_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle show projects action."""
    settings: Settings = context.bot_data["settings"]

    try:
        if settings.enable_project_threads:
            registry = context.bot_data.get("project_registry")
            if not registry:
                await query.edit_message_text(
                    "❌ <b>Project registry is not initialized.</b>",
                    parse_mode="HTML",
                )
                return

            projects = registry.list_enabled()
            if not projects:
                await query.edit_message_text(
                    "📁 <b>No Projects Found</b>\n\n"
                    "No enabled projects found in projects config.",
                    parse_mode="HTML",
                )
                return

            project_list = "\n".join(
                [
                    f"• <b>{escape_html(p.name)}</b> "
                    f"(<code>{escape_html(p.slug)}</code>) "
                    f"→ <code>{escape_html(str(p.relative_path))}</code>"
                    for p in projects
                ]
            )

            await query.edit_message_text(
                f"📁 <b>Configured Projects</b>\n\n{project_list}",
                parse_mode="HTML",
            )
            return

        # Get directories in approved directory (these are "projects"). The
        # blocking iterdir walk runs off the event loop.
        def _list_projects() -> list[str]:
            return [
                item.name
                for item in sorted(settings.approved_directory.iterdir())
                if item.is_dir() and not item.name.startswith(".")
            ]

        projects = await asyncio.to_thread(_list_projects)

        if not projects:
            await query.edit_message_text(
                "📁 <b>No Projects Found</b>\n\n"
                "No subdirectories found in your approved directory.\n"
                "Create some directories to organize your projects!",
                parse_mode="HTML",
            )
            return

        # Create project buttons. Telegram caps callback_data at 64 UTF-8
        # bytes; a multibyte name that overflows would break the whole
        # keyboard, so omit its button (the name still shows in the text
        # list and stays reachable via /cd).
        keyboard = []
        for i in range(0, len(projects), 2):
            row = []
            for j in range(2):
                if i + j < len(projects):
                    project = projects[i + j]
                    if len(f"cd:{project}".encode("utf-8")) > 64:
                        continue
                    row.append(
                        InlineKeyboardButton(
                            f"📁 {project}", callback_data=f"cd:{project}"
                        )
                    )
            if row:
                keyboard.append(row)

        # Add navigation buttons
        keyboard.append(
            [
                InlineKeyboardButton("🏠 Root", callback_data="cd:/"),
                InlineKeyboardButton(
                    "🔄 Refresh", callback_data="action:show_projects"
                ),
            ]
        )

        reply_markup = InlineKeyboardMarkup(keyboard)
        project_list = "\n".join(
            [f"• <code>{escape_html(project)}/</code>" for project in projects]
        )

        await query.edit_message_text(
            f"📁 <b>Available Projects</b>\n\n"
            f"{project_list}\n\n"
            f"Click a project to navigate to it:",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    except Exception as e:
        await query.edit_message_text(f"❌ Error loading projects: {str(e)}")


async def _handle_new_session_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle new session action."""
    settings: Settings = context.bot_data["settings"]

    # Clear session and force a fresh start so the next message does not
    # auto-resume the previous session.
    context.user_data["claude_session_id"] = None
    context.user_data["session_started"] = True
    context.user_data["force_new_session"] = True

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = _relative_to_root(current_dir, settings, context)

    keyboard = [
        [
            InlineKeyboardButton(
                "📝 Start Coding", callback_data="action:start_coding"
            ),
            InlineKeyboardButton(
                "📁 Change Project", callback_data="action:show_projects"
            ),
        ],
        [
            InlineKeyboardButton(
                "📋 Quick Actions", callback_data="action:quick_actions"
            ),
            InlineKeyboardButton("❓ Help", callback_data="action:help"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"🆕 <b>New Claude Code Session</b>\n\n"
        f"📂 Working directory: <code>{escape_html(str(relative_path))}/</code>\n\n"
        f"Ready to help you code! Send me a message to get started:",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def _handle_end_session_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle end session action."""
    settings: Settings = context.bot_data["settings"]

    # Check if there's an active session
    claude_session_id = context.user_data.get("claude_session_id")

    if not claude_session_id:
        await query.edit_message_text(
            "ℹ️ <b>No Active Session</b>\n\n"
            "There's no active Claude session to end.\n\n"
            "<b>What you can do:</b>\n"
            "• Use the button below to start a new session\n"
            "• Check your session status\n"
            "• Send any message to start a conversation",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🆕 New Session", callback_data="action:new_session"
                        )
                    ],
                    [InlineKeyboardButton("📊 Status", callback_data="action:status")],
                ]
            ),
        )
        return

    # Get current directory for display
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = _relative_to_root(current_dir, settings, context)

    # Deactivate the persisted session and clear the Telegram context, so the
    # next message does not auto-resume the just-ended session and /sessions
    # stops listing it as active.
    await terminate_user_session(context, query.from_user.id)

    # Create quick action buttons
    keyboard = [
        [
            InlineKeyboardButton("🆕 New Session", callback_data="action:new_session"),
            InlineKeyboardButton(
                "📁 Change Project", callback_data="action:show_projects"
            ),
        ],
        [
            InlineKeyboardButton("📊 Status", callback_data="action:status"),
            InlineKeyboardButton("❓ Help", callback_data="action:help"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "✅ <b>Session Ended</b>\n\n"
        f"Your Claude session has been terminated.\n\n"
        f"<b>Current Status:</b>\n"
        f"• Directory: <code>{escape_html(str(relative_path))}/</code>\n"
        f"• Session: None\n"
        f"• Ready for new commands\n\n"
        f"<b>Next Steps:</b>\n"
        f"• Start a new session\n"
        f"• Check status\n"
        f"• Send any message to begin a new conversation",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def _handle_continue_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle continue session action."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")
    rate_limiter = context.bot_data.get("rate_limiter")
    storage = context.bot_data.get("storage")

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        if not claude_integration:
            await query.edit_message_text(
                "❌ <b>Claude Integration Not Available</b>\n\n"
                "Claude integration is not properly configured.",
                parse_mode="HTML",
            )
            return

        # Check if there's an existing session in user context
        claude_session_id = context.user_data.get("claude_session_id")
        continue_prompt = "Please continue where we left off"

        if claude_session_id:
            # Continue with the existing session.
            await query.edit_message_text(
                f"🔄 <b>Continuing Session</b>\n\n"
                f"Session ID: <code>{escape_html(claude_session_id[:8])}...</code>\n"
                f"Directory: <code>{escape_html(str(_relative_to_root(current_dir, settings, context)))}/</code>\n\n"
                f"Continuing where you left off...",
                parse_mode="HTML",
            )

            async def _run():
                return await claude_integration.run_command(
                    prompt=continue_prompt,
                    working_directory=current_dir,
                    user_id=user_id,
                    session_id=claude_session_id,
                )

        else:
            # No session in context, try to find the most recent session
            await query.edit_message_text(
                "🔍 <b>Looking for Recent Session</b>\n\n"
                "Searching for your most recent session in this directory...",
                parse_mode="HTML",
            )

            async def _run():
                return await claude_integration.continue_session(
                    user_id=user_id,
                    working_directory=current_dir,
                    prompt=None,  # No prompt = use --continue
                )

        # Shared runner: holds the budget and persists the interaction, which
        # this button used to skip entirely.
        claude_response, budget_error = await run_claude_for_user(
            run=_run,
            prompt=continue_prompt,
            user_id=user_id,
            rate_limiter=rate_limiter,
            storage=storage,
            estimated_cost=estimate_prompt_cost(continue_prompt),
        )
        if budget_error:
            await query.edit_message_text(
                f"⏱️ {escape_html(budget_error)}", parse_mode="HTML"
            )
            return

        if claude_response:
            # Update session ID in context
            context.user_data["claude_session_id"] = claude_response.session_id

            # Send Claude's response. ``query.message`` may be ``None``/
            # ``InaccessibleMessage`` for buttons older than 48h, so fall back
            # to sending directly to the user.
            continued_text = (
                f"✅ <b>Session Continued</b>\n\n"
                f"{escape_html(claude_response.content[:500])}"
                f"{'...' if len(claude_response.content) > 500 else ''}"
            )
            if isinstance(query.message, Message):
                await query.message.reply_text(continued_text, parse_mode="HTML")
            else:
                await context.bot.send_message(
                    query.from_user.id, continued_text, parse_mode="HTML"
                )
        else:
            # No session found to continue
            await query.edit_message_text(
                "❌ <b>No Session Found</b>\n\n"
                f"No recent Claude session found in this directory.\n"
                f"Directory: <code>{escape_html(str(_relative_to_root(current_dir, settings, context)))}/</code>\n\n"
                f"<b>What you can do:</b>\n"
                f"• Use the button below to start a fresh session\n"
                f"• Check your session status\n"
                f"• Navigate to a different directory",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🆕 New Session", callback_data="action:new_session"
                            ),
                            InlineKeyboardButton(
                                "📊 Status", callback_data="action:status"
                            ),
                        ]
                    ]
                ),
            )

    except Exception as e:
        logger.error("Error in continue action", error=str(e), user_id=user_id)
        await query.edit_message_text(
            f"❌ <b>Error Continuing Session</b>\n\n"
            f"An error occurred: <code>{escape_html(str(e))}</code>\n\n"
            f"Try starting a new session instead.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🆕 New Session", callback_data="action:new_session"
                        )
                    ]
                ]
            ),
        )


async def _handle_status_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle status action."""
    # This essentially duplicates the /status command functionality
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]

    claude_session_id = context.user_data.get("claude_session_id")
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    relative_path = _relative_to_root(current_dir, settings, context)

    # Get usage info if rate limiter is available
    rate_limiter = context.bot_data.get("rate_limiter")
    usage_info = ""
    if rate_limiter:
        try:
            user_status = rate_limiter.get_user_status(user_id)
            cost_usage = user_status.get("cost_usage", {})
            current_cost = cost_usage.get("current", 0.0)
            cost_limit = cost_usage.get("limit", settings.claude_max_cost_per_user)
            cost_percentage = (current_cost / cost_limit) * 100 if cost_limit > 0 else 0

            usage_info = f"💰 Usage: ${current_cost:.2f} / ${cost_limit:.2f} ({cost_percentage:.0f}%)\n"
        except Exception:
            usage_info = "💰 Usage: <i>Unable to retrieve</i>\n"

    status_lines = [
        "📊 <b>Session Status</b>",
        "",
        f"📂 Directory: <code>{escape_html(str(relative_path))}/</code>",
        f"🤖 Claude Session: {'✅ Active' if claude_session_id else '❌ None'}",
        usage_info.rstrip(),
    ]

    if claude_session_id:
        status_lines.append(
            f"🆔 Session ID: <code>{escape_html(claude_session_id[:8])}...</code>"
        )

    # Add action buttons
    keyboard = []
    if claude_session_id:
        keyboard.append(
            [
                InlineKeyboardButton("🔄 Continue", callback_data="action:continue"),
                InlineKeyboardButton(
                    "🛑 End Session", callback_data="action:end_session"
                ),
            ]
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🆕 New Session", callback_data="action:new_session"
                ),
            ]
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🆕 Start Session", callback_data="action:new_session"
                )
            ]
        )

    keyboard.append(
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="action:refresh_status"),
            InlineKeyboardButton("📁 Projects", callback_data="action:show_projects"),
        ]
    )

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "\n".join(status_lines), parse_mode="HTML", reply_markup=reply_markup
    )


async def _handle_ls_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ls action."""
    settings: Settings = context.bot_data["settings"]
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        # List directory contents (similar to /ls command). The blocking
        # iterdir/stat walk runs off the event loop so a slow/large directory
        # doesn't stall other updates.
        def _walk_directory() -> list[str]:
            directories: list[str] = []
            files: list[str] = []
            for item in sorted(current_dir.iterdir()):
                if item.name.startswith("."):
                    continue

                # Escape markdown special characters in filenames
                safe_name = _escape_markdown(item.name)

                if item.is_dir():
                    directories.append(f"📁 {safe_name}/")
                else:
                    try:
                        size = item.stat().st_size
                        size_str = format_file_size(size)
                        files.append(f"📄 {safe_name} ({size_str})")
                    except OSError:
                        files.append(f"📄 {safe_name}")

            return directories + files

        items = await asyncio.to_thread(_walk_directory)
        relative_path = _relative_to_root(current_dir, settings, context)

        if not items:
            message = f"📂 <code>{escape_html(str(relative_path))}/</code>\n\n<i>(empty directory)</i>"
        else:
            message = f"📂 <code>{escape_html(str(relative_path))}/</code>\n\n"
            max_items = 30  # Limit for inline display
            if len(items) > max_items:
                shown_items = items[:max_items]
                message += "\n".join(shown_items)
                message += f"\n\n<i>... and {len(items) - max_items} more items</i>"
            else:
                message += "\n".join(items)

        # Add buttons
        keyboard = []
        if current_dir != settings.approved_directory:
            keyboard.append(
                [
                    InlineKeyboardButton("⬆️ Go Up", callback_data="cd:.."),
                    InlineKeyboardButton("🏠 Root", callback_data="cd:/"),
                ]
            )

        keyboard.append(
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="action:refresh_ls"),
                InlineKeyboardButton(
                    "📋 Projects", callback_data="action:show_projects"
                ),
            ]
        )

        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            message, parse_mode="HTML", reply_markup=reply_markup
        )

    except Exception as e:
        await query.edit_message_text(f"❌ Error listing directory: {str(e)}")


async def _handle_start_coding_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle start coding action."""
    await query.edit_message_text(
        "🚀 <b>Ready to Code!</b>\n\n"
        "Send me any message to start coding with Claude:\n\n"
        "<b>Examples:</b>\n"
        '• <i>"Create a Python script that..."</i>\n'
        '• <i>"Help me debug this code..."</i>\n'
        '• <i>"Explain how this file works..."</i>\n'
        "• Upload a file for review\n\n"
        "I'm here to help with all your coding needs!",
        parse_mode="HTML",
    )


async def _handle_quick_actions_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle quick actions menu."""
    # Build the menu dynamically from the real registered actions so every
    # button maps to an id that ``handle_quick_action_callback`` can run.
    features = context.bot_data.get("features")
    quick_actions = features.get_quick_actions() if features else None

    if not quick_actions:
        await query.edit_message_text(
            "❌ <b>Quick Actions Not Available</b>\n\n"
            "Quick actions feature is not available.",
            parse_mode="HTML",
        )
        return

    keyboard = []
    row = []
    for action in quick_actions.actions.values():
        row.append(
            InlineKeyboardButton(
                f"{action.icon} {action.name}",
                callback_data=f"quick:{action.id}",
            )
        )
        if len(row) >= 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append(
        [InlineKeyboardButton("⬅️ Back", callback_data="action:new_session")]
    )
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🛠️ <b>Quick Actions</b>\n\n" "Choose a common development task:",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def _handle_refresh_status_action(
    query, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle refresh status action."""
    await _handle_status_action(query, context)


async def _handle_refresh_ls_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle refresh ls action."""
    await _handle_ls_action(query, context)


async def _handle_export_action(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle export action — mirror the /export format-selection keyboard."""
    features = context.bot_data.get("features")
    session_exporter = features.get_session_export() if features else None

    if not session_exporter:
        await query.edit_message_text(
            "❌ <b>Export Unavailable</b>\n\n"
            "Session export service is not available.",
            parse_mode="HTML",
        )
        return

    claude_session_id = context.user_data.get("claude_session_id")
    if not claude_session_id:
        await query.edit_message_text(
            "❌ <b>No Active Session</b>\n\n"
            "There's no active Claude session to export.\n\n"
            "<b>What you can do:</b>\n"
            "• Start a new session with <code>/new</code>\n"
            "• Continue an existing session with <code>/continue</code>\n"
            "• Check your status with <code>/status</code>",
            parse_mode="HTML",
        )
        return

    keyboard = [
        [
            InlineKeyboardButton("📝 Markdown", callback_data="export:markdown"),
            InlineKeyboardButton("🌐 HTML", callback_data="export:html"),
        ],
        [
            InlineKeyboardButton("📋 JSON", callback_data="export:json"),
            InlineKeyboardButton("❌ Cancel", callback_data="export:cancel"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "📤 <b>Export Session</b>\n\n"
        f"Ready to export session: <code>{escape_html(claude_session_id[:8])}...</code>\n\n"
        "<b>Choose export format:</b>",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


async def handle_quick_action_callback(
    query, action_id: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle quick action callbacks."""
    user_id = query.from_user.id

    # Get quick actions manager via the feature registry if available.
    features = context.bot_data.get("features")
    quick_actions = features.get_quick_actions() if features else None

    if not quick_actions:
        await query.edit_message_text(
            "❌ <b>Quick Actions Not Available</b>\n\n"
            "Quick actions feature is not available.",
            parse_mode="HTML",
        )
        return

    # Get Claude integration
    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")
    if not claude_integration:
        await query.edit_message_text(
            "❌ <b>Claude Integration Not Available</b>\n\n"
            "Claude integration is not properly configured.",
            parse_mode="HTML",
        )
        return

    settings: Settings = context.bot_data["settings"]
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        # Get the action from the manager
        action = quick_actions.actions.get(action_id)
        if not action:
            await query.edit_message_text(
                f"❌ <b>Action Not Found</b>\n\n"
                f"Quick action '{escape_html(action_id)}' is not available.",
                parse_mode="HTML",
            )
            return

        # Execute the action
        await query.edit_message_text(
            f"🚀 <b>Executing {action.icon} {escape_html(action.name)}</b>\n\n"
            f"Running quick action in directory: <code>{escape_html(str(_relative_to_root(current_dir, settings, context)))}/</code>\n\n"
            f"Please wait...",
            parse_mode="HTML",
        )

        # Run the action through Claude via the shared runner so the run holds
        # budget and lands in history like a typed message would.
        async def _run():
            return await claude_integration.run_command(
                prompt=action.prompt, working_directory=current_dir, user_id=user_id
            )

        claude_response, budget_error = await run_claude_for_user(
            run=_run,
            prompt=action.prompt,
            user_id=user_id,
            rate_limiter=context.bot_data.get("rate_limiter"),
            storage=context.bot_data.get("storage"),
            estimated_cost=estimate_prompt_cost(action.prompt),
        )
        if budget_error:
            await query.edit_message_text(
                f"⏱️ {escape_html(budget_error)}", parse_mode="HTML"
            )
            return

        if claude_response is not None:
            # Format and send the response. Truncate the composed message, not
            # just the body -- the header also counts against Telegram's limit.
            # A run that succeeded but produced no text is not a failure; say so
            # rather than sending an empty body (which Telegram rejects).
            body = escape_html(claude_response.content or "") or "<i>(no output)</i>"
            done_text = _first_chunk(
                f"✅ <b>{action.icon} {escape_html(action.name)} Complete</b>\n\n"
                f"{body}"
            )
            if isinstance(query.message, Message):
                await query.message.reply_text(done_text, parse_mode="HTML")
            else:
                await context.bot.send_message(
                    query.from_user.id, done_text, parse_mode="HTML"
                )
        else:
            await query.edit_message_text(
                f"❌ <b>Action Failed</b>\n\n"
                f"Failed to execute {escape_html(action.name)}. Please try again.",
                parse_mode="HTML",
            )

    except Exception as e:
        logger.error("Quick action execution failed", error=str(e), user_id=user_id)
        await query.edit_message_text(
            f"❌ <b>Action Error</b>\n\n"
            f"An error occurred while executing {escape_html(action_id)}: {escape_html(str(e))}",
            parse_mode="HTML",
        )


async def handle_followup_callback(
    query, suggestion_hash: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle follow-up suggestion callbacks.

    Resolves the 12-char hash carried in callback_data back to the original
    suggestion text (stored in ``user_data`` when the keyboard was built) and
    runs it through Claude as if the user had typed it.
    """
    user_id = query.from_user.id

    # Resolve the hash back to the suggestion text.
    followup_map = context.user_data.get("followup_suggestions", {})
    suggestion = followup_map.get(suggestion_hash)

    if not suggestion:
        await query.edit_message_text(
            "💡 <b>Suggestion Unavailable</b>\n\n"
            "This follow-up suggestion has expired. "
            "Send a message to continue the conversation.",
            parse_mode="HTML",
        )
        return

    claude_integration: ClaudeIntegration = context.bot_data.get("claude_integration")
    if not claude_integration:
        await query.edit_message_text(
            "❌ <b>Claude Integration Not Available</b>\n\n"
            "Claude integration is not properly configured.",
            parse_mode="HTML",
        )
        return

    settings: Settings = context.bot_data["settings"]
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )
    session_id = context.user_data.get("claude_session_id")

    try:
        await query.edit_message_text(
            f"💡 <b>{escape_html(suggestion)}</b>\n\nWorking on it...",
            parse_mode="HTML",
        )

        async def _run():
            return await claude_integration.run_command(
                prompt=suggestion,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
            )

        # Shared runner: a follow-up button is a full Claude run and must be
        # budgeted and persisted like the message the user could have typed.
        claude_response, budget_error = await run_claude_for_user(
            run=_run,
            prompt=suggestion,
            user_id=user_id,
            rate_limiter=context.bot_data.get("rate_limiter"),
            storage=context.bot_data.get("storage"),
            estimated_cost=estimate_prompt_cost(suggestion),
        )
        if budget_error:
            await query.edit_message_text(
                f"⏱️ {escape_html(budget_error)}", parse_mode="HTML"
            )
            return

        if claude_response is not None:
            context.user_data["claude_session_id"] = claude_response.session_id

            # An empty body would be rejected by Telegram as an empty message.
            response_text = _first_chunk(
                escape_html(claude_response.content or "") or "<i>(no output)</i>"
            )
            if isinstance(query.message, Message):
                await query.message.reply_text(response_text, parse_mode="HTML")
            else:
                await context.bot.send_message(
                    query.from_user.id, response_text, parse_mode="HTML"
                )
        else:
            # No response at all: without this the user is left staring at
            # "Working on it..." forever.
            await query.edit_message_text(
                f"💡 <b>{escape_html(suggestion)}</b>\n\n"
                "❌ Claude returned no response. Please try again.",
                parse_mode="HTML",
            )

        logger.info(
            "Follow-up suggestion executed",
            user_id=user_id,
            suggestion_hash=suggestion_hash,
        )

    except Exception as e:
        logger.error(
            "Error handling follow-up callback",
            error=str(e),
            user_id=user_id,
            suggestion_hash=suggestion_hash,
        )

        await query.edit_message_text(
            "❌ <b>Error Processing Follow-up</b>\n\n"
            "An error occurred while processing your follow-up suggestion.",
            parse_mode="HTML",
        )


async def handle_conversation_callback(
    query, action_type: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle conversation control callbacks."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]

    if action_type == "continue":
        # Remove suggestion buttons and show continue message
        await query.edit_message_text(
            "✅ <b>Continuing Conversation</b>\n\n"
            "Send me your next message to continue coding!\n\n"
            "I'm ready to help with:\n"
            "• Code review and debugging\n"
            "• Feature implementation\n"
            "• Architecture decisions\n"
            "• Testing and optimization\n"
            "• Documentation\n\n"
            "<i>Just type your request or upload files.</i>",
            parse_mode="HTML",
        )

    elif action_type == "end":
        # End the current session
        features = context.bot_data.get("features")
        conversation_enhancer = (
            features.get_conversation_enhancer() if features else None
        )
        if conversation_enhancer:
            conversation_enhancer.clear_context(user_id)

        # Deactivate the persisted session and clear the Telegram context so
        # the next message starts fresh instead of auto-resuming the just-ended
        # session, and /sessions stops listing it as active.
        await terminate_user_session(context, user_id)

        current_dir = context.user_data.get(
            "current_directory", settings.approved_directory
        )
        relative_path = _relative_to_root(current_dir, settings, context)

        # Create quick action buttons
        keyboard = [
            [
                InlineKeyboardButton(
                    "🆕 New Session", callback_data="action:new_session"
                ),
                InlineKeyboardButton(
                    "📁 Change Project", callback_data="action:show_projects"
                ),
            ],
            [
                InlineKeyboardButton("📊 Status", callback_data="action:status"),
                InlineKeyboardButton("❓ Help", callback_data="action:help"),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "✅ <b>Conversation Ended</b>\n\n"
            f"Your Claude session has been terminated.\n\n"
            f"<b>Current Status:</b>\n"
            f"• Directory: <code>{escape_html(str(relative_path))}/</code>\n"
            f"• Session: None\n"
            f"• Ready for new commands\n\n"
            f"<b>Next Steps:</b>\n"
            f"• Start a new session\n"
            f"• Check status\n"
            f"• Send any message to begin a new conversation",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

        logger.info("Conversation ended via callback", user_id=user_id)

    else:
        await query.edit_message_text(
            f"❌ <b>Unknown Conversation Action: {escape_html(action_type)}</b>\n\n"
            "This conversation action is not recognized.",
            parse_mode="HTML",
        )


async def handle_git_callback(
    query, git_action: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle git-related callbacks."""
    user_id = query.from_user.id
    settings: Settings = context.bot_data["settings"]
    features = context.bot_data.get("features")

    if not features or not features.is_enabled("git"):
        await query.edit_message_text(
            "❌ <b>Git Integration Disabled</b>\n\n"
            "Git integration feature is not enabled.",
            parse_mode="HTML",
        )
        return

    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    try:
        git_integration = features.get_git_integration()
        if not git_integration:
            await query.edit_message_text(
                "❌ <b>Git Integration Unavailable</b>\n\n"
                "Git integration service is not available.",
                parse_mode="HTML",
            )
            return

        if git_action == "status":
            # Refresh git status
            git_status = await git_integration.get_status(current_dir)
            status_message = git_integration.format_status(git_status)

            keyboard = [
                [
                    InlineKeyboardButton("📊 Show Diff", callback_data="git:diff"),
                    InlineKeyboardButton("📜 Show Log", callback_data="git:log"),
                ],
                [
                    InlineKeyboardButton("🔄 Refresh", callback_data="git:status"),
                    InlineKeyboardButton("📁 Files", callback_data="action:ls"),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                status_message, parse_mode="HTML", reply_markup=reply_markup
            )

        elif git_action == "diff":
            # Show git diff
            diff_output = await git_integration.get_diff(current_dir)

            if not diff_output.strip():
                diff_message = "📊 <b>Git Diff</b>\n\n<i>No changes to show.</i>"
            else:
                # Clean up diff output for Telegram
                # Remove emoji symbols that interfere with parsing
                clean_diff = (
                    diff_output.replace("➕", "+").replace("➖", "-").replace("📍", "@")
                )

                # Escape first, then truncate the composed message: escaping
                # expands &, < and > to 4-5 chars each, so a budget measured on
                # the raw diff can still overflow once escaped.
                escaped_diff = escape_html(clean_diff)
                diff_message = _first_chunk(
                    f"📊 <b>Git Diff</b>\n\n<pre><code>{escaped_diff}</code></pre>",
                    notice="\n\n<i>(output truncated)</i>",
                )

            keyboard = [
                [
                    InlineKeyboardButton("📜 Show Log", callback_data="git:log"),
                    InlineKeyboardButton("📊 Status", callback_data="git:status"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                diff_message, parse_mode="HTML", reply_markup=reply_markup
            )

        elif git_action == "log":
            # Show git log
            commits = await git_integration.get_file_history(current_dir, ".")

            if not commits:
                log_message = "📜 <b>Git Log</b>\n\n<i>No commits found.</i>"
            else:
                log_message = "📜 <b>Git Log</b>\n\n"
                for commit in commits[:10]:  # Show last 10 commits
                    short_hash = commit.hash[:7]
                    short_message = escape_html(commit.message[:60])
                    if len(commit.message) > 60:
                        short_message += "..."
                    log_message += f"• <code>{short_hash}</code> {short_message}\n"

            keyboard = [
                [
                    InlineKeyboardButton("📊 Show Diff", callback_data="git:diff"),
                    InlineKeyboardButton("📊 Status", callback_data="git:status"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                log_message, parse_mode="HTML", reply_markup=reply_markup
            )

        else:
            await query.edit_message_text(
                f"❌ <b>Unknown Git Action: {escape_html(git_action)}</b>\n\n"
                "This git action is not recognized.",
                parse_mode="HTML",
            )

    except Exception as e:
        logger.error(
            "Error in git callback",
            error=str(e),
            git_action=git_action,
            user_id=user_id,
        )
        await query.edit_message_text(
            f"❌ <b>Git Error</b>\n\n{escape_html(str(e))}",
            parse_mode="HTML",
        )


async def handle_export_callback(
    query, export_format: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle export format selection callbacks."""
    user_id = query.from_user.id
    features = context.bot_data.get("features")

    if export_format == "cancel":
        await query.edit_message_text(
            "📤 <b>Export Cancelled</b>\n\n" "Session export has been cancelled.",
            parse_mode="HTML",
        )
        return

    session_exporter = features.get_session_export() if features else None
    if not session_exporter:
        await query.edit_message_text(
            "❌ <b>Export Unavailable</b>\n\n"
            "Session export service is not available.",
            parse_mode="HTML",
        )
        return

    # Get current session
    claude_session_id = context.user_data.get("claude_session_id")
    if not claude_session_id:
        await query.edit_message_text(
            "❌ <b>No Active Session</b>\n\n" "There's no active session to export.",
            parse_mode="HTML",
        )
        return

    try:
        # Show processing message
        await query.edit_message_text(
            f"📤 <b>Exporting Session</b>\n\n"
            f"Generating {escape_html(export_format.upper())} export...",
            parse_mode="HTML",
        )

        # Export session. ``SessionExporter.export_session`` expects
        # ``(user_id, session_id, format)`` with an :class:`ExportFormat`
        # enum value, not a raw string.
        from ..features.session_export import ExportFormat  # lazy import

        try:
            fmt = ExportFormat(export_format)
        except ValueError:
            await query.edit_message_text(
                f"❌ <b>Unsupported export format: {escape_html(export_format)}</b>",
                parse_mode="HTML",
            )
            return
        exported_session = await session_exporter.export_session(
            user_id=user_id,
            session_id=claude_session_id,
            format=fmt,
        )

        # Send the exported file
        from io import BytesIO

        file_bytes = BytesIO(exported_session.content.encode("utf-8"))
        file_bytes.name = exported_session.filename

        caption = (
            f"📤 <b>Session Export Complete</b>\n\n"
            f"Format: {escape_html(exported_session.format.value.upper())}\n"
            f"Size: {exported_session.size_bytes:,} bytes\n"
            f"Created: {exported_session.created_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if isinstance(query.message, Message):
            await query.message.reply_document(
                document=file_bytes,
                filename=exported_session.filename,
                caption=caption,
                parse_mode="HTML",
            )
        else:
            await context.bot.send_document(
                chat_id=query.from_user.id,
                document=file_bytes,
                filename=exported_session.filename,
                caption=caption,
                parse_mode="HTML",
            )

        # Update the original message
        await query.edit_message_text(
            f"✅ <b>Export Complete</b>\n\n"
            f"Your session has been exported as {escape_html(exported_session.filename)}.\n"
            f"Check the file above for your complete conversation history.",
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(
            "Export failed", error=str(e), user_id=user_id, format=export_format
        )
        await query.edit_message_text(
            f"❌ <b>Export Failed</b>\n\n{escape_html(str(e))}",
            parse_mode="HTML",
        )


def _escape_markdown(text: str) -> str:
    """Escape HTML-special characters in text for Telegram.

    Legacy name kept for compatibility with callers; actually escapes HTML.
    """
    return escape_html(text)


# Dispatch table for ``action:<name>`` callback_data. Module-level so a test can
# assert every action button rendered anywhere in the bot resolves to a handler
# instead of silently falling through to "Unknown Action".
ACTION_HANDLERS = {
    "help": _handle_help_action,
    "full_help": _handle_full_help_action,
    "main_menu": _handle_main_menu_action,
    "show_projects": _handle_show_projects_action,
    "new_session": _handle_new_session_action,
    "continue": _handle_continue_action,
    "end_session": _handle_end_session_action,
    "status": _handle_status_action,
    "ls": _handle_ls_action,
    "start_coding": _handle_start_coding_action,
    "quick_actions": _handle_quick_actions_action,
    "refresh_status": _handle_refresh_status_action,
    "refresh_ls": _handle_refresh_ls_action,
    "export": _handle_export_action,
}
