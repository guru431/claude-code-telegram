"""Security middleware for input validation and threat detection."""

import re
from typing import Any, Callable, Dict

import structlog

from ..utils.html_format import escape_html
from ..utils.upload_limits import exceeds_upload_limit

logger = structlog.get_logger()

# Command injection patterns. The backtick pattern is scoped to *dangerous*
# commands inside backticks (rm/curl/wget/etc.) so users can still discuss
# code snippets using inline code formatting. Compiled once at import time.
_DANGEROUS_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r";\s*rm\s+",
        r";\s*del\s+",
        r";\s*format\s+",
        r"`[^`]*\b(?:rm|curl|wget|chmod|chown|nc|bash|sh)\s+[^`]*`",
        r"\$\([^)]*\)",
        r"&&\s*rm\s+",
        r"\|\s*mail\s+",
        r">\s*/dev/",
        r"curl\s+.*\|\s*sh",
        r"wget\s+.*\|\s*sh",
        r"exec\s*\(",
        r"eval\s*\(",
    )
]

# Path traversal patterns. These intentionally require a command-like context
# (start of message, separator, or a cd/rm/cat-style command prefix) so
# legitimate references such as "see ../README.md" or "/etc/hosts is documented
# in ..." are not blocked. The authoritative path check still happens in
# ``SecurityValidator.validate_path`` for actual file operations. Matched
# case-sensitively (no re.IGNORECASE). Compiled once at import time.
_PATH_TRAVERSAL_PATTERNS = [
    re.compile(p)
    for p in (
        r"(?:^|[;&|]|\b(?:cd|cat|rm|mv|cp|ls|less|head|tail)\s+)\.\./",
        r"(?:^|[;&|]\s*)~/",
        r"(?:^|[;&|]|\b(?:cd|cat|rm|mv|cp|ls|less|head|tail)\s+)/(?:etc|var|usr|sys|proc)/",
    )
]

# Suspicious URLs or domains.
# Note: blanket TLD blocks (.ru/.tk/.ml) produce too many false positives for a
# Russian-speaking userbase and any project hosted on legitimate ccTLDs.
# Restrict to URL-shortener-style obfuscation and inline-script schemes that
# have no business use case here. Compiled once at import time.
_SUSPICIOUS_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"https?://bit\.ly/",
        r"https?://tinyurl\.com/",
        r"https?://t\.co/",
        r"https?://goo\.gl/",
        r"javascript:",
        r"data:text/html",
    )
]


async def security_middleware(
    handler: Callable, event: Any, data: Dict[str, Any]
) -> Any:
    """Validate inputs and detect security threats.

    This middleware:
    1. Validates message content for dangerous patterns
    2. Sanitizes file uploads
    3. Detects potential attacks
    4. Logs security violations
    """
    user_id = event.effective_user.id if event.effective_user else None
    username = (
        getattr(event.effective_user, "username", None)
        if event.effective_user
        else None
    )

    if not user_id:
        logger.warning("No user information in update")
        return await handler(event, data)

    # Get dependencies from context
    security_validator = data.get("security_validator")
    audit_logger = data.get("audit_logger")

    # In agentic mode, user text is a prompt to Claude — not a command.
    # Skip input validation so natural conversation (backticks, paths, etc.) works.
    settings = data.get("settings")
    agentic_mode = getattr(settings, "agentic_mode", False) if settings else False
    development_mode = (
        getattr(settings, "development_mode", False) if settings else False
    )

    if not security_validator:
        # In agentic mode input validation is intentionally skipped, so a
        # missing validator is harmless — preserve pass-through. In classic
        # mode, fail closed in production: without a validator we cannot
        # enforce the input checks, so refuse rather than silently allowing
        # unvalidated input through. Development keeps the lenient behavior.
        if not agentic_mode and not development_mode:
            logger.error(
                "Security validator not available in middleware context; "
                "blocking request (fail-closed, production)",
                user_id=user_id,
            )
            return  # Block processing (wrapper raises ApplicationHandlerStop)
        logger.error("Security validator not available in middleware context")
        # Agentic mode or development: continue without validation.
        return await handler(event, data)

    # For callback queries, ``effective_message`` is the bot's own message that
    # carries the inline keyboard, not user-supplied input — validating its text
    # would be both meaningless and a false-positive risk. Auth and rate-limit
    # checks still apply to callbacks; only content validation is skipped here.
    is_callback = getattr(event, "callback_query", None) is not None

    # Validate text content if present (classic mode only)
    message = event.effective_message
    if message and message.text and not agentic_mode and not is_callback:
        is_safe, violation_type = await validate_message_content(
            message.text, security_validator, user_id, audit_logger
        )
        if not is_safe:
            await message.reply_text(
                f"🛡️ <b>Security Alert</b>\n\n"
                f"Your message contains potentially dangerous content and has been blocked.\n"
                f"Violation: {escape_html(violation_type)}\n\n"
                "If you believe this is an error, please contact the administrator.",
                parse_mode="HTML",
            )
            return  # Block processing

    # Validate file uploads if present
    if message and message.document:
        is_safe, error_message = await validate_file_upload(
            message.document, security_validator, user_id, audit_logger, settings
        )
        if not is_safe:
            await message.reply_text(
                f"🛡️ <b>File Upload Blocked</b>\n\n"
                f"{escape_html(error_message)}\n\n"
                "Please ensure your file meets security requirements.",
                parse_mode="HTML",
            )
            return  # Block processing

    # Log successful security validation
    logger.debug(
        "Security validation passed",
        user_id=user_id,
        username=username,
        has_text=bool(message and message.text),
        has_document=bool(message and message.document),
    )

    # Continue to handler
    return await handler(event, data)


