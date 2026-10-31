"""The response and streaming contract implemented by both agent runtimes."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol


@dataclass
class AgentResponse:
    content: str
    session_id: str
    cost: float
    duration_ms: int
    num_turns: int
    is_error: bool = False
    error_type: Optional[str] = None
    tools_used: List[Dict[str, Any]] = field(default_factory=list)
    interrupted: bool = False
    provider: str = "claude"
    model: Optional[str] = None
    token_usage: Dict[str, int] = field(default_factory=dict)
    cost_known: bool = True
    ephemeral: bool = False


@dataclass
class StreamUpdate:
    type: str
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None


class AgentBackend(Protocol):
    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable] = None,
        allowed_tools_override: Optional[List[str]] = None,
        images: Optional[List[Dict[str, str]]] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        ephemeral: bool = False,
    ) -> AgentResponse: ...
