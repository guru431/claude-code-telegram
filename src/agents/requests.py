"""Conversation-scoped, asynchronous user decisions requested by an agent."""

from contextvars import ContextVar
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

DecisionHandler = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]
decision_handler: ContextVar[Optional[DecisionHandler]] = ContextVar(
    "agent_decision_handler", default=None
)
run_user_id: ContextVar[Optional[int]] = ContextVar("agent_run_user_id", default=None)
run_token_limit: ContextVar[Optional[int]] = ContextVar(
    "agent_run_token_limit", default=None
)
preference_directory: ContextVar[Optional[Path]] = ContextVar(
    "agent_preference_directory", default=None
)
