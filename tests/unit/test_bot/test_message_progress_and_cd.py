"""Tests for progress-update HTML escaping.

The cd-path extraction tests that used to live here were removed together with
``_update_working_directory_from_claude_response``: the working directory is no
longer inferred by regex from Claude's prose. See ``TestCwdIsNotInferred`` below
for the property that replaced them.
"""

from typing import Optional

from src.bot.handlers import message as message_handlers
from src.bot.handlers.message import _format_progress_update


class FakeUpdateObj:
    """Minimal stand-in for a Claude stream update object."""

    def __init__(
        self,
        type: str,
        content: Optional[str] = None,
        metadata: Optional[dict] = None,
        tool_calls: Optional[list] = None,
    ) -> None:
        self.type = type
        self.content = content
        self.metadata = metadata
        self.tool_calls = tool_calls or []
        self.progress: Optional[dict] = None

    def is_error(self) -> bool:
        return False

    def get_error_message(self) -> Optional[str]:
        return None

    def get_progress_percentage(self) -> Optional[int]:
        return None

    def get_tool_names(self) -> list:
        return [c["name"] for c in self.tool_calls]


class TestProgressUpdateEscaping:
    """Claude's output must never inject raw HTML into Telegram messages."""

    async def test_assistant_content_preview_is_escaped(self) -> None:
        obj = FakeUpdateObj(
            "assistant", content="Fixing <b>bold</b> & <script>alert(1)</script>"
        )
        result = await _format_progress_update(obj)
        assert result is not None
        assert "<script>" not in result
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in result
        assert "&amp;" in result
        # Our own wrapper tags survive.
        assert result.endswith("</i>")

    async def test_tool_result_name_is_escaped(self) -> None:
        obj = FakeUpdateObj(
            "tool_result",
            metadata={"tool_use_id": "x", "tool_name": "Read<i>"},
        )
        result = await _format_progress_update(obj)
        assert result is not None
        assert "Read&lt;i&gt;" in result

    async def test_tool_names_are_escaped(self) -> None:
        obj = FakeUpdateObj("assistant", tool_calls=[{"name": "Bash<b>"}])
        result = await _format_progress_update(obj)
        assert result is not None
        assert "Bash&lt;b&gt;" in result

    async def test_progress_content_is_escaped(self) -> None:
        obj = FakeUpdateObj("progress", content="step <1>")
        result = await _format_progress_update(obj)
        assert result is not None
        assert "step &lt;1&gt;" in result


class TestCwdIsNotInferred:
    """The bot must not change its working directory by reading Claude's text.

    The old heuristic matched any line starting with ``cd `` — including install
    instructions Claude quoted from a README ("cd my-app && npm install") — and
    silently moved the user into another directory with another auto-resumed
    session, with no notification. The working directory now changes only through
    explicit actions (/repo, the ``cd:`` callback, resume).
    """

    def test_extractor_is_gone(self) -> None:
        assert not hasattr(
            message_handlers, "_update_working_directory_from_claude_response"
        )
