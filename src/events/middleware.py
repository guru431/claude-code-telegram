"""Audit logging for webhook events on the bus.

Log-only. This module holds no enforcement gate, and its name says so: the
previous ``EventSecurityMiddleware`` promised one it never had — it took a
``SecurityValidator`` and an ``AuthenticationManager``, used neither, and wrote a
single info line. SECURITY.md read that name and claimed webhook events were
"validated before handler processing", so an operator believed a control was on
that did not exist.
"""

import structlog

from .bus import Event, EventBus
from .types import WebhookEvent

logger = structlog.get_logger()


class WebhookAuditLogger:
    """Records webhook events reaching the bus. Not an enforcement gate.

    Nothing here can veto a run even in principle: ``EventBus._dispatch``
    dispatches matching handlers concurrently with ``return_exceptions=True``, so
    a raised error would not stop ``AgentHandler``. Webhook payloads also carry no
    user identity or filesystem path there would be anything to authenticate or
    validate. The real enforcement sits upstream and downstream of this log line:

    - API layer: GitHub HMAC-SHA256 signature / Bearer-token verification and
      atomic deduplication before an event is ever published.
    - Agent layer: webhook-driven runs use a read-only tool set
      (``_WEBHOOK_READONLY_TOOLS`` in ``events.handlers``), and every tool call is
      gated by the SDK ``can_use_tool`` callback and
      ``check_bash_directory_boundary``.

    If a bus source ever carries validatable fields, give this class the
    dependencies it needs *then* — holding unused ones "for the future" is what
    made the gap invisible.
    """

    def __init__(self, event_bus: EventBus) -> None:
        self.event_bus = event_bus

    def register(self) -> None:
        """Subscribe to webhook events for audit logging.

        Note that ``ScheduledEvent`` is not covered: scheduled jobs originate
        from persisted local config rather than an external caller.
        """
        self.event_bus.subscribe(WebhookEvent, self.log_webhook)

    async def log_webhook(self, event: Event) -> None:
        """Write the audit line for one webhook event."""
        if not isinstance(event, WebhookEvent):
            return

        logger.info(
            "Webhook event passed to bus",
            provider=event.provider,
            event_type=event.event_type_name,
            delivery_id=event.delivery_id,
        )
