"""Message handlers for non-command inputs."""

import asyncio
from typing import Optional

import structlog
from telegram import InputMediaPhoto, Update
from telegram.ext import ContextTypes

from ...claude.exceptions import (
    ClaudeError,
    ClaudeMCPError,
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeSessionError,
    ClaudeTimeoutError,
)
from ...config.settings import Settings
from ...security.audit import AuditLogger
from ...security.rate_limiter import RateLimiter
from ...security.validators import SecurityValidator
from ..features.file_handler import FileTooLargeError
from ..middleware.rate_limit import estimate_message_cost
from ..utils.claude_run import persist_interaction
from ..utils.html_format import escape_html
from ..utils.image_extractor import (
    ImageAttachment,
    should_send_as_photo,
    validate_image_path,
)
from ..utils.upload_limits import exceeds_upload_limit

logger = structlog.get_logger()


async def _format_progress_update(update_obj) -> Optional[str]:
    """Format progress updates with enhanced context and visual indicators."""
    if update_obj.type == "tool_result":
        # Show tool completion status
        tool_name = "Unknown"
        if update_obj.metadata and update_obj.metadata.get("tool_use_id"):
            # Try to extract tool name from context if available
            tool_name = update_obj.metadata.get("tool_name", "Tool")

        if update_obj.is_error():
            return (
                f"❌ <b>{escape_html(tool_name)} failed</b>\n\n"
                f"<i>{escape_html(update_obj.get_error_message() or '')}</i>"
            )
        else:
            execution_time = ""
            if update_obj.metadata and update_obj.metadata.get("execution_time_ms"):
                time_ms = update_obj.metadata["execution_time_ms"]
                execution_time = f" ({time_ms}ms)"
            return f"✅ <b>{escape_html(tool_name)} completed</b>{execution_time}"

    elif update_obj.type == "progress":
        # Handle progress updates
        progress_text = f"🔄 <b>{escape_html(update_obj.content or 'Working...')}</b>"

        percentage = update_obj.get_progress_percentage()
        if percentage is not None:
            # Create a simple progress bar
            filled = int(percentage / 10)  # 0-10 scale
            bar = "█" * filled + "░" * (10 - filled)
            progress_text += f"\n\n<code>{bar}</code> {percentage}%"

        if update_obj.progress:
            step = update_obj.progress.get("step")
            total_steps = update_obj.progress.get("total_steps")
            if step and total_steps:
                progress_text += f"\n\nStep {step} of {total_steps}"

        return progress_text

    elif update_obj.type == "error":
        # Handle error messages
        return (
            f"❌ <b>Error</b>\n\n"
            f"<i>{escape_html(update_obj.get_error_message() or '')}</i>"
        )

    elif update_obj.type == "assistant" and update_obj.tool_calls:
        # Show when tools are being called
        tool_names = update_obj.get_tool_names()
        if tool_names:
            tools_text = ", ".join(tool_names)
            return f"🔧 <b>Using tools:</b> {escape_html(tools_text)}"

    elif update_obj.type == "assistant" and update_obj.content:
        # Regular content updates with preview
        content_preview = (
            update_obj.content[:150] + "..."
            if len(update_obj.content) > 150
            else update_obj.content
        )
        return (
            f"🤖 <b>Claude is working...</b>\n\n"
            f"<i>{escape_html(content_preview)}</i>"
        )

    elif update_obj.type == "system":
        # System initialization or other system messages
        if update_obj.metadata and update_obj.metadata.get("subtype") == "init":
            tools_count = len(update_obj.metadata.get("tools", []))
            model = update_obj.metadata.get("model", "Claude")
            return f"🚀 <b>Starting {model}</b> with {tools_count} tools available"

    return None


