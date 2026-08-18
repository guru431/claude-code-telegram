"""Foreign-key integrity regressions for audit logging and retention cleanup."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.security.audit import AuditEvent, AuditLogger, SQLiteAuditStorage
from src.storage.facade import Storage


@pytest.fixture
async def storage(migrated_db):
    """Create test storage backed by a temporary file database."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = migrated_db(Path(temp_dir) / "test.db")
        storage = Storage(f"sqlite:///{db_path}")
        await storage.initialize()
        yield storage
        await storage.close()


class TestAuditLogUnregisteredSubject:
    """audit_log must accept subjects that are not (yet) registered users."""

    async def test_store_event_for_unknown_user_on_fresh_db(self, storage):
        """Fresh DB + first update: audit row is written before the user row."""
        audit_storage = SQLiteAuditStorage(storage)
        event = AuditEvent(
            timestamp=datetime.now(UTC),
            user_id=999001,
            event_type="auth_attempt",
            success=True,
            details={"method": "automatic", "reason": "message_received"},
        )

        await audit_storage.store_event(event)

        events = await audit_storage.get_events(user_id=999001)
        assert len(events) == 1
        assert events[0].event_type == "auth_attempt"

        # The audit write must not have created a users row.
        assert await storage.users.get_user(999001) is None

    async def test_auth_attempt_logging_does_not_raise(self, storage):
        """The auth middleware path (log_auth_attempt) must not raise."""
        audit_logger = AuditLogger(SQLiteAuditStorage(storage))

        await audit_logger.log_auth_attempt(
            user_id=999002,
            success=False,
            method="automatic",
            reason="message_received",
        )

        events = await audit_logger.storage.get_events(user_id=999002)
        assert len(events) == 1
        assert events[0].success is False

    async def test_store_event_failure_does_not_break_auth(self, storage, monkeypatch):
        """A storage-layer failure is logged, not propagated to the caller."""

        async def boom(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(storage.audit, "log_event", boom)
        audit_logger = AuditLogger(SQLiteAuditStorage(storage))

        # Must not raise.
        await audit_logger.log_auth_attempt(
            user_id=999003, success=True, method="automatic"
        )


class TestAuditLogFkMigration:
    """Migration 8 rebuilds audit_log without the users FK, preserving rows."""

    async def test_existing_database_is_migrated(self):
        """A v7 database with the old FK migrates, keeping data and indexes."""
        import aiosqlite

        from src.storage.database import DatabaseManager

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "legacy.db"

            # Build the pre-migration state: schema through version 7.
            legacy = DatabaseManager(f"sqlite:///{db_path}")
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute("PRAGMA foreign_keys = ON")
                for version, migration in legacy._get_migrations():
                    if version >= 8:
                        break
                    if version == 1:
                        # Reinstate the FK the initial schema used to carry.
                        migration = migration.replace(
                            "ip_address TEXT\n);",
                            "ip_address TEXT,\n"
                            "    FOREIGN KEY (user_id) REFERENCES users(user_id)\n);",
                        )
                    await conn.executescript(migration)
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_version "
                    "(version INTEGER PRIMARY KEY)"
                )
                await conn.execute("INSERT INTO schema_version (version) VALUES (7)")
                await conn.execute(
                    "INSERT INTO users (user_id, telegram_username) VALUES (?, ?)",
                    (42, "legacy"),
                )
                await conn.execute(
                    "INSERT INTO audit_log (user_id, event_type, success) "
                    "VALUES (?, ?, ?)",
                    (42, "auth_attempt", True),
                )
                await conn.commit()

                # Sanity: the legacy schema really does reject unknown users.
                with pytest.raises(aiosqlite.IntegrityError):
                    await conn.execute(
                        "INSERT INTO audit_log (user_id, event_type, success) "
                        "VALUES (?, ?, ?)",
                        (777, "auth_attempt", True),
                    )
                await conn.rollback()

            storage = Storage(f"sqlite:///{db_path}")
            await storage.initialize()
            try:
                # Existing rows survived the rebuild.
                async with storage.db_manager.get_connection() as conn:
                    cursor = await conn.execute(
                        "SELECT user_id, event_type FROM audit_log"
                    )
                    rows = await cursor.fetchall()
                    assert [(r[0], r[1]) for r in rows] == [(42, "auth_attempt")]

                    # The FK is gone.
                    cursor = await conn.execute("PRAGMA foreign_key_list(audit_log)")
                    assert await cursor.fetchall() == []

                    # Indexes were recreated on the new table.
                    cursor = await conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'index' AND tbl_name = 'audit_log'"
                    )
                    names = {r[0] for r in await cursor.fetchall()}
                    assert {
                        "idx_audit_log_user_id",
                        "idx_audit_log_timestamp",
                        "idx_audit_log_ts_expr",
                    } <= names

                # An unregistered subject is now accepted.
                audit_storage = SQLiteAuditStorage(storage)
                await audit_storage.store_event(
                    AuditEvent(
                        timestamp=datetime.now(UTC),
                        user_id=777,
                        event_type="auth_attempt",
                        success=False,
                        details={},
                    )
                )
                assert len(await audit_storage.get_events(user_id=777)) == 1
            finally:
                await storage.close()


