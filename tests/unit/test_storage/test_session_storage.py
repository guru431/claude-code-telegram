"""Regressions for SQLite-backed session storage."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.claude.session import ClaudeSession
from src.storage.database import DatabaseManager
from src.storage.session_storage import SQLiteSessionStorage


@pytest.fixture
async def session_storage(migrated_db):
    """Create session storage backed by a temporary file database."""
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = migrated_db(Path(temp_dir) / "test.db")
        db_manager = DatabaseManager(f"sqlite:///{db_path}")
        await db_manager.initialize()
        yield SQLiteSessionStorage(db_manager)
        await db_manager.close()


@pytest.fixture
def sample_session():
    """Create a sample session."""
    return ClaudeSession(
        session_id="sqlite-session",
        user_id=555001,
        project_path=Path("/test/path"),
        created_at=datetime.now(UTC),
        last_used=datetime.now(UTC),
    )


class TestSoftDeletedSessions:
    """A soft-deleted session must not be resurrectable by its stale id."""

    async def test_live_session_still_loads(self, session_storage, sample_session):
        """Auto-resume of an active session is unaffected by the is_active filter."""
        await session_storage.save_session(sample_session)

        loaded = await session_storage.load_session("sqlite-session", user_id=555001)

        assert loaded is not None
        assert loaded.session_id == "sqlite-session"
        assert loaded.user_id == 555001

    async def test_deleted_session_does_not_load(self, session_storage, sample_session):
        """delete_session (is_active = FALSE) makes the session unloadable."""
        await session_storage.save_session(sample_session)
        await session_storage.delete_session("sqlite-session")

        assert (
            await session_storage.load_session("sqlite-session", user_id=555001)
        ) is None
        # Consistent with the listings, which already filtered on is_active.
        assert await session_storage.get_user_sessions(555001) == []

    async def test_expired_session_does_not_load(self, session_storage, sample_session):
        """A session retired by cleanup is likewise not resurrected."""
        await session_storage.save_session(sample_session)

        async with session_storage.db_manager.get_connection() as conn:
            await conn.execute(
                "UPDATE sessions SET last_used = datetime('now', '-48 hours') "
                "WHERE session_id = ?",
                ("sqlite-session",),
            )
            await conn.commit()

        assert await session_storage.cleanup_expired_sessions(timeout_hours=24) == 1
        assert (
            await session_storage.load_session("sqlite-session", user_id=555001)
        ) is None


class TestUserSessionCounter:
    """users.session_count must track the production session-write path.

    Regression: only Storage.create_session incremented the counter, and nothing
    in the running bot calls it — SessionManager persists through
    SQLiteSessionStorage.save_session, which never touched it. Every deployment
    reported session_count = 0 no matter how many sessions existed.
    """

    async def _session_count(self, session_storage, user_id: int) -> int:
        async with session_storage.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT session_count FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cursor.fetchone()
            return row[0]

    async def test_new_session_increments_counter(
        self, session_storage, sample_session
    ):
        await session_storage.save_session(sample_session)

        assert await self._session_count(session_storage, 555001) == 1

    async def test_resaving_the_same_session_does_not_increment(
        self, session_storage, sample_session
    ):
        await session_storage.save_session(sample_session)
        sample_session.message_count = 5
        await session_storage.save_session(sample_session)

        assert await self._session_count(session_storage, 555001) == 1

    async def test_each_distinct_session_counts_once(
        self, session_storage, sample_session
    ):
        await session_storage.save_session(sample_session)
        second = ClaudeSession(
            session_id="sqlite-session-2",
            user_id=555001,
            project_path=Path("/test/other"),
            created_at=datetime.now(UTC),
            last_used=datetime.now(UTC),
        )
        await session_storage.save_session(second)

        assert await self._session_count(session_storage, 555001) == 2