def _format_error_message(error: Exception | str) -> str:
    """Format error messages for user-friendly display.

    Accepts an exception object (preferred) or a string for backward
    compatibility.  When an exception is provided, the error type is used
    to produce a specific, actionable message.
    """
    # Normalise: keep both the object and a string representation.
    if isinstance(error, str):
        error_str = error
        error_obj: Exception | None = None
    else:
        error_str = str(error)
        error_obj = error

    # --- Dispatch on exception type first (most specific) ---

    if isinstance(error_obj, ClaudeTimeoutError):
        return (
            "⏰ <b>Request Timeout</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Try breaking your request into smaller parts\n"
            "• Avoid asking for very large file operations in one go\n"
            "• Try again — transient slowdowns happen"
        )

    if isinstance(error_obj, ClaudeMCPError):
        server_hint = ""
        if error_obj.server_name:
            server_hint = f" (<code>{escape_html(error_obj.server_name)}</code>)"
        return (
            f"🔌 <b>MCP Server Error</b>{server_hint}\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Check that the MCP server is running and reachable\n"
            "• Verify <code>MCP_CONFIG_PATH</code> points to a valid config\n"
            "• Ask the administrator to check MCP server logs"
        )

    if isinstance(error_obj, ClaudeParsingError):
        return (
            "📄 <b>Response Parsing Error</b>\n\n"
            f"Claude returned a response that could not be parsed:\n"
            f"<code>{escape_html(error_str[:300])}</code>\n\n"
            "<b>What you can do:</b>\n"
            "• Try your request again\n"
            "• Rephrase your prompt if the problem persists"
        )

    if isinstance(error_obj, ClaudeSessionError):
        return (
            "🔄 <b>Session Error</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Use /new to start a fresh session\n"
            "• Try your request again\n"
            "• Use /status to check your current session"
        )

    if isinstance(error_obj, ClaudeProcessError):
        return _format_process_error(error_str)

    # Any future ClaudeError subtypes not explicitly handled above —
    # preserve their existing message as-is rather than downgrading
    # to a generic "process error".
    if isinstance(error_obj, ClaudeError):
        safe_error = escape_html(error_str)
        if len(safe_error) > 500:
            safe_error = safe_error[:500] + "..."
        return (
            f"❌ <b>Claude Error</b>\n\n"
            f"{safe_error}\n\n"
            f"Try again or use /new to start a fresh session."
        )

    # --- Fall back to keyword matching (for string-only callers) --------
    # These patterns match the known error prefixes produced by
    # sdk_integration.py and facade.py, NOT arbitrary user content.

    error_lower = error_str.lower()

    if "usage limit reached" in error_lower or "usage limit" in error_lower:
        return error_str  # Already user-friendly

    if "tool not allowed" in error_lower:
        return error_str  # Already formatted by facade.py

    if "no conversation found" in error_lower:
        return (
            "🔄 <b>Session Not Found</b>\n\n"
            "The previous Claude session could not be found or has expired.\n\n"
            "<b>What you can do:</b>\n"
            "• Use /new to start a fresh session\n"
            "• Try your request again\n"
            "• Use /status to check your current session"
        )

    if "rate limit" in error_lower:
        return (
            "⏱️ <b>Rate Limit Reached</b>\n\n"
            "Too many requests in a short time period.\n\n"
            "<b>What you can do:</b>\n"
            "• Wait a moment before trying again\n"
            "• Use simpler requests\n"
            "• Check your current usage with /status"
        )

    if "timed out after" in error_lower or "claude sdk timed out" in error_lower:
        return (
            "⏰ <b>Request Timeout</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Try breaking your request into smaller parts\n"
            "• Avoid asking for very large file operations in one go\n"
            "• Try again — transient slowdowns happen"
        )

    if "overloaded" in error_lower:
        return (
            "🏗️ <b>Claude is Overloaded</b>\n\n"
            "The Claude API is currently experiencing high demand.\n\n"
            "<b>What you can do:</b>\n"
            "• Wait a moment and try again\n"
            "• Shorter prompts may succeed more easily"
        )

    if "invalid api key" in error_lower or "authentication_error" in error_lower:
        return (
            "🔑 <b>API Authentication Error</b>\n\n"
            "The API key used to connect to Claude is invalid or expired.\n\n"
            "<b>What you can do:</b>\n"
            "• Ask the administrator to verify the "
            "<code>ANTHROPIC_API_KEY</code> setting\n"
            "• Check that the API key has not been revoked"
        )

    # Match known SDK prefixes: "Failed to connect to Claude: ..."
    # and "MCP server connection failed: ..."
    if error_lower.startswith("failed to connect to claude"):
        return (
            "🌐 <b>Connection Error</b>\n\n"
            f"Could not connect to Claude:\n"
            f"<code>{escape_html(error_str[:300])}</code>\n\n"
            "<b>What you can do:</b>\n"
            "• Check your network / firewall settings\n"
            "• Verify the Claude CLI is installed and accessible\n"
            "• Try again in a moment"
        )

    # Match known SDK prefix: "Claude Code not found. ..."
    if error_lower.startswith("claude code not found"):
        return (
            "🔍 <b>Claude CLI Not Found</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Ensure Claude Code is installed: "
            "<code>npm install -g @anthropic-ai/claude-code</code>\n"
            "• Set the <code>CLAUDE_CLI_PATH</code> environment variable"
        )

    # Match known SDK prefixes: "MCP server error: ..." and
    # "MCP server connection failed: ..."
    if error_lower.startswith("mcp server"):
        return (
            "🔌 <b>MCP Server Error</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Check that the MCP server is running\n"
            "• Verify MCP configuration\n"
            "• Ask the administrator to check MCP server logs"
        )

    # Voice transcription failures are a known, user-actionable category (the
    # provider rejected/failed the request) — surface the message rather than
    # hiding it behind the generic fallback.
    if "transcription" in error_lower:
        return (
            "🎙️ <b>Transcription Failed</b>\n\n"
            f"{escape_html(error_str)}\n\n"
            "<b>What you can do:</b>\n"
            "• Try sending the voice message again\n"
            "• Send your request as text instead"
        )

    # --- No match — unknown error. Do NOT echo the raw exception to the user:
    # it can leak internal paths, config, or stack details. Log the full text
    # and show a generic message instead.
    logger.error("Unhandled error surfaced to user", error=error_str)
    return (
        "❌ <b>Unexpected Error</b>\n\n"
        "Something went wrong while handling your request. "
        "The details have been logged for the administrator.\n\n"
        "<b>What you can do:</b>\n"
        "• Try again\n"
        "• Use /new to start a fresh session if the problem persists\n"
        "• Check /status for your current session state"
    )


