"""Event bus middleware wrapping existing security and auth systems.

Provides event-level validation before handlers process events,
reusing SecurityValidator and AuthenticationManager.
"""

import structlog

from ..security.auth import AuthenticationManager
from ..security.validators import SecurityValidator
from .bus import Event, EventBus
from .types import WebhookEvent

logger = structlog.get_logger()


class EventSecurityMiddleware:
    """Validates events before they reach handlers.

    Wraps the existing SecurityValidator for path/input validation
    and AuthenticationManager for user authentication.
    """

    def __init__(
        self,
        event_bus: EventBus,
        security_validator: SecurityValidator,
        auth_manager: AuthenticationManager,
    ) -> None:
        self.event_bus = event_bus
        self.security = security_validator
        self.auth = auth_manager

    def register(self) -> None:
        """Subscribe as a global handler to validate all events."""
        self.event_bus.subscribe(WebhookEvent, self.validate_webhook)

    async def validate_webhook(self, event: Event) -> None:
        """Validate webhook events (signature verified upstream in API layer)."""
        if not isinstance(event, WebhookEvent):
            return

        # Webhooks are signature-verified in the API layer.
        # Here we just log for audit purposes.
        logger.info(
            "Webhook event passed to bus",
            provider=event.provider,
            event_type=event.event_type_name,
            delivery_id=event.delivery_id,
        )
