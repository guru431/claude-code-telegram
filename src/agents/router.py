"""Route the existing bot API to a durable, project-specific agent choice."""

from pathlib import Path
from typing import Any, Dict, List, Optional

from ..claude.exceptions import ClaudeProcessError
from ..claude.facade import ClaudeIntegration
from ..config.settings import Settings
from ..utils.constants import AUTOMATION_USER_ID
from .codex import codex_command
from .requests import preference_directory, run_token_limit, run_user_id
from .storage import AgentState, session_provider
from .types import AgentResponse


class AgentRouter:
    def __init__(
        self,
        config: Settings,
        state: AgentState,
        integrations: Dict[str, ClaudeIntegration],
    ):
        self.config = config
        self.state = state
        self.integrations = integrations
        # Compatibility for classic /end, which only needs remove_session().
        self.session_manager = self

    async def get_provider(self, user_id: int, directory: Path) -> str:
        if user_id == AUTOMATION_USER_ID:
            return self.config.automation_agent
        return await self.state.get_provider(
            user_id, self._preference_root(directory), self.config.default_agent
        )

    @staticmethod
    def _preference_root(directory: Path) -> Path:
        root = preference_directory.get()
        if root is not None and directory.resolve().is_relative_to(root.resolve()):
            return root
        return directory

    async def set_provider(self, user_id: int, directory: Path, provider: str) -> None:
        if provider not in self.integrations:
            raise ValueError("Choose claude or codex")
        if provider == "codex":
            if not self.config.codex_enabled:
                raise ValueError("Codex is disabled by the bot administrator")
            codex_command(self.config.codex_cli_path)
        await self.state.set_provider(
            user_id, self._preference_root(directory), provider
        )

    async def _integration(self, user_id: int, directory: Path) -> ClaudeIntegration:
        provider = await self.get_provider(user_id, directory)
        if provider == "codex" and not self.config.codex_enabled:
            raise ClaudeProcessError("Codex is disabled. Use /agent claude.")
        return self.integrations[provider]

    async def run_command(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        **kwargs: Any,
    ) -> AgentResponse:
        provider = await self.get_provider(user_id, working_directory)
        integration = await self._integration(user_id, working_directory)
        if session_id and session_provider(session_id) != provider:
            session_id = None
        if provider == "codex":
            used_tokens = await self.state.daily_codex_tokens(user_id)
            if used_tokens >= self.config.codex_max_tokens_per_user_per_day:
                raise ClaudeProcessError("Today's Codex token budget is exhausted.")
            if session_id:
                owned = await integration.session_manager.storage.load_session(
                    session_id, user_id
                )
                if owned is None:
                    session_id = None
        owner_token = run_user_id.set(user_id)
        budget_token = run_token_limit.set(
            min(
                self.config.codex_max_tokens_per_request,
                self.config.codex_max_tokens_per_user_per_day - used_tokens,
            )
            if provider == "codex"
            else None
        )
        try:
            return await integration.run_command(
                prompt=prompt,
                working_directory=working_directory,
                user_id=user_id,
                session_id=session_id,
                **kwargs,
            )
        finally:
            run_token_limit.reset(budget_token)
            run_user_id.reset(owner_token)

    async def find_resumable_session(
        self, user_id: int, working_directory: Path
    ) -> Any:
        integration = await self._integration(user_id, working_directory)
        return await integration.find_resumable_session(user_id, working_directory)

    async def continue_session(
        self,
        user_id: int,
        working_directory: Path,
        prompt: Optional[str] = None,
        on_stream: Any = None,
    ) -> Optional[AgentResponse]:
        session = await self.find_resumable_session(user_id, working_directory)
        if session is None:
            return None
        return await self.run_command(
            prompt=prompt or "Please continue where we left off",
            working_directory=working_directory,
            user_id=user_id,
            session_id=session.session_id,
            on_stream=on_stream,
        )

    async def get_session_info(self, session_id: str, user_id: int) -> Any:
        integration = self.integrations[session_provider(session_id)]
        return await integration.get_session_info(session_id, user_id)

    async def get_user_sessions(self, user_id: int) -> List[Dict[str, Any]]:
        sessions: List[Dict[str, Any]] = []
        for provider, integration in self.integrations.items():
            for session in await integration.get_user_sessions(user_id):
                sessions.append({**session, "provider": provider})
        return sorted(sessions, key=lambda s: s["last_used"], reverse=True)

    async def get_user_summary(self, user_id: int) -> Dict[str, Any]:
        summaries = [
            await integration.get_user_summary(user_id)
            for integration in self.integrations.values()
        ]
        return {
            "user_id": user_id,
            **{
                key: sum(s[key] for s in summaries)
                for key in (
                    "total_sessions",
                    "active_sessions",
                    "total_cost",
                    "total_messages",
                )
            },
            "projects": list({p for s in summaries for p in s["projects"]}),
        }

    async def remove_session(self, session_id: str) -> None:
        await self.integrations[
            session_provider(session_id)
        ].session_manager.remove_session(session_id)

    async def cleanup_expired_sessions(self) -> int:
        count = 0
        for integration in self.integrations.values():
            count += await integration.cleanup_expired_sessions()
        return count

    async def shutdown(self) -> None:
        for integration in self.integrations.values():
            await integration.shutdown()