async def validate_message_content(
    text: str, security_validator: Any, user_id: int, audit_logger: Any
) -> tuple[bool, str]:
    """Validate message text content for security threats."""

    # Check for command injection patterns (compiled at module level).
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(text):
            if audit_logger:
                await audit_logger.log_security_violation(
                    user_id=user_id,
                    violation_type="command_injection_attempt",
                    details=f"Dangerous pattern detected: {pattern.pattern}",
                    severity="high",
                    attempted_action="message_send",
                )

            logger.warning(
                "Command injection attempt detected",
                user_id=user_id,
                pattern=pattern.pattern,
                text_preview=text[:100],
            )
            return False, "Command injection attempt"

    # Check for path traversal attempts (compiled at module level,
    # case-sensitive).
    for pattern in _PATH_TRAVERSAL_PATTERNS:
        if pattern.search(text):
            if audit_logger:
                await audit_logger.log_security_violation(
                    user_id=user_id,
                    violation_type="path_traversal_attempt",
                    details=f"Path traversal pattern detected: {pattern.pattern}",
                    severity="high",
                    attempted_action="message_send",
                )

            logger.warning(
                "Path traversal attempt detected",
                user_id=user_id,
                pattern=pattern.pattern,
                text_preview=text[:100],
            )
            return False, "Path traversal attempt"

    # Check for suspicious URLs or domains (compiled at module level).
    for pattern in _SUSPICIOUS_PATTERNS:
        if pattern.search(text):
            if audit_logger:
                await audit_logger.log_security_violation(
                    user_id=user_id,
                    violation_type="suspicious_url",
                    details=f"Suspicious URL pattern detected: {pattern.pattern}",
                    severity="medium",
                    attempted_action="message_send",
                )

            logger.warning(
                "Suspicious URL detected", user_id=user_id, pattern=pattern.pattern
            )
            return False, "Suspicious URL detected"

    # Sanitize content using security validator. Compare against the same
    # whitespace normalisation the sanitiser applies (" ".join(split())):
    # otherwise deeply indented code loses half its length to collapsed
    # whitespace alone and gets rejected as "dangerous characters".
    sanitized = security_validator.sanitize_command_input(text)
    baseline = " ".join(text.split())
    if len(sanitized) < len(baseline) * 0.5:  # More than 50% removed
        if audit_logger:
            await audit_logger.log_security_violation(
                user_id=user_id,
                violation_type="excessive_sanitization",
                details="More than 50% of content was dangerous",
                severity="medium",
                attempted_action="message_send",
            )

        logger.warning(
            "Excessive content sanitization required",
            user_id=user_id,
            original_length=len(baseline),
            sanitized_length=len(sanitized),
        )
        return False, "Content contains too many dangerous characters"

    return True, ""


async def validate_file_upload(
    document: Any,
    security_validator: Any,
    user_id: int,
    audit_logger: Any,
    settings: Any = None,
) -> tuple[bool, str]:
    """Validate file uploads for security."""

    filename = getattr(document, "file_name", "unknown")
    # Keep the declared size as-is (possibly None): an absent size is unknown,
    # not zero. The handlers re-check the real byte length after download.
    file_size = getattr(document, "file_size", None)
    mime_type = getattr(document, "mime_type", "unknown")

    # Validate filename
    is_valid, error_message = security_validator.validate_filename(filename)
    if not is_valid:
        if audit_logger:
            await audit_logger.log_security_violation(
                user_id=user_id,
                violation_type="dangerous_filename",
                details=f"Filename validation failed: {error_message}",
                severity="medium",
                attempted_action="file_upload",
            )

        logger.warning(
            "Dangerous filename detected",
            user_id=user_id,
            filename=filename,
            error=error_message,
        )
        return False, error_message

    # Check file size limits. Single source of truth is
    # Settings.max_file_upload_size_bytes (MAX_FILE_UPLOAD_SIZE_MB); the literal
    # is only a fallback for callers that have no Settings available.
    max_file_size = getattr(
        settings, "max_file_upload_size_bytes", 10 * 1024 * 1024  # 10MB
    )
    if exceeds_upload_limit(file_size, max_file_size):
        if audit_logger:
            await audit_logger.log_security_violation(
                user_id=user_id,
                violation_type="file_too_large",
                details=f"File size {file_size} exceeds limit {max_file_size}",
                severity="low",
                attempted_action="file_upload",
            )

        return False, f"File too large. Maximum size: {max_file_size // (1024*1024)}MB"

    # Check MIME type
    dangerous_mime_types = [
        "application/x-executable",
        "application/x-msdownload",
        "application/x-msdos-program",
        "application/x-dosexec",
        "application/x-winexe",
        "application/x-sh",
        "application/x-shellscript",
    ]

    if mime_type in dangerous_mime_types:
        if audit_logger:
            await audit_logger.log_security_violation(
                user_id=user_id,
                violation_type="dangerous_mime_type",
                details=f"Dangerous MIME type: {mime_type}",
                severity="high",
                attempted_action="file_upload",
            )

        logger.warning(
            "Dangerous MIME type detected",
            user_id=user_id,
            filename=filename,
            mime_type=mime_type,
        )
        return False, f"File type not allowed: {mime_type}"

    # Log successful file validation
    if audit_logger:
        await audit_logger.log_file_access(
            user_id=user_id,
            file_path=filename,
            action="upload_validated",
            success=True,
            file_size=file_size,
        )

    logger.info(
        "File upload validated",
        user_id=user_id,
        filename=filename,
        file_size=file_size,
        mime_type=mime_type,
    )

    return True, ""
