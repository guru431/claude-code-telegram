"""Tests for database management."""

import tempfile
from pathlib import Path

import pytest

from src.exceptions import DatabaseConnectionError
from src.storage.database import DatabaseManager


@pytest.fixture
async def db_manager():
    """Create test database manager."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test.db"
        manager = DatabaseManager(f"sqlite:///{db_path}")
        await manager.initialize()
        yield manager
        await manager.close()


class TestDatabaseManager:
    """Test database manager functionality."""

    async def test_initialization(self, db_manager):
        """Test database initialization."""
        # Database should be initialized
        assert await db_manager.health_check()

    async def test_connection_pool(self, db_manager):
        """Test connection pooling."""
        # Should be able to get multiple connections
        async with db_manager.get_connection() as conn1:
            async with db_manager.get_connection() as conn2:
                # Both connections should work
                await conn1.execute("SELECT 1")
                await conn2.execute("SELECT 1")

    async def test_schema_creation(self, db_manager):
        """Test that schema is created properly."""
        async with db_manager.get_connection() as conn:
            # Check that tables exist
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = [row[0] for row in await cursor.fetchall()]

            expected_tables = [
                "users",
                "sessions",
                "messages",
                "tool_usage",
                "audit_log",
                "user_tokens",
                "cost_tracking",
                "project_threads",
                "schema_version",
            ]

            for table in expected_tables:
                assert table in tables

    async def test_foreign_keys_enabled(self, db_manager):
        """Test that foreign keys are enabled."""
        async with db_manager.get_connection() as conn:
            cursor = await conn.execute("PRAGMA foreign_keys")
            result = await cursor.fetchone()
            assert result[0] == 1  # Foreign keys enabled

    async def test_indexes_created(self, db_manager):
        """Test that indexes are created."""
        async with db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name LIKE 'idx_%'"
            )
            indexes = [row[0] for row in await cursor.fetchall()]

            expected_indexes = [
                "idx_sessions_user_id",
                "idx_sessions_project_path",
                "idx_messages_session_id",
                "idx_messages_timestamp",
                "idx_audit_log_user_id",
                "idx_audit_log_timestamp",
                "idx_cost_tracking_user_date",
                "idx_project_threads_chat_active",
                "idx_project_threads_slug",
            ]

            for index in expected_indexes:
                assert index in indexes

    async def test_migration_tracking(self, db_manager):
        """Test that migrations are tracked."""
        async with db_manager.get_connection() as conn:
            cursor = await conn.execute("SELECT MAX(version) FROM schema_version")
            version = await cursor.fetchone()
            assert version[0] >= 1  # At least initial migration

    async def test_audit_log_has_risk_and_session_columns(self, db_manager):
        """Migration 9 adds risk_level/session_id to audit_log."""
        async with db_manager.get_connection() as conn:
            cursor = await conn.execute("PRAGMA table_info(audit_log)")
            columns = {row[1] for row in await cursor.fetchall()}

        assert "risk_level" in columns
        assert "session_id" in columns


class TestDatabaseClose:
    """close() must be terminal: no silent reopen after shutdown."""

    async def test_get_connection_raises_after_close(self, db_manager):
        """A closed manager refuses to hand out (or lazily open) connections."""
        await db_manager.close()

        with pytest.raises(DatabaseConnectionError):
            async with db_manager.get_connection():
                pass  # pragma: no cover — must not be reached

        # The pool must not have been repopulated by the attempt.
        assert db_manager._connection_pool == []

    async def test_health_check_false_after_close(self, db_manager):
        """health_check reports False instead of reopening the database."""
        assert await db_manager.health_check()

        await db_manager.close()

        assert await db_manager.health_check() is False

    async def test_close_is_idempotent(self, db_manager):
        """Closing twice is a no-op, not an error."""
        await db_manager.close()
        await db_manager.close()

        assert db_manager._closed is True


class TestMigrationAtomicity:
    """A migration's DDL and its schema_version row must land together.

    Regression: executescript() performs no transaction control, so committing
    the version row separately let a crash in between persist the schema without
    its version. The next start reran migration 1 (bare CREATE TABLE) and died
    with "table users already exists".
    """

    async def test_failing_migration_leaves_no_partial_schema(self, tmp_path):
        db_path = tmp_path / "atomic.db"
        manager = DatabaseManager(f"sqlite:///{db_path}")

        real_migrations = manager._get_migrations()
        broken = [
            real_migrations[0],
            (2, "CREATE TABLE marker (id INTEGER);\nSELECT this_is_not_valid_sql();"),
        ]
        manager._get_migrations = lambda: broken  # type: ignore[method-assign]

        with pytest.raises(Exception):
            await manager.initialize()

        # Migration 1 committed with its version row; migration 2 rolled back
        # whole, so its table is absent and its version was never recorded.
        import aiosqlite

        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute("SELECT MAX(version) FROM schema_version")
            assert (await cursor.fetchone())[0] == 1
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='marker'"
            )
            assert await cursor.fetchone() is None

    async def test_rerun_after_failure_completes(self, tmp_path):
        """The half-migrated database must still be startable once fixed."""
        db_path = tmp_path / "recover.db"

        manager = DatabaseManager(f"sqlite:///{db_path}")
        real_migrations = manager._get_migrations()
        manager._get_migrations = lambda: [  # type: ignore[method-assign]
            real_migrations[0],
            (2, "SELECT this_is_not_valid_sql();"),
        ]
        with pytest.raises(Exception):
            await manager.initialize()
        await manager.close()

        # Same database, real migration list: no "table users already exists".
        recovered = DatabaseManager(f"sqlite:///{db_path}")
        await recovered.initialize()
        assert await recovered.health_check()
        await recovered.close()


class TestInMemoryDatabase:
    """``sqlite:///:memory:`` is documented and used by TestingConfig.

    A bare ``:memory:`` database is private to the connection that opened it, so
    migrations ran on a throwaway connection and the five pooled connections each
    opened their own empty database — the first query failed with "no such
    table" and nothing was shared between connections.
    """

    async def test_schema_visible_from_pooled_connection(self):
        manager = DatabaseManager("sqlite:///:memory:")
        await manager.initialize()
        try:
            async with manager.get_connection() as conn:
                cursor = await conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                tables = {row[0] for row in await cursor.fetchall()}
            assert "users" in tables
            assert "sessions" in tables
        finally:
            await manager.close()

    async def test_writes_are_shared_between_connections(self):
        manager = DatabaseManager("sqlite:///:memory:")
        await manager.initialize()
        try:
            async with manager.get_connection() as conn:
                await conn.execute(
                    "INSERT INTO users (user_id, telegram_username) VALUES (7, 'me')"
                )
                await conn.commit()
            async with manager.get_connection() as other:
                cursor = await other.execute(
                    "SELECT telegram_username FROM users WHERE user_id = 7"
                )
                row = await cursor.fetchone()
            assert row is not None and row[0] == "me"
        finally:
            await manager.close()

    async def test_two_managers_do_not_share_memory(self):
        """Each manager gets its own in-memory database, not a global one."""
        first = DatabaseManager("sqlite:///:memory:")
        second = DatabaseManager("sqlite:///:memory:")
        await first.initialize()
        await second.initialize()
        try:
            async with first.get_connection() as conn:
                await conn.execute("INSERT INTO users (user_id) VALUES (1)")
                await conn.commit()
            async with second.get_connection() as conn:
                cursor = await conn.execute("SELECT COUNT(*) FROM users")
                assert (await cursor.fetchone())[0] == 0
        finally:
            await first.close()
            await second.close()
