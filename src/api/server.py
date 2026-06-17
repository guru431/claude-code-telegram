"""FastAPI webhook server.

Runs in the same process as the bot, sharing the event loop.
Receives external webhooks and publishes them as events on the bus.
"""

import json
import uuid
from typing import Any, Dict, Optional

import structlog
from fastapi import FastAPI, Header, HTTPException, Request

from ..config.settings import Settings
from ..events.bus import EventBus
from ..events.types import WebhookEvent
from ..storage.database import DatabaseManager
from .auth import verify_github_signature, verify_shared_secret

logger = structlog.get_logger()


def create_api_app(
    event_bus: EventBus,
    settings: Settings,
    db_manager: Optional[DatabaseManager] = None,
) -> FastAPI:
    """Create the FastAPI application."""

    app = FastAPI(
        title="Claude Code Telegram - Webhook API",
        version="0.1.0",
        docs_url="/docs" if settings.development_mode else None,
        redoc_url=None,
    )

    @app.get("/health")
    async def health_check() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/{provider}")
    async def receive_webhook(
        provider: str,
        request: Request,
        x_hub_signature_256: Optional[str] = Header(None),
        x_github_event: Optional[str] = Header(None),
        x_github_delivery: Optional[str] = Header(None),
        authorization: Optional[str] = Header(None),
    ) -> Dict[str, str]:
        """Receive and validate webhook from an external provider."""
        body = await request.body()

        # Verify signature based on provider
        if provider == "github":
            secret = settings.github_webhook_secret
            if not secret:
                raise HTTPException(
                    status_code=500,
                    detail="GitHub webhook secret not configured",
                )
            if not verify_github_signature(body, x_hub_signature_256, secret):
                logger.warning(
                    "GitHub webhook signature verification failed",
                    delivery_id=x_github_delivery,
                )
                raise HTTPException(status_code=401, detail="Invalid signature")

            event_type_name = x_github_event or "unknown"
            delivery_id = x_github_delivery or str(uuid.uuid4())

            # Only act on configured GitHub event types. 'ping' (sent on
            # webhook setup) and any non-allowlisted type are acknowledged
            # without running an agent, before dedup-record/publish.
            if (
                event_type_name == "ping"
                or event_type_name not in settings.github_webhook_events
            ):
                logger.info(
                    "Ignoring GitHub webhook event type",
                    event_type=event_type_name,
                    delivery_id=delivery_id,
                )
                return {"status": "ignored", "event": event_type_name}
        else:
            # Generic provider — require auth (fail-closed)
            secret = settings.webhook_api_secret
            if not secret:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "Webhook API secret not configured. "
                        "Set WEBHOOK_API_SECRET to accept "
                        "webhooks from this provider."
                    ),
                )
            if not verify_shared_secret(authorization, secret):
                raise HTTPException(status_code=401, detail="Invalid authorization")
            event_type_name = request.headers.get("X-Event-Type", "unknown")
            delivery_id = request.headers.get("X-Delivery-ID", str(uuid.uuid4()))

        # Parse JSON payload from the already-buffered body so we do not
        # re-read the request stream after signature verification.
        try:
            payload: Dict[str, Any] = json.loads(body)
        except Exception:
            payload = {"raw_body": body.decode("utf-8", errors="replace")[:5000]}

        # Atomic dedupe: attempt INSERT first, only publish if new
        if db_manager and delivery_id:
            is_new = await _try_record_webhook(
                db_manager,
                event_id=str(uuid.uuid4()),
                provider=provider,
                event_type=event_type_name,
                delivery_id=delivery_id,
                payload=payload,
            )
            if not is_new:
                logger.info(
                    "Duplicate webhook delivery ignored",
                    provider=provider,
                    delivery_id=delivery_id,
                )
                return {
                    "status": "duplicate",
                    "delivery_id": delivery_id,
                }

        # Publish event to the bus
        event = WebhookEvent(
            provider=provider,
            event_type_name=event_type_name,
            payload=payload,
            delivery_id=delivery_id,
        )

        await event_bus.publish(event)

        logger.info(
            "Webhook received and published",
            provider=provider,
            event_type=event_type_name,
            delivery_id=delivery_id,
            event_id=event.id,
        )

        return {"status": "accepted", "event_id": event.id}

    return app


