"""Tests for the webhook API server."""

import hashlib
import hmac
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from src.api.server import (
    _try_record_webhook,
    create_api_app,
    recover_unprocessed_webhooks,
    retry_pending_webhooks,
)
from src.events.bus import EventBus
from src.events.types import WebhookEvent
from src.storage.facade import Storage


def make_settings(**overrides):  # type: ignore[no-untyped-def]
    """Create a minimal mock settings object."""
    from unittest.mock import MagicMock

    settings = MagicMock()
    settings.development_mode = True
    settings.github_webhook_secret = overrides.get("github_webhook_secret", "gh-secret")
    settings.webhook_api_secret = overrides.get(
        "webhook_api_secret", "default-api-secret"
    )
    settings.api_server_port = 8080
    settings.api_server_host = "127.0.0.1"
    settings.github_webhook_events = overrides.get(
        "github_webhook_events", ["issues", "pull_request", "release"]
    )
    settings.debug = False
    return settings


class TestWebhookAPI:
    """Tests for the FastAPI webhook endpoints."""

    def test_health_check(self) -> None:
        bus = EventBus()
        app = create_api_app(bus, make_settings())
        client = TestClient(app)

        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_github_webhook_valid_signature(self) -> None:
        """Valid GitHub webhook is accepted and event published."""
        bus = EventBus()
        secret = "gh-secret"
        settings = make_settings(github_webhook_secret=secret)
        app = create_api_app(bus, settings)
        client = TestClient(app)

        payload = b'{"action": "opened", "number": 1}'
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        response = client.post(
            "/webhooks/github",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "del-123",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "accepted"
        assert "event_id" in data

    def test_github_webhook_invalid_signature(self) -> None:
        """Invalid GitHub signature returns 401."""
        bus = EventBus()
        app = create_api_app(bus, make_settings())
        client = TestClient(app)

        response = client.post(
            "/webhooks/github",
            content=b'{"test": true}',
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=invalid",
                "X-GitHub-Event": "push",
            },
        )

        assert response.status_code == 401

    def test_generic_webhook_no_secret_configured_rejected(self) -> None:
        """Generic webhooks without configured secret return 500."""
        bus = EventBus()
        settings = make_settings(webhook_api_secret=None)
        app = create_api_app(bus, settings)
        client = TestClient(app)

        response = client.post(
            "/webhooks/custom",
            json={"event": "test"},
            headers={"X-Event-Type": "test.event"},
        )

        assert response.status_code == 500

    def test_generic_webhook_with_auth(self) -> None:
        """Generic webhooks with configured secret require Bearer token."""
        bus = EventBus()
        settings = make_settings(webhook_api_secret="my-api-secret")
        app = create_api_app(bus, settings)
        client = TestClient(app)

        # Without auth
        response = client.post(
            "/webhooks/custom",
            json={"data": "test"},
        )
        assert response.status_code == 401

        # With valid auth
        response = client.post(
            "/webhooks/custom",
            json={"data": "test"},
            headers={"Authorization": "Bearer my-api-secret"},
        )
        assert response.status_code == 200

    def test_github_webhook_no_secret_configured(self) -> None:
        """GitHub webhook without configured secret returns 500."""
        bus = EventBus()
        settings = make_settings(github_webhook_secret=None)
        app = create_api_app(bus, settings)
        client = TestClient(app)

        response = client.post(
            "/webhooks/github",
            json={"test": True},
            headers={"X-GitHub-Event": "push"},
        )

        assert response.status_code == 500

    def test_generic_webhook_wrong_token_rejected(self) -> None:
        """Generic webhook with wrong Bearer token returns 401."""
        bus = EventBus()
        settings = make_settings(webhook_api_secret="correct-secret")
        app = create_api_app(bus, settings)
        client = TestClient(app)

        response = client.post(
            "/webhooks/custom",
            json={"data": "test"},
            headers={"Authorization": "Bearer wrong-secret"},
        )

        assert response.status_code == 401


async def test_recover_unprocessed_webhooks_replays_then_idempotent(tmp_path) -> None:
    """processed=0 rows are replayed once; after processed=1 nothing replays."""
    storage = Storage(f"sqlite:///{tmp_path / 'wh.db'}")
    await storage.initialize()
    db = storage.db_manager

    await _try_record_webhook(
        db,
        event_id="e1",
        provider="github",
        event_type="issues",
        delivery_id="d1",
        payload={"action": "opened"},
    )

    bus = AsyncMock()
    replayed = await recover_unprocessed_webhooks(db, bus)

    assert replayed == 1
    bus.publish.assert_awaited_once()
    event = bus.publish.await_args.args[0]
    assert isinstance(event, WebhookEvent)
    assert event.provider == "github"
    assert event.delivery_id == "d1"
    assert event.payload == {"action": "opened"}

    # Once marked processed, recovery is a no-op (idempotent across restarts).
    async with db.get_connection() as conn:
        await conn.execute(
            "UPDATE webhook_events SET processed = 1 WHERE delivery_id = 'd1'"
        )
        await conn.commit()

    bus.publish.reset_mock()
    assert await recover_unprocessed_webhooks(db, bus) == 0
    bus.publish.assert_not_awaited()

    await storage.close()


async def test_webhook_dead_letter_after_max_attempts(tmp_path) -> None:
    """A webhook is dead-lettered (processed=2) once retry attempts are spent."""
    from pathlib import Path
    from unittest.mock import MagicMock

    from src.events.handlers import AgentHandler

    storage = Storage(f"sqlite:///{tmp_path / 'wh.db'}")
    await storage.initialize()
    db = storage.db_manager
    await _try_record_webhook(
        db,
        event_id="e1",
        provider="github",
        event_type="issues",
        delivery_id="d1",
        payload={},
    )

    handler = AgentHandler(
        event_bus=MagicMock(),
        claude_integration=MagicMock(),
        default_working_directory=Path(tmp_path),
        db_manager=db,
    )

    assert await handler._mark_webhook_failed("d1", "boom") == (1, False)
    assert await handler._mark_webhook_failed("d1", "boom") == (2, False)
    assert await handler._mark_webhook_failed("d1", "boom") == (3, True)

    async with db.get_connection() as conn:
        cur = await conn.execute(
            "SELECT processed, attempts FROM webhook_events WHERE delivery_id='d1'"
        )
        row = dict(await cur.fetchone())
    assert row["processed"] == 2
    assert row["attempts"] == 3

    await storage.close()


async def test_retry_sweep_excludes_dead_letter(tmp_path) -> None:
    """retry_pending_webhooks replays pending rows and skips dead-lettered ones."""
    storage = Storage(f"sqlite:///{tmp_path / 'wh.db'}")
    await storage.initialize()
    db = storage.db_manager
    await _try_record_webhook(
        db,
        event_id="e1",
        provider="github",
        event_type="issues",
        delivery_id="pending",
        payload={},
    )
    await _try_record_webhook(
        db,
        event_id="e2",
        provider="github",
        event_type="issues",
        delivery_id="dead",
        payload={},
    )
    async with db.get_connection() as conn:
        await conn.execute(
            "UPDATE webhook_events SET processed = 2, attempts = 3 "
            "WHERE delivery_id = 'dead'"
        )
        await conn.commit()

    bus = AsyncMock()
    # The pending row was never attempted (last_attempt_at IS NULL) so it is
    # eligible immediately; the dead-lettered row (processed=2) is skipped.
    replayed = await retry_pending_webhooks(db, bus, base_delay_seconds=60)

    assert replayed == 1
    bus.publish.assert_awaited_once()
    assert bus.publish.await_args.args[0].delivery_id == "pending"

    await storage.close()
