"""Seeding helpers for storage tests.

``Storage.get_or_create_user`` and ``Storage.create_session`` used to live in
``src/storage/facade.py`` but were never called by the running bot — the real
write path is ``SQLiteSessionStorage.save_session``. They stayed alive only as
test fixtures while carrying their own, slightly different, ``session_count``
bookkeeping, which meant the suite exercised a path production never took. They
were deleted from production code and re-appear here as what they always were:
test setup.
"""

from datetime import UTC, datetime
from typing import Optional

from src.storage.models import SessionModel, UserModel


async def seed_user(storage, user_id: int, username: Optional[str] = None) -> UserModel:
    """Insert (or fetch) a user row for a test."""
    user = await storage.users.get_user(user_id)
    if user:
        return user
    now = datetime.now(UTC)
    user = UserModel(
        user_id=user_id,
        telegram_username=username,
        first_seen=now,
        last_active=now,
        is_allowed=False,
    )
    await storage.users.create_user(user)
    return await storage.users.get_user(user_id) or user


async def seed_session(
    storage, user_id: int, project_path: str, session_id: str
) -> SessionModel:
    """Insert a session row and bump the owner's session counter."""
    now = datetime.now(UTC)
    session = SessionModel(
        session_id=session_id,
        user_id=user_id,
        project_path=project_path,
        created_at=now,
        last_used=now,
    )
    async with storage.db_manager.get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO sessions
            (session_id, user_id, project_path, created_at, last_used)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session.session_id,
                session.user_id,
                session.project_path,
                session.created_at,
                session.last_used,
            ),
        )
        await conn.execute(
            "UPDATE users SET session_count = session_count + 1, last_active = ? "
            "WHERE user_id = ?",
            (now, user_id),
        )
        await conn.commit()
    return session