async def _try_record_webhook(
    db_manager: DatabaseManager,
    event_id: str,
    provider: str,
    event_type: str,
    delivery_id: str,
    payload: Dict[str, Any],
) -> bool:
    """Atomically insert a webhook event, returning whether it was new.

    Uses INSERT OR IGNORE on the unique delivery_id column.
    If the row already exists the insert is a no-op and changes() == 0.
    Returns True if the event is new (inserted), False if duplicate.

    The row is recorded with ``processed=0``; the agent handler flips it to 1
    only after a successful run, so a failed/empty run stays durable as 0.
    """
    async with db_manager.get_connection() as conn:
        await conn.execute(
            """
            INSERT OR IGNORE INTO webhook_events
            (event_id, provider, event_type, delivery_id, payload,
             processed)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (
                event_id,
                provider,
                event_type,
                delivery_id,
                json.dumps(payload),
            ),
        )
        cursor = await conn.execute("SELECT changes()")
        row = await cursor.fetchone()
        inserted = row[0] > 0 if row else False
        await conn.commit()
        return inserted


async def _replay_webhook_rows(rows: Any, event_bus: EventBus) -> int:
    """Re-publish a set of webhook_events rows to the bus. Returns the count."""
    replayed = 0
    for row in rows:
        row_dict = dict(row)
        try:
            raw = row_dict.get("payload")
            payload: Dict[str, Any] = json.loads(raw) if raw else {}
        except Exception:
            payload = {}
        event = WebhookEvent(
            provider=row_dict["provider"],
            event_type_name=row_dict["event_type"],
            payload=payload,
            delivery_id=row_dict.get("delivery_id") or "",
        )
        await event_bus.publish(event)
        replayed += 1
    return replayed


async def recover_unprocessed_webhooks(
    db_manager: DatabaseManager, event_bus: EventBus
) -> int:
    """Startup recovery: replay every still-pending (``processed=0``) delivery.

    On a hard crash a delivery can be persisted yet never run, and the provider
    blocks re-delivery as a duplicate. Dead-lettered rows (``processed=2``) and
    completed rows (``processed=1``) are excluded. The agent handler flips the
    row to 1 on success or bumps ``attempts``/dead-letters it on failure.
    """
    try:
        async with db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT provider, event_type, delivery_id, payload "
                "FROM webhook_events WHERE processed = 0 ORDER BY received_at"
            )
            rows = list(await cursor.fetchall())
    except Exception:
        logger.exception("Failed to load unprocessed webhook events for recovery")
        return 0

    replayed = await _replay_webhook_rows(rows, event_bus)
    if replayed:
        logger.info("Replayed unprocessed webhook events on startup", count=replayed)
    return replayed


async def retry_pending_webhooks(
    db_manager: DatabaseManager, event_bus: EventBus, base_delay_seconds: int = 60
) -> int:
    """Periodic retry sweep: replay pending deliveries whose backoff has elapsed.

    Backoff is exponential per attempt (``base_delay_seconds * 2**attempts``).
    Rows that exhausted their retry budget are already dead-lettered
    (``processed=2``) and excluded; ``processed=0`` rows never attempted yet
    (``last_attempt_at IS NULL``) are eligible immediately.
    """
    try:
        async with db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT provider, event_type, delivery_id, payload "
                "FROM webhook_events "
                "WHERE processed = 0 AND ("
                "  last_attempt_at IS NULL OR "
                "  datetime(last_attempt_at) <= "
                "  datetime('now', '-' || (? * (1 << attempts)) || ' seconds')"
                ") ORDER BY received_at",
                (base_delay_seconds,),
            )
            rows = list(await cursor.fetchall())
    except Exception:
        logger.exception("Failed to load webhook events for retry")
        return 0

    replayed = await _replay_webhook_rows(rows, event_bus)
    if replayed:
        logger.info("Retried pending webhook events", count=replayed)
    return replayed


async def run_api_server(
    event_bus: EventBus,
    settings: Settings,
    db_manager: Optional[DatabaseManager] = None,
) -> None:
    """Run the FastAPI server using uvicorn."""
    import uvicorn

    app = create_api_app(event_bus, settings, db_manager)

    config = uvicorn.Config(
        app=app,
        host=settings.api_server_host,
        port=settings.api_server_port,
        log_level="info" if not settings.debug else "debug",
    )
    server = uvicorn.Server(config)
    await server.serve()
