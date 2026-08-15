"""Feature flag management."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .settings import Settings


class FeatureFlags:
    """Feature flag management system."""

    def __init__(self, settings: "Settings"):
        """Initialize with settings."""
        self.settings = settings

    @property
    def mcp_enabled(self) -> bool:
        """Check if Model Context Protocol is enabled."""
        return self.settings.enable_mcp and self.settings.mcp_config_path is not None

    @property
    def git_enabled(self) -> bool:
        """Check if Git integration is enabled."""
        return self.settings.enable_git_integration

    @property
    def file_uploads_enabled(self) -> bool:
        """Check if file uploads are enabled."""
        return self.settings.enable_file_uploads

    @property
    def quick_actions_enabled(self) -> bool:
        """Check if quick action buttons are enabled."""
        return self.settings.enable_quick_actions

    @property
    def telemetry_enabled(self) -> bool:
        """Check if telemetry is enabled."""
        return self.settings.enable_telemetry

    @property
    def webhook_enabled(self) -> bool:
        """Check if webhook mode is enabled."""
        return self.settings.webhook_url is not None

    @property
    def development_features_enabled(self) -> bool:
        """Check if development features are enabled."""
        return self.settings.development_mode

    @property
    def api_server_enabled(self) -> bool:
        """Check if the webhook API server is enabled."""
        return self.settings.enable_api_server

    @property
    def scheduler_enabled(self) -> bool:
        """Check if the job scheduler is enabled."""
        return self.settings.enable_scheduler

    @property
    def agentic_mode_enabled(self) -> bool:
        """Check if agentic conversational mode is enabled."""
        return self.settings.agentic_mode

    @property
    def voice_messages_enabled(self) -> bool:
        """Check if voice message transcription is enabled."""
        if not self.settings.enable_voice_messages:
            return False
        if self.settings.voice_provider == "openai":
            return (
                self.settings.openai_api_key is not None
                or self.settings.voice_base_url is not None
            )
        return self.settings.mistral_api_key is not None

    @property
    def stream_drafts_enabled(self) -> bool:
        """Check if streaming drafts via sendMessageDraft is enabled."""
        return self.settings.enable_stream_drafts

    def _feature_map(self) -> dict[str, bool]:
        """Single source of truth for feature name -> enabled state."""
        return {
            "mcp": self.mcp_enabled,
            "git": self.git_enabled,
            "file_uploads": self.file_uploads_enabled,
            "quick_actions": self.quick_actions_enabled,
            "telemetry": self.telemetry_enabled,
            "webhook": self.webhook_enabled,
            "development": self.development_features_enabled,
            "api_server": self.api_server_enabled,
            "scheduler": self.scheduler_enabled,
            "agentic_mode": self.agentic_mode_enabled,
            "voice_messages": self.voice_messages_enabled,
            "stream_drafts": self.stream_drafts_enabled,
        }

    def is_feature_enabled(self, feature_name: str) -> bool:
        """Generic feature check by name."""
        return self._feature_map().get(feature_name, False)

    def get_enabled_features(self) -> list[str]:
        """Get list of all enabled features."""
        return [name for name, enabled in self._feature_map().items() if enabled]
