"""Tests for middleware handler stop behavior and bot-originated guards.

Verifies that when middleware rejects a request (auth failure, security
violation, rate limit exceeded), ApplicationHandlerStop is raised to
prevent subsequent handler groups from processing the update.

Regression tests for: https://github.com/RichardAtCT/claude-code-telegram/issues/44
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ApplicationHandlerStop

from src.bot.core import ClaudeCodeBot
from src.bot.middleware.rate_limit import estimate_message_cost
from src.config import create_test_config
from src.config.settings import Settings


@pytest.fixture
def mock_settings():
    """Minimal Settings mock for ClaudeCodeBot."""
    settings = MagicMock(spec=Settings)
    settings.telegram_token_str = "test:token"
    settings.webhook_url = None
    settings.agentic_mode = True
    settings.enable_quick_actions = False
    settings.enable_mcp = False
    settings.enable_git_integration = False
    settings.enable_file_uploads = False
    settings.enable_session_export = False
    settings.enable_image_uploads = False
    settings.enable_conversation_mode = False
    settings.enable_api_server = False
    settings.enable_scheduler = False
    settings.approved_directory = "/tmp/test"
    return settings


@pytest.fixture
def bot(mock_settings):
    """Create a ClaudeCodeBot instance with mock dependencies."""
    deps = {
        "auth_manager": MagicMock(),
        "security_validator": MagicMock(),
        "rate_limiter": MagicMock(),
        "audit_logger": MagicMock(),
        "storage": MagicMock(),
        "claude_integration": MagicMock(),
    }
    return ClaudeCodeBot(mock_settings, deps)


@pytest.fixture
def mock_update():
    """Create a mock Telegram Update with an unauthenticated user."""
    update = MagicMock()
    update.callback_query = None
    update.effective_user = MagicMock()
    update.effective_user.id = 999999
    update.effective_user.username = "attacker"
    update.effective_user.is_bot = False
    update.effective_message = MagicMock()
    update.effective_message.text = "hello"
    update.effective_message.document = None
    update.effective_message.photo = None
    update.effective_message.reply_text = AsyncMock()
    return update


@pytest.fixture
def mock_context():
    """Create a mock CallbackContext."""
    context = MagicMock()
    context.bot_data = {}
    return context


class TestMiddlewareBlocksSubsequentGroups:
    """Verify middleware rejection raises ApplicationHandlerStop."""

    async def test_auth_rejection_raises_handler_stop(
        self, bot, mock_update, mock_context
    ):
        """Auth middleware must raise ApplicationHandlerStop on rejection."""

        async def rejecting_auth(handler, event, data):
            await event.effective_message.reply_text("Not authorized")
            return

        wrapper = bot._create_middleware_handler(rejecting_auth)

        with pytest.raises(ApplicationHandlerStop):
            await wrapper(mock_update, mock_context)

    async def test_security_rejection_raises_handler_stop(
        self, bot, mock_update, mock_context
    ):
        """Security middleware must raise ApplicationHandlerStop on dangerous input."""

        async def rejecting_security(handler, event, data):
            await event.effective_message.reply_text("Blocked")
            return

        wrapper = bot._create_middleware_handler(rejecting_security)

        with pytest.raises(ApplicationHandlerStop):
            await wrapper(mock_update, mock_context)

    async def test_rate_limit_rejection_raises_handler_stop(
        self, bot, mock_update, mock_context
    ):
        """Rate limit middleware must raise ApplicationHandlerStop."""

        async def rejecting_rate_limit(handler, event, data):
            await event.effective_message.reply_text("Rate limited")
            return

        wrapper = bot._create_middleware_handler(rejecting_rate_limit)

        with pytest.raises(ApplicationHandlerStop):
            await wrapper(mock_update, mock_context)

    async def test_allowed_request_does_not_raise(self, bot, mock_update, mock_context):
        """Middleware that calls the handler must NOT raise ApplicationHandlerStop."""

        async def allowing_middleware(handler, event, data):
            return await handler(event, data)

        wrapper = bot._create_middleware_handler(allowing_middleware)
        await wrapper(mock_update, mock_context)

    async def test_real_auth_middleware_rejection(self, bot, mock_update, mock_context):
        """Integration test: actual auth_middleware rejects unauthorized user."""
        from src.bot.middleware.auth import auth_middleware

        auth_manager = MagicMock()
        auth_manager.is_authenticated.return_value = False
        auth_manager.authenticate_user = AsyncMock(return_value=False)
        bot.deps["auth_manager"] = auth_manager

        audit_logger = AsyncMock()
        bot.deps["audit_logger"] = audit_logger

        wrapper = bot._create_middleware_handler(auth_middleware)

        with pytest.raises(ApplicationHandlerStop):
            await wrapper(mock_update, mock_context)

        mock_update.effective_message.reply_text.assert_called_once()
        call_args = mock_update.effective_message.reply_text.call_args
        assert (
            "not authorized" in call_args[0][0].lower()
            or "Authentication" in call_args[0][0]
        )

    async def test_real_auth_middleware_allows_authenticated_user(
        self, bot, mock_update, mock_context
    ):
        """Integration test: auth_middleware allows an authenticated user through."""
        from src.bot.middleware.auth import auth_middleware

        auth_manager = MagicMock()
        auth_manager.is_authenticated.return_value = True
        auth_manager.refresh_session.return_value = True
        auth_manager.get_session.return_value = MagicMock(auth_provider="whitelist")
        bot.deps["auth_manager"] = auth_manager

        wrapper = bot._create_middleware_handler(auth_middleware)
        await wrapper(mock_update, mock_context)

    async def test_real_rate_limit_middleware_rejection(
        self, bot, mock_update, mock_context
    ):
        """Integration test: rate_limit_middleware rejects when limit exceeded."""
        from src.bot.middleware.rate_limit import rate_limit_middleware

        rate_limiter = MagicMock()
        rate_limiter.check_rate_limit = AsyncMock(
            return_value=(False, "Rate limit exceeded. Try again in 30s.")
        )
        bot.deps["rate_limiter"] = rate_limiter

        audit_logger = AsyncMock()
        bot.deps["audit_logger"] = audit_logger

        wrapper = bot._create_middleware_handler(rate_limit_middleware)

        with pytest.raises(ApplicationHandlerStop):
            await wrapper(mock_update, mock_context)

    async def test_rate_limit_middleware_lets_stop_callback_through(
        self, bot, mock_update, mock_context
    ):
        """stop: callbacks bypass the rate limiter without consuming tokens."""
        from src.bot.middleware.rate_limit import rate_limit_middleware

        rate_limiter = MagicMock()
        # Would reject if consulted -- the bypass must skip this entirely.
        rate_limiter.check_rate_limit = AsyncMock(
            return_value=(False, "Rate limit exceeded.")
        )
        bot.deps["rate_limiter"] = rate_limiter

        mock_update.callback_query = MagicMock()
        mock_update.callback_query.data = "stop:999999"

        handler_called = False

        async def handler(event, data):
            nonlocal handler_called
            handler_called = True

        await rate_limit_middleware(handler, mock_update, bot.deps)

        assert handler_called is True
        rate_limiter.check_rate_limit.assert_not_called()

    async def test_rejection_reply_map_evicts_stale_entries(
        self, bot, mock_update, mock_context
    ):
        """The rejection-throttle map must not grow without bound.

        A stream of distinct unauthorized senders adds one entry each; entries
        older than the throttle window are swept (the sweep itself is throttled
        to once per window, so it is forced here by rewinding its timestamp).
        """
        import time

        from src.bot.middleware import auth as auth_mod

        auth_manager = MagicMock()
        auth_manager.is_authenticated.return_value = False
        auth_manager.authenticate_user = AsyncMock(return_value=False)
        bot.deps["auth_manager"] = auth_manager
        bot.deps["audit_logger"] = AsyncMock()

        auth_mod._rejection_state.clear()
        # Seed entries that are already older than the window.
        stale_now = time.monotonic()
        for uid in range(1000, 1050):
            auth_mod._rejection_state[uid] = (
                stale_now - auth_mod._REJECTION_REPLY_WINDOW - 10,
                0,
            )
        # Force the throttled sweep to be due on the next rejection.
        auth_mod._last_rejection_gc = 0.0

        wrapper = bot._create_middleware_handler(auth_mod.auth_middleware)
        with pytest.raises(ApplicationHandlerStop):
            await wrapper(mock_update, mock_context)

        # All 50 stale entries swept; only the current rejecting user remains.
        assert set(auth_mod._rejection_state) == {mock_update.effective_user.id}

    async def test_rejection_reply_sweep_is_throttled(
        self, bot, mock_update, mock_context
    ):
        """Back-to-back rejections must not re-scan the whole map each time."""
        from src.bot.middleware import auth as auth_mod

        auth_manager = MagicMock()
        auth_manager.is_authenticated.return_value = False
        auth_manager.authenticate_user = AsyncMock(return_value=False)
        bot.deps["auth_manager"] = auth_manager
        bot.deps["audit_logger"] = AsyncMock()

        auth_mod._rejection_state.clear()
        wrapper = bot._create_middleware_handler(auth_mod.auth_middleware)

        with pytest.raises(ApplicationHandlerStop):
            await wrapper(mock_update, mock_context)
        gc_after_first = auth_mod._last_rejection_gc

        # A stale entry added after the sweep survives the next rejection,
        # proving the second call did not run another sweep.
        auth_mod._rejection_state[4242] = (0.0, 0)
        with pytest.raises(ApplicationHandlerStop):
            await wrapper(mock_update, mock_context)

        assert auth_mod._last_rejection_gc == gc_after_first
        assert 4242 in auth_mod._rejection_state

    async def test_dependencies_injected_before_middleware_runs(
        self, bot, mock_update, mock_context
    ):
        """Verify dependencies are available in bot_data when middleware executes."""
        captured_data = {}

        async def capturing_middleware(handler, event, data):
            captured_data.update(data)
            return await handler(event, data)

        wrapper = bot._create_middleware_handler(capturing_middleware)
        await wrapper(mock_update, mock_context)

        assert "auth_manager" in captured_data
        assert "security_validator" in captured_data
        assert "rate_limiter" in captured_data
        assert "settings" in captured_data


@pytest.mark.asyncio
async def test_middleware_wrapper_stops_bot_originated_updates() -> None:
    """Middleware wrapper should stop updates sent by bot users."""
    settings = create_test_config()
    claude_bot = ClaudeCodeBot(settings, {})

    middleware_called = False

    async def fake_middleware(handler, event, data):
        nonlocal middleware_called
        middleware_called = True
        return await handler(event, data)

    wrapper = claude_bot._create_middleware_handler(fake_middleware)

    update = MagicMock()
    update.effective_user = MagicMock(id=123, is_bot=True)
    context = MagicMock()
    context.bot_data = {}

    with pytest.raises(ApplicationHandlerStop):
        await wrapper(update, context)

    assert middleware_called is False


@pytest.mark.asyncio
async def test_middleware_wrapper_runs_for_non_bot_updates() -> None:
    """Middleware wrapper should execute middleware for user updates."""
    settings = create_test_config()
    claude_bot = ClaudeCodeBot(settings, {})

    middleware_called = False

    async def allowing_middleware(handler, event, data):
        nonlocal middleware_called
        middleware_called = True
        return await handler(event, data)

    wrapper = claude_bot._create_middleware_handler(allowing_middleware)

    update = MagicMock()
    update.effective_user = MagicMock(id=456, is_bot=False)
    context = MagicMock()
    context.bot_data = {}

    await wrapper(update, context)

    assert middleware_called is True


def test_estimate_message_cost_handles_none_text() -> None:
    """Cost estimation should not fail on service-like messages without text."""
    event = MagicMock()
    event.effective_message = MagicMock(text=None, document=None, photo=None)

    cost = estimate_message_cost(event)

    assert cost >= 0.01


class TestRejectionAuditIsAggregated:
    """One audit row per throttle window, not one per spam message.

    The rejection *reply* was already throttled, but ``log_auth_attempt`` ran
    before the throttle — so a stream of messages from an unauthorized sender to
    the bot's public @username produced an unbounded stream of identical INSERTs
    into ``audit_log``, a table pruned once a year.
    """

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        from src.bot.middleware import auth as auth_mod

        auth_mod._rejection_state.clear()
        yield
        auth_mod._rejection_state.clear()

    async def test_repeat_rejections_write_one_row_with_a_count(
        self, bot, mock_update, mock_context
    ):
        from src.bot.middleware import auth as auth_mod

        auth_manager = MagicMock()
        auth_manager.is_authenticated.return_value = False
        auth_manager.authenticate_user = AsyncMock(return_value=False)
        bot.deps["auth_manager"] = auth_manager
        audit = AsyncMock()
        bot.deps["audit_logger"] = audit

        wrapper = bot._create_middleware_handler(auth_mod.auth_middleware)
        for _ in range(5):
            with pytest.raises(ApplicationHandlerStop):
                await wrapper(mock_update, mock_context)

        assert audit.log_auth_attempt.await_count == 1

        # The window's suppressed attempts are not lost: they ride along on the
        # next window's row.
        user_id = mock_update.effective_user.id
        window_start, suppressed = auth_mod._rejection_state[user_id]
        auth_mod._rejection_state[user_id] = (
            window_start - auth_mod._REJECTION_REPLY_WINDOW - 1,
            suppressed,
        )
        with pytest.raises(ApplicationHandlerStop):
            await wrapper(mock_update, mock_context)

        assert audit.log_auth_attempt.await_count == 2
        assert audit.log_auth_attempt.await_args.kwargs["suppressed_attempts"] == 4

    async def test_successful_auth_records_the_username(
        self, bot, mock_update, mock_context
    ):
        """users.telegram_username was NULL in every deployment before this."""
        from src.bot.middleware import auth as auth_mod

        auth_mod._identity_recorded.clear()
        auth_manager = MagicMock()
        auth_manager.is_authenticated.return_value = False
        auth_manager.authenticate_user = AsyncMock(return_value=True)
        auth_manager.get_session.return_value = None
        bot.deps["auth_manager"] = auth_manager
        bot.deps["audit_logger"] = AsyncMock()
        storage = MagicMock()
        storage.users = AsyncMock()
        bot.deps["storage"] = storage
        mock_update.effective_user.username = "someone"

        wrapper = bot._create_middleware_handler(auth_mod.auth_middleware)
        await wrapper(mock_update, mock_context)

        storage.users.record_identity.assert_awaited_once_with(
            mock_update.effective_user.id, "someone"
        )
