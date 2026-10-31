"""Provider isolation and durable project preferences."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Dict, List, Optional

from ..claude.session import ClaudeSession, SessionStorage
from ..projects.registry import canonical_path_key
from ..storage.database import DatabaseManager

CODEX_PREFIX = "codex:"


def session_provider(session_id: str) -> str:
    """Old, unprefixed sessions always remain Claude sessions."""
    return "codex" if session_id.startswith(CODEX_PREFIX) else "claude"


class ProviderSessionStorage(SessionStorage):
    """Prevent one backend from listing, loading or evicting the other's sessions."""

    def __init__(self, storage: SessionStorage, provider: str):
        self.storage = storage
        self.provider = provider

    async def save_session(self, session: ClaudeSession) -> None:
        if session_provider(session.session_id) != self.provider:
            raise ValueError("Agent returned a session ID for a different provider")
        await self.storage.save_session(session)

    async def load_session(
        self, session_id: str, user_id: int
    ) -> Optional[ClaudeSession]:
        if session_provider(session_id) != self.provider:
            return None
        return await self.storage.load_session(session_id, user_id)

    async def delete_session(self, session_id: str) -> None:
        if session_provider(session_id) == self.provider:
            await self.storage.delete_session(session_id)

    async def get_user_sessions(self, user_id: int) -> List[ClaudeSession]:
        return [
            s
            for s in await self.storage.get_user_sessions(user_id)
            if session_provider(s.session_id) == self.provider
        ]

    async def get_all_sessions(self) -> List[ClaudeSession]:
        return [
            s
            for s in await self.storage.get_all_sessions()
            if session_provider(s.session_id) == self.provider
        ]

    async def session_known(self, session_id: str) -> bool:
        # A foreign-provider ID must never be treated as a new external session.
        return session_provider(session_id) != self.provider or (
            await self.storage.session_known(session_id)
        )


class AgentState:
    def __init__(self, db: DatabaseManager):
        self.db = db

    async def get_provider(self, user_id: int, directory: Path, default: str) -> str:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT provider FROM agent_preferences "
                "WHERE user_id = ? AND project_path = ?",
                (user_id, canonical_path_key(directory.resolve())),
            )
            row = await cursor.fetchone()
            return str(row[0]) if row else default

    async def set_provider(self, user_id: int, directory: Path, provider: str) -> None:
        async with self.db.get_connection() as conn:
            await conn.execute(
                "INSERT INTO agent_preferences (user_id, project_path, provider) "
                "VALUES (?, ?, ?) ON CONFLICT(user_id, project_path) "
                "DO UPDATE SET provider = excluded.provider",
                (user_id, canonical_path_key(directory.resolve()), provider),
            )
            await conn.commit()

    async def get_thread_tokens(self, session_id: str) -> Dict[str, int]:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT input_tokens, cached_tokens, output_tokens "
                "FROM agent_thread_usage WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
            return (
                dict(zip(("inputTokens", "cachedInputTokens", "outputTokens"), row))
                if row
                else {}
            )

    async def save_thread_tokens(
        self, session_id: str, usage: Dict[str, int], user_id: Optional[int] = None
    ) -> None:
        async with self.db.get_connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "SELECT input_tokens + output_tokens FROM agent_thread_usage "
                "WHERE session_id = ?",
                (session_id,),
            )
            previous = await cursor.fetchone()
            delta = max(
                0,
                usage.get("inputTokens", 0)
                + usage.get("outputTokens", 0)
                - (int(previous[0]) if previous else 0),
            )
            await conn.execute(
                "INSERT INTO agent_thread_usage "
                "(session_id, input_tokens, cached_tokens, output_tokens) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "input_tokens = excluded.input_tokens, "
                "cached_tokens = excluded.cached_tokens, "
                "output_tokens = excluded.output_tokens",
                (
                    session_id,
                    usage.get("inputTokens", 0),
                    usage.get("cachedInputTokens", 0),
                    usage.get("outputTokens", 0),
                ),
            )
            if user_id is not None and delta:
                await conn.execute(
                    "INSERT INTO agent_daily_usage (user_id, date, tokens) "
                    "VALUES (?, ?, ?) ON CONFLICT(user_id, date) DO UPDATE SET "
                    "tokens = tokens + excluded.tokens",
                    (user_id, datetime.now(UTC).date().isoformat(), delta),
                )
            await conn.commit()

    async def daily_codex_tokens(self, user_id: int) -> int:
        day = datetime.now(UTC).date().isoformat()
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT tokens FROM agent_daily_usage WHERE user_id = ? AND date = ?",
                (user_id, day),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0
