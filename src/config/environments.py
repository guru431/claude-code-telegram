"""Environment-specific configuration overrides."""

from typing import Any, Dict


def _annotated_fields(cls: type) -> Dict[str, Any]:
    """Return ``{name: value}`` for each annotated field on *cls*.

    Driving the dict from ``__annotations__`` means subclasses can add
    type-annotated overrides without explicit values being filtered out
    by ``cls.__dict__`` lookups, and annotations-only fields (without a
    default value) are still picked up via ``getattr`` if present on a
    parent class.
    """
    return {
        name: getattr(cls, name) for name in cls.__annotations__ if hasattr(cls, name)
    }


class DevelopmentConfig:
    """Development environment overrides."""

    debug: bool = True
    development_mode: bool = True
    log_level: str = "DEBUG"
    rate_limit_requests: int = 100  # More lenient for testing
    claude_timeout_seconds: int = 600  # Longer timeout for debugging
    enable_telemetry: bool = False

    @classmethod
    def as_dict(cls) -> Dict[str, Any]:
        """Return config as dictionary."""
        return _annotated_fields(cls)


class TestingConfig:
    """Testing environment configuration."""

    debug: bool = True
    development_mode: bool = True
    database_url: str = "sqlite:///:memory:"
    approved_directory: str = "/tmp/test_projects"
    enable_telemetry: bool = False
    claude_timeout_seconds: int = 30  # Faster timeout for tests
    rate_limit_requests: int = 1000  # No rate limiting in tests
    session_timeout_hours: int = 1  # Short session timeout for testing

    @classmethod
    def as_dict(cls) -> Dict[str, Any]:
        """Return config as dictionary."""
        return _annotated_fields(cls)


class ProductionConfig:
    """Production environment configuration."""

    debug: bool = False
    development_mode: bool = False
    log_level: str = "INFO"
    enable_telemetry: bool = True
    # Use stricter defaults for production
    claude_max_cost_per_user: float = 5.0  # Lower cost limit
    claude_max_cost_per_request: float = 2.0  # Per-request SDK cap
    rate_limit_requests: int = 5  # Stricter rate limiting
    session_timeout_hours: int = 12  # Shorter session timeout

    @classmethod
    def as_dict(cls) -> Dict[str, Any]:
        """Return config as dictionary."""
        return _annotated_fields(cls)