def _format_process_error(error_str: str) -> str:
    """Format a Claude process/SDK error with the actual details."""
    safe_error = escape_html(error_str)
    if len(safe_error) > 500:
        safe_error = safe_error[:500] + "..."

    return (
        f"❌ <b>Claude Process Error</b>\n\n"
        f"{safe_error}\n\n"
        "<b>What you can do:</b>\n"
        "• Try your request again\n"
        "• Use /new to start a fresh session if the problem persists\n"
        "• Check /status for current session state"
    )


async def handle_text_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle regular text messages as Claude prompts."""
    user_id = update.effective_user.id
    message_text = update.message.text
    settings: Settings = context.bot_data["settings"]

    # Get services
    rate_limiter: Optional[RateLimiter] = context.bot_data.get("rate_limiter")
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")

    logger.info(
        "Processing text message", user_id=user_id, message_length=len(message_text)
    )

    try:
        # Rate limiting was already enforced by rate_limit_middleware (group -1);
        # re-checking here would double-charge the token bucket. Budget for the
        # run is held via rate_limiter.reserve_cost and released with the real
        # cost via rate_limiter.settle_reservation below.

        # Send typing indicator
        await update.message.chat.send_action("typing")

        # Create progress message
        progress_msg = await update.message.reply_text(
            "🤔 Processing your request...",
            reply_to_message_id=update.message.message_id,
        )

        # Get Claude integration and storage from context
        claude_integration = context.bot_data.get("claude_integration")
        storage = context.bot_data.get("storage")

        if not claude_integration:
            # Remove the orphaned progress message before bailing out.
            try:
                await progress_msg.delete()
            except Exception:
                logger.debug("Failed to delete progress message, ignoring")
            await update.message.reply_text(
                "❌ <b>Claude integration not available</b>\n\n"
                "The Claude Code integration is not properly configured. "
                "Please contact the administrator.",
                parse_mode="HTML",
            )
            return

        # Get current directory
        current_dir = context.user_data.get(
            "current_directory", settings.approved_directory
        )

        # Get existing session ID
        session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        # MCP image collection via stream intercept
        mcp_images: list[ImageAttachment] = []

        # Throttle progress edits to at most once per 2s (mirrors the agentic
        # orchestrator) — unthrottled edits hit Telegram rate limits.
        last_edit = [0.0]

        # Enhanced stream updates handler with progress tracking
        async def stream_handler(update_obj):
            # Intercept send_image_to_user MCP tool calls.
            # The SDK namespaces MCP tools as "mcp__<server>__<tool>".
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    tc_name = tc.get("name", "")
                    if tc_name == "send_image_to_user" or tc_name.endswith(
                        "__send_image_to_user"
                    ):
                        tc_input = tc.get("input", {})
                        file_path = tc_input.get("file_path", "")
                        caption = tc_input.get("caption", "")
                        img = validate_image_path(
                            file_path, settings.approved_directory, caption
                        )
                        if img:
                            mcp_images.append(img)

            now = asyncio.get_event_loop().time()
            if now - last_edit[0] < 2.0:
                return
            try:
                progress_text = await _format_progress_update(update_obj)
                if progress_text:
                    await progress_msg.edit_text(progress_text, parse_mode="HTML")
                    last_edit[0] = now
            except Exception as e:
                logger.warning("Failed to update progress message", error=str(e))

        # Hold budget for this specific run. The middleware only throttled;
        # money is charged here and released in the finally below on every
        # path (success, soft error, exception, cancel).
        reservation_id: Optional[str] = None
        actual_cost = 0.0
        if rate_limiter:
            reservation_id, reserve_error = await rate_limiter.reserve_cost(
                user_id, estimate_message_cost(update)
            )
            if reserve_error:
                await progress_msg.edit_text(
                    f"⏱️ {escape_html(reserve_error)}", parse_mode="HTML"
                )
                return

        # Run Claude command. Initialize so a failure inside run_command leaves
        # claude_response bound (the inner except below references it).
        claude_response = None
        try:
            claude_response = await claude_integration.run_command(
                prompt=message_text,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=stream_handler,
                force_new=force_new,
            )

            # New session created successfully — clear the one-shot flag
            if force_new:
                context.user_data["force_new_session"] = False

            # Update session ID
            context.user_data["claude_session_id"] = claude_response.session_id

            # Settled in the finally below so a later failure cannot leave the
            # hold outstanding. A run flagged is_error produced nothing usable
            # and is not charged.
            if not claude_response.is_error:
                actual_cost = claude_response.cost

            # Check if Claude changed the working directory and update our tracking
            _update_working_directory_from_claude_response(
                claude_response, context, settings, user_id
            )

            # Log interaction to storage
            await persist_interaction(storage, user_id, message_text, claude_response)

            # Format response
            from ..utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

        except Exception as e:
            logger.error("Claude integration failed", error=str(e), user_id=user_id)
            from ..utils.formatting import FormattedMessage

            formatted_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]
        finally:
            # Always release the hold. A run that produced no usable result
            # settles at 0.0 and costs the user nothing.
            if rate_limiter and reservation_id:
                await rate_limiter.settle_reservation(reservation_id, actual_cost)

        # Delete progress message
        await progress_msg.delete()

        # Use MCP-collected images (from send_image_to_user tool calls)
        images: list[ImageAttachment] = mcp_images

        # Try to combine text + images when response fits in a caption
        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                photos = [i for i in images if should_send_as_photo(i.path)]
                documents = [i for i in images if not should_send_as_photo(i.path)]
                if photos and not documents:
                    try:
                        if len(photos) == 1:
                            # Read the image off the event loop; PTB would
                            # otherwise read the whole body synchronously.
                            data = await asyncio.to_thread(photos[0].path.read_bytes)
                            await update.message.reply_photo(
                                photo=data,
                                caption=msg.text,
                                parse_mode=msg.parse_mode,
                                reply_to_message_id=update.message.message_id,
                            )
                            caption_sent = True
                        else:
                            media = []
                            for idx, img in enumerate(photos[:10]):
                                data = await asyncio.to_thread(img.path.read_bytes)
                                media.append(
                                    InputMediaPhoto(
                                        media=data,
                                        caption=msg.text if idx == 0 else None,
                                        parse_mode=(
                                            msg.parse_mode if idx == 0 else None
                                        ),
                                    )
                                )
                            await update.message.chat.send_media_group(
                                media=media,
                                reply_to_message_id=update.message.message_id,
                            )
                            caption_sent = True
                    except Exception as album_err:
                        logger.warning(
                            "Failed to send photo+caption", error=str(album_err)
                        )

        if not caption_sent:
            # Send formatted responses (may be multiple messages)
            for i, message in enumerate(formatted_messages):
                try:
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=message.reply_markup,
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
                            reply_markup=message.reply_markup,
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                    except Exception as plain_err:
                        logger.error(
                            "Failed to send plain text fallback response",
                            error=str(plain_err),
                        )
                        await update.message.reply_text(
                            f"Failed to deliver response "
                            f"(Telegram error: {str(plain_err)[:150]}). "
                            f"Please try again.",
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )

            # Send images separately
            if images:
                photos = [i for i in images if should_send_as_photo(i.path)]
                documents = [i for i in images if not should_send_as_photo(i.path)]
                if photos:
                    try:
                        if len(photos) == 1:
                            # Read the image off the event loop; PTB would
                            # otherwise read the whole body synchronously.
                            data = await asyncio.to_thread(photos[0].path.read_bytes)
                            await update.message.reply_photo(
                                photo=data,
                                reply_to_message_id=update.message.message_id,
                            )
                        else:
                            media = []
                            for img in photos[:10]:
                                data = await asyncio.to_thread(img.path.read_bytes)
                                media.append(InputMediaPhoto(media=data))
                            await update.message.chat.send_media_group(
                                media=media,
                                reply_to_message_id=update.message.message_id,
                            )
                    except Exception as album_err:
                        logger.warning(
                            "Failed to send photo album", error=str(album_err)
                        )
                for img in documents:
                    try:
                        # Read the file off the event loop before handing the
                        # bytes to PTB.
                        data = await asyncio.to_thread(img.path.read_bytes)
                        await update.message.reply_document(
                            document=data,
                            filename=img.path.name,
                            reply_to_message_id=update.message.message_id,
                        )
                        await asyncio.sleep(0.5)
                    except Exception as doc_err:
                        logger.warning(
                            "Failed to send document image",
                            path=str(img.path),
                            error=str(doc_err),
                        )

        # Update session info
        context.user_data["last_message"] = update.message.text

        # Add conversation enhancements if available
        features = context.bot_data.get("features")
        conversation_enhancer = (
            features.get_conversation_enhancer() if features else None
        )

        if conversation_enhancer and claude_response:
            try:
                # Update conversation context. ``ConversationEnhancer``
                # accepts ``(user_id, ClaudeResponse)`` — pass the whole
                # response object rather than a bag of keyword arguments.
                conversation_enhancer.update_context(user_id, claude_response)
                conversation_context = conversation_enhancer.get_or_create_context(
                    user_id
                )

                # Check if we should show follow-up suggestions
                if conversation_enhancer.should_show_suggestions(claude_response):
                    # Generate follow-up suggestions
                    suggestions = conversation_enhancer.generate_follow_up_suggestions(
                        claude_response,
                        conversation_context,
                    )

                    if suggestions:
                        # Create keyboard with suggestions
                        suggestion_keyboard = (
                            conversation_enhancer.create_follow_up_keyboard(suggestions)
                        )

                        # Persist the hash->text mapping so the followup
                        # callback can recover the suggestion the user tapped
                        # (callback_data only carries the 12-char hash).
                        followup_map = context.user_data.setdefault(
                            "followup_suggestions", {}
                        )
                        for suggestion in suggestions[:4]:
                            followup_map[
                                conversation_enhancer.suggestion_hash(suggestion)
                            ] = suggestion

                        # Send follow-up suggestions
                        await update.message.reply_text(
                            "💡 <b>What would you like to do next?</b>",
                            parse_mode="HTML",
                            reply_markup=suggestion_keyboard,
                        )

            except Exception as e:
                logger.warning(
                    "Conversation enhancement failed", error=str(e), user_id=user_id
                )

        # Log successful message processing. The response was already delivered
        # above, so an audit failure here must not fall through to the outer
        # except — that would send the user a duplicate error message and log
        # the interaction as failed.
        if audit_logger:
            try:
                await audit_logger.log_command(
                    user_id=user_id,
                    command="text_message",
                    args=[update.message.text[:100]],  # First 100 chars
                    success=True,
                )
            except Exception as audit_err:
                logger.warning(
                    "Failed to write success audit log",
                    error=str(audit_err),
                    user_id=user_id,
                )

        logger.info("Text message processed successfully", user_id=user_id)

    except Exception as e:
        # Clean up progress message if it exists
        try:
            await progress_msg.delete()
        except Exception as delete_error:
            logger.debug("Failed to delete progress message", error=str(delete_error))

        await update.message.reply_text(_format_error_message(e), parse_mode="HTML")

        # Log failed processing
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[update.message.text[:100]],
                success=False,
            )

        logger.error("Error processing text message", error=str(e), user_id=user_id)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle file uploads."""
    user_id = update.effective_user.id
    document = update.message.document
    settings: Settings = context.bot_data["settings"]

    # Initialize prompt to avoid UnboundLocalError
    prompt: str = ""

    # Initialize so the outer except can safely clean it up even when the
    # failure happens before the progress message is created (~767 below).
    progress_msg = None

    # Get services
    security_validator: Optional[SecurityValidator] = context.bot_data.get(
        "security_validator"
    )
    audit_logger: Optional[AuditLogger] = context.bot_data.get("audit_logger")
    rate_limiter: Optional[RateLimiter] = context.bot_data.get("rate_limiter")

    logger.info(
        "Processing document upload",
        user_id=user_id,
        filename=document.file_name,
        file_size=document.file_size,
    )

    try:
        # Validate filename using security validator. ``file_name`` is optional
        # in Telegram and may be None for nameless uploads — fall back to a
        # placeholder so the typed validator never receives None.
        if security_validator:
            valid, error = security_validator.validate_filename(
                document.file_name or "document"
            )
            if not valid:
                await update.message.reply_text(
                    f"❌ <b>File Upload Rejected</b>\n\n{escape_html(error)}",
                    parse_mode="HTML",
                )

                # Log security violation
                if audit_logger:
                    await audit_logger.log_security_violation(
                        user_id=user_id,
                        violation_type="invalid_file_upload",
                        details=f"Filename: {document.file_name}, Error: {error}",
                        severity="medium",
                    )
                return

        # Check file size limits. ``file_size`` is optional in Telegram, so an
        # absent size is "not yet verified" (never an implicit 0 that passes);
        # the real byte length is re-checked after download below.
        max_size = settings.max_file_upload_size_bytes
        max_mb = settings.max_file_upload_size_mb
        if exceeds_upload_limit(document.file_size, max_size):
            await update.message.reply_text(
                f"❌ <b>File Too Large</b>\n\n"
                f"Maximum file size: {max_mb}MB\n"
                f"Your file: {document.file_size / 1024 / 1024:.1f}MB",
                parse_mode="HTML",
            )
            return

        # Throttling was already applied to this update by rate_limit_middleware
        # (group -1); re-checking here would burn a second bucket token. Budget
        # is held just before the Claude run below via reserve_cost.

        # Send processing indicator
        await update.message.chat.send_action("upload_document")

        progress_msg = await update.message.reply_text(
            f"📄 Processing file: <code>{document.file_name}</code>...",
            parse_mode="HTML",
        )

        # Check if enhanced file handler is available
        features = context.bot_data.get("features")
        file_handler = features.get_file_handler() if features else None

        if file_handler:
            # Use enhanced file handler
            try:
                processed_file = await file_handler.handle_document_upload(
                    document,
                    user_id,
                    update.message.caption or "Please review this file:",
                )
                prompt = processed_file.prompt

                # Update progress message with file type info
                await progress_msg.edit_text(
                    f"📄 Processing {processed_file.type} file: <code>{document.file_name}</code>...",
                    parse_mode="HTML",
                )

            except FileTooLargeError as e:
                # Falling back would download the same over-limit file again.
                await progress_msg.edit_text(
                    f"❌ <b>File Too Large</b>\n\n{escape_html(str(e))}",
                    parse_mode="HTML",
                )
                return
            except Exception as e:
                logger.warning(
                    "Enhanced file handler failed, falling back to basic handler",
                    error=str(e),
                )
                file_handler = None  # Fall back to basic handling

        if not file_handler:
            # Fall back to basic file handling
            file = await document.get_file()
            # Re-check the resolved File metadata, then the bytes actually
            # received: a missing or understated Document.file_size must not
            # let an over-limit payload through.
            if exceeds_upload_limit(getattr(file, "file_size", None), max_size):
                await update.message.reply_text(
                    f"❌ <b>File Too Large</b>\n\n"
                    f"Maximum file size: {max_mb}MB\n"
                    f"Your file: {getattr(file, 'file_size') / 1024 / 1024:.1f}MB",
                    parse_mode="HTML",
                )
                return
            file_bytes = await file.download_as_bytearray()
            if exceeds_upload_limit(len(file_bytes), max_size):
                await update.message.reply_text(
                    f"❌ <b>File Too Large</b>\n\n"
                    f"Maximum file size: {max_mb}MB\n"
                    f"Your file: {len(file_bytes) / 1024 / 1024:.1f}MB",
                    parse_mode="HTML",
                )
                return

            # Try to decode as text
            try:
                content = file_bytes.decode("utf-8")

                # Check content length
                max_content_length = 50000  # 50KB of text
                if len(content) > max_content_length:
                    content = (
                        content[:max_content_length]
                        + "\n... (file truncated for processing)"
                    )

                # Create prompt with file content
                caption = update.message.caption or "Please review this file:"
                prompt = f"{caption}\n\n**File:** `{document.file_name}`\n\n```\n{content}\n```"

            except UnicodeDecodeError:
                await progress_msg.edit_text(
                    "❌ <b>File Format Not Supported</b>\n\n"
                    "File must be text-based and UTF-8 encoded.\n\n"
                    "<b>Supported formats:</b>\n"
                    "• Source code files (.py, .js, .ts, etc.)\n"
                    "• Text files (.txt, .md)\n"
                    "• Configuration files (.json, .yaml, .toml)\n"
                    "• Documentation files",
                    parse_mode="HTML",
                )
                return

        # Delete progress message
        await progress_msg.delete()

        # Create a new progress message for Claude processing
        claude_progress_msg = await update.message.reply_text(
            "🤖 Processing file with Claude...", parse_mode="HTML"
        )

        # Get Claude integration from context
        claude_integration = context.bot_data.get("claude_integration")

        if not claude_integration:
            await claude_progress_msg.edit_text(
                "❌ <b>Claude integration not available</b>\n\n"
                "The Claude Code integration is not properly configured.",
                parse_mode="HTML",
            )
            return

        # Get current directory and session
        current_dir = context.user_data.get(
            "current_directory", settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")

        # Hold budget for this run, sized from the actual upload; released in
        # the finally below on every path so a failure costs the user nothing.
        file_cost = _estimate_file_processing_cost(document.file_size)
        reservation_id: Optional[str] = None
        actual_cost = 0.0
        if rate_limiter:
            reservation_id, reserve_error = await rate_limiter.reserve_cost(
                user_id, file_cost
            )
            if reserve_error:
                await claude_progress_msg.edit_text(
                    f"⏱️ {escape_html(reserve_error)}", parse_mode="HTML"
                )
                return

        # Process with Claude
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
            )

            # Update session ID
            context.user_data["claude_session_id"] = claude_response.session_id

            # Settled in the finally below; a run flagged is_error produced
            # nothing usable and is not charged.
            if not claude_response.is_error:
                actual_cost = claude_response.cost

            # Same central persistence as the text path: without it the upload's
            # message pair, tool usage and cost row never reach storage.
            await persist_interaction(
                context.bot_data.get("storage"), user_id, prompt, claude_response
            )

            # Check if Claude changed the working directory and update our tracking
            _update_working_directory_from_claude_response(
                claude_response, context, settings, user_id
            )

            # Format and send response
            from ..utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            # Delete progress message
            await claude_progress_msg.delete()

            # Send responses
            for i, message in enumerate(formatted_messages):
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=message.reply_markup,
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
                )

                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

            # Log successful file processing (only after responses are sent —
            # a Claude failure must not be audited as a success).
            if audit_logger:
                await audit_logger.log_file_access(
                    user_id=user_id,
                    file_path=document.file_name,
                    action="upload_processed",
                    success=True,
                    file_size=document.file_size,
                )

        except Exception as e:
            await claude_progress_msg.edit_text(
                _format_error_message(e), parse_mode="HTML"
            )
            logger.error("Claude file processing failed", error=str(e), user_id=user_id)

            # Audit the failed Claude processing.
            if audit_logger:
                await audit_logger.log_file_access(
                    user_id=user_id,
                    file_path=document.file_name,
                    action="upload_failed",
                    success=False,
                    file_size=document.file_size,
                )
        finally:
            # Always release the hold; a failed or empty run settles at 0.0.
            if rate_limiter and reservation_id:
                await rate_limiter.settle_reservation(reservation_id, actual_cost)

    except Exception as e:
        if progress_msg:
            try:
                await progress_msg.delete()
            except Exception as delete_error:
                logger.debug(
                    "Failed to delete progress message", error=str(delete_error)
                )

        error_msg = f"❌ <b>Error processing file</b>\n\n{escape_html(str(e))}"
        await update.message.reply_text(error_msg, parse_mode="HTML")

        # Log failed file processing
        if audit_logger:
            await audit_logger.log_file_access(
                user_id=user_id,
                file_path=document.file_name,
                action="upload_failed",
                success=False,
                file_size=document.file_size,
            )

        logger.error("Error processing document", error=str(e), user_id=user_id)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo uploads."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]

    # Check if enhanced image handler is available
    features = context.bot_data.get("features")
    image_handler = features.get_image_handler() if features else None

    if image_handler:
        try:
            # Send processing indicator
            progress_msg = await update.message.reply_text(
                "📸 Processing image...", parse_mode="HTML"
            )

            # Get the largest photo size
            photo = update.message.photo[-1]

            # Process image with enhanced handler
            processed_image = await image_handler.process_image(
                photo, update.message.caption
            )

            # Delete progress message
            await progress_msg.delete()

            # Create Claude progress message
            claude_progress_msg = await update.message.reply_text(
                "🤖 Analyzing image with Claude...", parse_mode="HTML"
            )

            # Get Claude integration
            claude_integration = context.bot_data.get("claude_integration")

            if not claude_integration:
                await claude_progress_msg.edit_text(
                    "❌ <b>Claude integration not available</b>\n\n"
                    "The Claude Code integration is not properly configured.",
                    parse_mode="HTML",
                )
                return

            # Get current directory and session
            current_dir = context.user_data.get(
                "current_directory", settings.approved_directory
            )
            session_id = context.user_data.get("claude_session_id")

            # Hold budget for this run; released in the finally below on every
            # path so a failure or empty result costs the user nothing.
            rate_limiter: Optional[RateLimiter] = context.bot_data.get("rate_limiter")
            reservation_id: Optional[str] = None
            actual_cost = 0.0
            if rate_limiter:
                reservation_id, reserve_error = await rate_limiter.reserve_cost(
                    user_id, estimate_message_cost(update)
                )
                if reserve_error:
                    await claude_progress_msg.edit_text(
                        f"⏱️ {escape_html(reserve_error)}", parse_mode="HTML"
                    )
                    return

            # Process with Claude
            try:
                claude_response = await claude_integration.run_command(
                    prompt=processed_image.prompt,
                    working_directory=current_dir,
                    user_id=user_id,
                    session_id=session_id,
                )

                # Update session ID
                context.user_data["claude_session_id"] = claude_response.session_id

                # Settled in the finally below; a run flagged is_error produced
                # nothing usable and is not charged.
                if not claude_response.is_error:
                    actual_cost = claude_response.cost

                # Same central persistence as the text path.
                await persist_interaction(
                    context.bot_data.get("storage"),
                    user_id,
                    processed_image.prompt,
                    claude_response,
                )

                # Check if Claude changed the working directory and update tracking
                _update_working_directory_from_claude_response(
                    claude_response, context, settings, user_id
                )

                # Format and send response
                from ..utils.formatting import ResponseFormatter

                formatter = ResponseFormatter(settings)
                formatted_messages = formatter.format_claude_response(
                    claude_response.content
                )

                # Delete progress message
                await claude_progress_msg.delete()

                # Send responses
                for i, message in enumerate(formatted_messages):
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=message.reply_markup,
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )

                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)

            except Exception as e:
                await claude_progress_msg.edit_text(
                    _format_error_message(e), parse_mode="HTML"
                )
                logger.error(
                    "Claude image processing failed", error=str(e), user_id=user_id
                )
            finally:
                # Always release the hold; a failed or empty run settles at 0.0.
                if rate_limiter and reservation_id:
                    await rate_limiter.settle_reservation(reservation_id, actual_cost)

        except Exception as e:
            logger.error("Image processing failed", error=str(e), user_id=user_id)
            await update.message.reply_text(
                _format_error_message(e),
                parse_mode="HTML",
            )
    else:
        # Fall back to unsupported message
        await update.message.reply_text(
            "📸 <b>Photo Upload</b>\n\n"
            "Photo processing is not yet supported.\n\n"
            "<b>Currently supported:</b>\n"
            "• Text files (.py, .js, .md, etc.)\n"
            "• Configuration files\n"
            "• Documentation files\n\n"
            "<b>Coming soon:</b>\n"
            "• Image analysis\n"
            "• Screenshot processing\n"
            "• Diagram interpretation",
            parse_mode="HTML",
        )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice message uploads."""
    user_id = update.effective_user.id
    settings: Settings = context.bot_data["settings"]

    features = context.bot_data.get("features")
    voice_handler = features.get_voice_handler() if features else None

    if not voice_handler:
        await update.message.reply_text(
            "🎙️ <b>Voice Messages</b>\n\n"
            "Voice transcription is not available.\n"
            f"Provider: <code>{settings.voice_provider_display_name}</code>\n"
            f"Set <code>{settings.voice_provider_api_key_env}</code> to enable.\n"
            "Install optional voice deps with "
            '<code>pip install "claude-code-telegram[voice]"</code>.',
            parse_mode="HTML",
        )
        return

    try:
        progress_msg = await update.message.reply_text(
            "🎙️ Transcribing voice message...", parse_mode="HTML"
        )

        voice = update.message.voice
        processed_voice = await voice_handler.process_voice_message(
            voice, update.message.caption
        )

        await progress_msg.edit_text(
            "🤖 Processing transcription with Claude...", parse_mode="HTML"
        )

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "❌ <b>Claude integration not available</b>\n\n"
                "The Claude Code integration is not properly configured.",
                parse_mode="HTML",
            )
            return

        current_dir = context.user_data.get(
            "current_directory", settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")

        # Hold budget for this run; released in the finally below on every path
        # so a failure or empty result costs the user nothing.
        rate_limiter: Optional[RateLimiter] = context.bot_data.get("rate_limiter")
        reservation_id: Optional[str] = None
        actual_cost = 0.0
        if rate_limiter:
            reservation_id, reserve_error = await rate_limiter.reserve_cost(
                user_id, estimate_message_cost(update)
            )
            if reserve_error:
                await progress_msg.edit_text(
                    f"⏱️ {escape_html(reserve_error)}", parse_mode="HTML"
                )
                return

        try:
            # Keep classic mode aligned with handle_photo: single progress message,
            # no streaming callback or typing heartbeat.
            claude_response = await claude_integration.run_command(
                prompt=processed_voice.prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
            )

            context.user_data["claude_session_id"] = claude_response.session_id

            # Settled in the finally below; a run flagged is_error produced
            # nothing usable and is not charged.
            if not claude_response.is_error:
                actual_cost = claude_response.cost

            # Same central persistence as the text path.
            await persist_interaction(
                context.bot_data.get("storage"),
                user_id,
                processed_voice.prompt,
                claude_response,
            )

            _update_working_directory_from_claude_response(
                claude_response, context, settings, user_id
            )

            from ..utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            await progress_msg.delete()

            for i, message in enumerate(formatted_messages):
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=message.reply_markup,
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

        except Exception as e:
            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude voice processing failed", error=str(e), user_id=user_id
            )
        finally:
            # Always release the hold; a failed or empty run settles at 0.0.
            if rate_limiter and reservation_id:
                await rate_limiter.settle_reservation(reservation_id, actual_cost)

    except Exception as e:
        logger.error("Voice processing failed", error=str(e), user_id=user_id)
        await update.message.reply_text(
            _format_error_message(e),
            parse_mode="HTML",
        )


def _estimate_file_processing_cost(file_size: int) -> float:
    """Estimate cost for processing uploaded file."""
    # Base cost for file handling
    base_cost = 0.005

    # Additional cost based on file size (per KB)
    size_cost = (file_size / 1024) * 0.0001

    return base_cost + size_cost


def _update_working_directory_from_claude_response(
    claude_response, context, settings, user_id
):
    """Update the working directory based on Claude's response content."""
    import re
    from pathlib import Path

    # Look for directory changes in Claude's response.
    # NOTE: This is a best-effort heuristic to keep the bot's notion of cwd
    # in sync with what Claude told the user. It is not authoritative —
    # Claude's actual cwd is tracked by the SDK. The patterns are anchored
    # tightly to reduce false-positive parsing (e.g. words like "racd" or
    # "I'd love to acd" no longer match).
    # A path is either quoted (may contain spaces) or an unquoted run of
    # non-space characters.
    quoted_or_bare = "(\"[^\"\\n\\r]+\"|'[^'\\n\\r]+'|`[^`\\n\\r]+`|\\S+)"
    patterns = [
        rf"(?:^|[\n\r])\s*cd\s+{quoted_or_bare}",  # cd command at start of a line
        rf"(?:^|[\n\r])(?:```|\$)\s*cd\s+{quoted_or_bare}",  # cd in a shell block
        rf"(?:^|[\n\r])\s*Changed directory to:?\s*{quoted_or_bare}",
        rf"(?:^|[\n\r])\s*Current directory:?\s*{quoted_or_bare}",
        rf"(?:^|[\n\r])\s*Working directory:?\s*{quoted_or_bare}",
    ]

    # Match against the original-case content — the patterns already use
    # IGNORECASE, and lowercasing would corrupt the extracted paths
    # (mixed-case dirs never resolve / .exists() fails).
    content = claude_response.content
    current_dir = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    for pattern in patterns:
        matches = re.findall(pattern, content, re.MULTILINE | re.IGNORECASE)
        for match in matches:
            try:
                # Clean up the path: drop surrounding quotes, then trailing
                # prose punctuation ("cd src." / "cd src, then ..."). The
                # lookbehind keeps dot-segments intact ("..", "../.."), and the
                # `or` guard restores the path if it was punctuation-only.
                raw = match.strip()
                if raw[:1] in ('"', "'", "`") and raw[-1:] == raw[:1]:
                    new_path = raw[1:-1]
                else:
                    new_path = re.sub(r"(?<![./])[.,;:!?]+$", "", raw) or raw

                # Handle relative paths
                if new_path.startswith("./") or new_path.startswith("../"):
                    new_path = (current_dir / new_path).resolve()
                elif not new_path.startswith("/"):
                    # Relative path without ./
                    new_path = (current_dir / new_path).resolve()
                else:
                    # Absolute path
                    new_path = Path(new_path).resolve()

                # Validate that the new path is within the approved directory.
                # Require a directory — Claude may also mention existing file
                # paths (e.g. "Working directory: .../file.txt"); storing a file
                # as current_directory breaks subsequent file operations.
                if (
                    new_path.is_relative_to(settings.approved_directory)
                    and new_path.is_dir()
                ):
                    context.user_data["current_directory"] = new_path
                    logger.info(
                        "Updated working directory from Claude response",
                        old_dir=str(current_dir),
                        new_dir=str(new_path),
                        user_id=user_id,
                    )
                    return  # Take the first valid match

            except (ValueError, OSError) as e:
                # Invalid path, skip this match
                logger.debug(
                    "Invalid path in Claude response", path=match, error=str(e)
                )
                continue
