"""Event bus audit middleware.

Logs bus events for audit purposes. This is log-only and not an
enforcement gate -- see ``EventSecurityMiddleware`` for why, and for where
the real signature/tool-level enforcement lives.
"""

import structlog

from ..security.auth import AuthenticationManager
from ..security.validators import SecurityValidator
from .bus import Event, EventBus
from .types import WebhookEvent

logger = structlog.get_logger()


class EventSecurityMiddleware:
    """Audit logger for bus events; not an enforcement gate.

    This is deliberately log-only. Webhook payloads carry no user identity
    or filesystem path to authenticate or validate, so ``SecurityValidator``
    and ``AuthenticationManager`` have nothing meaningful to check here.
    The bus also dispatches all matching handlers concurrently with
    ``return_exceptions=True`` (see ``EventBus._dispatch``), so even a raised
    error would not veto ``AgentHandler``. Actual enforcement lives upstream
    and downstream of this audit point:

    - API layer: GitHub HMAC-SHA256 signature / Bearer-token verification
      and atomic deduplication before an event is ever published.
    - Agent layer: webhook-driven runs use a read-only tool set
      (``_WEBHOOK_READONLY_TOOLS`` in ``events.handlers``), and tool calls
      are gated by the SDK ``can_use_tool`` callback and
      ``check_bash_directory_boundary``.

    ``security`` and ``auth`` are retained for future per-user/path events
    (e.g. authenticated bus sources) that would carry validatable fields.
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
