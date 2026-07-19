"""Tests for authentication system."""

from datetime import UTC, datetime, timedelta

import pytest

from src.exceptions import SecurityError
from src.security.auth import (
    AuthenticationManager,
    UserSession,
    WhitelistAuthProvider,
)


class TestUserSession:
    """Test UserSession functionality."""

    def test_session_creation(self):
        """Test session creation."""
        session = UserSession(
            user_id=123,
            auth_provider="TestProvider",
            created_at=datetime.now(UTC),
            last_activity=datetime.now(UTC),
        )

        assert session.user_id == 123
        assert session.auth_provider == "TestProvider"
        assert not session.is_expired()

    def test_session_expiry(self):
        """Test session expiry logic."""
        old_time = datetime.now(UTC) - timedelta(hours=25)
        session = UserSession(
            user_id=123,
            auth_provider="TestProvider",
            created_at=old_time,
            last_activity=old_time,
        )

        assert session.is_expired()

    def test_session_refresh(self):
        """Test session refresh."""
        old_time = datetime.now(UTC) - timedelta(hours=1)
        session = UserSession(
            user_id=123,
            auth_provider="TestProvider",
            created_at=old_time,
            last_activity=old_time,
        )

        session.refresh()
        assert not session.is_expired()
        assert session.last_activity > old_time


class TestWhitelistAuthProvider:
    """Test whitelist authentication provider."""

    async def test_allowed_user_authentication(self):
        """Test authentication of allowed user."""
        provider = WhitelistAuthProvider([123, 456])

        # Test allowed user
        result = await provider.authenticate(123, {})
        assert result is True

        # Test non-allowed user
        result = await provider.authenticate(789, {})
        assert result is False

    async def test_get_user_info(self):
        """Test user info retrieval."""
        provider = WhitelistAuthProvider([123])

        # Allowed user
        info = await provider.get_user_info(123)
        assert info is not None
        assert info["user_id"] == 123
        assert info["auth_type"] == "whitelist"

        # Non-allowed user
        info = await provider.get_user_info(456)
        assert info is None


class TestAuthenticationManager:
    """Test authentication manager."""

    @pytest.fixture
    def auth_manager(self):
        return AuthenticationManager([WhitelistAuthProvider([123, 456])])

    def test_manager_requires_providers(self):
        """Test that manager requires at least one provider."""
        with pytest.raises(SecurityError):
            AuthenticationManager([])

    async def test_whitelist_authentication(self, auth_manager):
        """Test authentication through whitelist."""
        # Allowed user should authenticate
        result = await auth_manager.authenticate_user(123)
        assert result is True
        assert auth_manager.is_authenticated(123)

        # Non-allowed user should fail
        result = await auth_manager.authenticate_user(999)
        assert result is False
        assert not auth_manager.is_authenticated(999)

    async def test_session_management(self, auth_manager):
        """Test session creation and management."""
        user_id = 123

        # Authenticate user
        await auth_manager.authenticate_user(user_id)

        # Should have session
        session = auth_manager.get_session(user_id)
        assert session is not None
        assert session.user_id == user_id

        # Refresh session
        old_activity = session.last_activity
        result = auth_manager.refresh_session(user_id)
        assert result is True
        assert session.last_activity > old_activity

        # End session
        auth_manager.end_session(user_id)
        assert not auth_manager.is_authenticated(user_id)

    async def test_expired_session_cleanup(self, auth_manager):
        """Test cleanup of expired sessions."""
        user_id = 123

        # Authenticate user
        await auth_manager.authenticate_user(user_id)

        # Manually expire session
        session = auth_manager.get_session(user_id)
        session.last_activity = datetime.now(UTC) - timedelta(hours=25)

        # Should no longer be authenticated
        assert not auth_manager.is_authenticated(user_id)
        assert auth_manager.get_session(user_id) is None

    async def test_session_info(self, auth_manager):
        """Test session information retrieval."""
        user_id = 123

        # No session initially
        info = auth_manager.get_session_info(user_id)
        assert info is None

        # Authenticate and get info
        await auth_manager.authenticate_user(user_id)
        info = auth_manager.get_session_info(user_id)

        assert info is not None
        assert info["user_id"] == user_id
        assert "created_at" in info
        assert "last_activity" in info
        assert info["is_expired"] is False

    async def test_active_sessions_count(self, auth_manager):
        """Test active sessions counting."""
        assert auth_manager.get_active_sessions_count() == 0

        # Authenticate two users
        await auth_manager.authenticate_user(123)
        await auth_manager.authenticate_user(456)

        assert auth_manager.get_active_sessions_count() == 2

        # End one session
        auth_manager.end_session(123)
        assert auth_manager.get_active_sessions_count() == 1
