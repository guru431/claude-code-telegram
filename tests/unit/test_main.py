"""Tests for application wiring in ``src.main``."""

import pytest

from src.config.settings import Settings
from src.exceptions import ConfigurationError
from src.main import _build_auth_providers
from src.security.auth import TokenAuthProvider


def test_token_provider_secret_is_hashable(tmp_path):
    """The token provider must receive a plain str secret, not a SecretStr.

    Regression: ``create_application`` passed ``config.auth_token_secret``
    (a ``SecretStr``) into ``TokenAuthProvider``, whose ``_hash_token`` calls
    ``self.secret.encode()`` -> ``AttributeError`` at runtime. The wiring must
    use ``config.auth_secret_str``.
    """
    test_dir = tmp_path / "projects"
    test_dir.mkdir()

    config = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(test_dir),
        allowed_users="123",
        enable_token_auth=True,
        development_mode=True,
        auth_token_secret="super-secret",
    )

    providers = _build_auth_providers(config)
    token_providers = [p for p in providers if isinstance(p, TokenAuthProvider)]
    assert token_providers, "expected a TokenAuthProvider when token auth is enabled"

    provider = token_providers[0]
    assert isinstance(provider.secret, str)
    # Must not raise AttributeError (the original bug).
    assert isinstance(provider._hash_token("a-token"), str)


def test_token_auth_is_fail_closed_in_production(tmp_path):
    """ENABLE_TOKEN_AUTH without DEVELOPMENT_MODE must refuse to start.

    In-memory token storage loses tokens on restart; enabling it in production
    is rejected so the fail-closed posture isn't silently weakened.
    """
    test_dir = tmp_path / "projects"
    test_dir.mkdir()

    config = Settings(
        telegram_bot_token="test_token",
        telegram_bot_username="test_bot",
        approved_directory=str(test_dir),
        allowed_users="123",
        enable_token_auth=True,
        development_mode=False,
        auth_token_secret="super-secret",
    )

    with pytest.raises(ConfigurationError):
        _build_auth_providers(config)