class TestRetentionCleanupOrdering:
    """Retention purge must delete child rows before their parents."""

    async def _make_old_message_with_tool_usage(self, storage) -> int:
        await storage.get_or_create_user(999010, "retentionuser")
        await storage.create_session(999010, "/test/retention", "retention-session")

        async with storage.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO messages (session_id, user_id, timestamp, prompt, response)
                VALUES (?, ?, datetime('now', '-120 days'), ?, ?)
                """,
                ("retention-session", 999010, "old prompt", "old response"),
            )
            message_id = cursor.lastrowid
            # Child row is NEWER than its parent: an age-based purge of
            # tool_usage alone would not remove it.
            await conn.execute(
                """
                INSERT INTO tool_usage (session_id, message_id, tool_name, timestamp)
                VALUES (?, ?, ?, datetime('now', '-1 days'))
                """,
                ("retention-session", message_id, "Read"),
            )
            await conn.commit()
        return message_id

    async def test_cleanup_purges_children_before_parents(self, storage):
        """Old message with a live child tool_usage row: cleanup completes."""
        await self._make_old_message_with_tool_usage(storage)

        result = await storage.cleanup_old_data(days=30, audit_days=365)

        assert result["messages_purged"] == 1
        assert result["tool_usage_purged"] == 1

        async with storage.db_manager.get_connection() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM messages")
            assert (await cursor.fetchone())[0] == 0
            cursor = await conn.execute("SELECT COUNT(*) FROM tool_usage")
            assert (await cursor.fetchone())[0] == 0

    async def test_cleanup_is_idempotent(self, storage):
        """A second run over the same window is a no-op, not an error."""
        await self._make_old_message_with_tool_usage(storage)

        await storage.cleanup_old_data(days=30, audit_days=365)
        second = await storage.cleanup_old_data(days=30, audit_days=365)

        assert second["messages_purged"] == 0
        assert second["tool_usage_purged"] == 0

    async def test_cleanup_keeps_recent_rows(self, storage):
        """Rows inside the retention window survive the purge."""
        await storage.get_or_create_user(999011, "recentuser")
        await storage.create_session(999011, "/test/recent", "recent-session")

        async with storage.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO messages (session_id, user_id, prompt, response)
                VALUES (?, ?, ?, ?)
                """,
                ("recent-session", 999011, "new prompt", "new response"),
            )
            await conn.execute(
                """
                INSERT INTO tool_usage (session_id, message_id, tool_name)
                VALUES (?, ?, ?)
                """,
                ("recent-session", cursor.lastrowid, "Read"),
            )
            await conn.commit()

        result = await storage.cleanup_old_data(days=30, audit_days=365)

        assert result["messages_purged"] == 0
        assert result["tool_usage_purged"] == 0
        async with storage.db_manager.get_connection() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM messages")
            assert (await cursor.fetchone())[0] == 1
            cursor = await conn.execute("SELECT COUNT(*) FROM tool_usage")
            assert (await cursor.fetchone())[0] == 1
