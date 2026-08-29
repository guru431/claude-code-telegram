"""FastAPI webhook server.

Runs in the same process as the bot, sharing the event loop.
Receives external webhooks and publishes them as events on the bus.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Dict, Optional

import structlog
from fastapi import FastAPI, Header, HTTPException, Request

from ..config.settings import Settings
from ..events.bus import EventBus
from ..events.types import WebhookEvent
from ..storage.database import DatabaseManager
from .auth import verify_github_signature, verify_shared_secret

logger = structlog.get_logger()

# Reject webhook bodies larger than this before buffering them into memory.
# The body is read in full (needed for signature verification) before auth
# runs, so an unauthenticated caller could otherwise force an OOM. GitHub caps
# webhook payloads at 25 MB; anything larger is treated as abuse.
_MAX_WEBHOOK_BODY_BYTES = 25 * 1024 * 1024


def create_api_app(
    event_bus: EventBus,
    settings: Settings,
    db_manager: Optional[DatabaseManager] = None,
) -> FastAPI:
    """Create the FastAPI application."""

    app = FastAPI(
        title="Claude Code Telegram - Webhook API",
        version="0.1.0",
        # Opt-in explicitly rather than riding on development_mode: the docs page
        # enumerates the webhook surface and should not appear just because
        # ENVIRONMENT happened to be left unset or set to development.
        docs_url="/docs" if settings.api_docs_enabled else None,
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
        # Reject oversized payloads before buffering them into memory. Check the
        # declared Content-Length first for a cheap reject, then cap the actual
        # stream to defend against a missing/spoofed length (chunked transfer).
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid Content-Length")
            if declared > _MAX_WEBHOOK_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Payload too large")

        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > _MAX_WEBHOOK_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Payload too large")

        # Verify signature based on provider
        if provider == "github":
            github_secret = settings.github_webhook_secret
            if not github_secret:
                raise HTTPException(
                    status_code=500,
                    detail="GitHub webhook secret not configured",
                )
            if not verify_github_signature(
                body, x_hub_signature_256, github_secret.get_secret_value()
            ):
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
            api_secret = settings.webhook_api_secret
            if not api_secret:
                # Log the actionable config detail server-side only; the client
                # gets a generic message so the env-var name is not leaked for
                # reconnaissance.
                logger.error(
                    "Generic webhook rejected: WEBHOOK_API_SECRET not configured",
                    provider=provider,
                )
                raise HTTPException(
                    status_code=500,
                    detail="Webhook endpoint not configured",
                )
            if not verify_shared_secret(authorization, api_secret.get_secret_value()):
                raise HTTPException(status_code=401, detail="Invalid authorization")
            event_type_name = request.headers.get("X-Event-Type", "unknown")
            delivery_id = request.headers.get("X-Delivery-ID")
            if not delivery_id:
                # No stable delivery id from the provider: derive one
                # deterministically from provider + raw body so an at-least-once
                # retry of the same payload collides on the dedup key instead of
                # generating a fresh random id that would defeat deduplication.
                delivery_id = hashlib.sha256(
                    provider.encode("utf-8") + b"\0" + body
                ).hexdigest()

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


async def _claim_and_replay(
    db_manager: DatabaseManager,
    event_bus: EventBus,
    where_extra: str,
    params: tuple[Any, ...],
) -> int:
    """Atomically claim eligible pending deliveries and re-publish them.

    Both startup recovery and the periodic sweep funnel through here so a row is
    only ever replayed by one caller at a time. The claim stamps
    ``last_attempt_at`` in the SAME ``UPDATE ... RETURNING`` that selects the
    rows, so a concurrent claim (recovery racing the first sweep, or two sweeps)
    sees the just-stamped timestamp and skips the row within its backoff window
    instead of replaying the same in-flight delivery twice.

    ``where_extra`` is appended to the ``processed = 0`` predicate; ``params``
    are bound to it plus the claim timestamp. Returns the number replayed.
    """
    now = datetime.now(UTC).isoformat()
    try:
        async with db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "UPDATE webhook_events SET last_attempt_at = ? "
                "WHERE processed = 0" + where_extra + " "
                "RETURNING provider, event_type, delivery_id, payload",
                (now, *params),
            )
            rows = list(await cursor.fetchall())
            await conn.commit()
    except Exception:
        logger.exception("Failed to claim webhook events for replay")
        return 0

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
    completed rows (``processed=1``) are excluded. Each claimed row is stamped
    in-flight (``last_attempt_at``) before publishing so the first retry sweep
    does not immediately replay it again; the agent handler flips the row to 1
    on success or bumps ``attempts``/dead-letters it on failure.
    """
    replayed = await _claim_and_replay(db_manager, event_bus, "", ())
    if replayed:
        logger.info("Replayed unprocessed webhook events on startup", count=replayed)
    return replayed


async def retry_pending_webhooks(
    db_manager: DatabaseManager,
    event_bus: EventBus,
    base_delay_seconds: int = 60,
    in_flight_grace_seconds: int = 900,
) -> int:
    """Periodic retry sweep: replay pending deliveries whose backoff has elapsed.

    Backoff is exponential per attempt (``base_delay_seconds * 2**attempts``).
    Rows that exhausted their retry budget are already dead-lettered
    (``processed=2``) and excluded. A ``processed=0`` row never attempted yet
    (``last_attempt_at IS NULL``) is normally still in flight — the initial
    publish runs the agent as a background task for minutes — so it becomes
    eligible only once its ``received_at`` is older than
    ``in_flight_grace_seconds`` (i.e. the run was orphaned, not merely slow);
    replaying it sooner would spawn a second concurrent run for the same
    delivery. The claim stamps ``last_attempt_at`` atomically, so an in-flight
    row recovered at startup or by a prior sweep is not re-replayed until its
    backoff window elapses.
    """
    where_extra = (
        " AND ("
        "  (last_attempt_at IS NULL AND"
        "   datetime(received_at) <= datetime('now', '-' || ? || ' seconds')) OR"
        "  (last_attempt_at IS NOT NULL AND"
        "   datetime(last_attempt_at) <="
        "   datetime('now', '-' || (? * (1 << attempts)) || ' seconds'))"
        ")"
    )
    replayed = await _claim_and_replay(
        db_manager,
        event_bus,
        where_extra,
        (in_flight_grace_seconds, base_delay_seconds),
    )
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
