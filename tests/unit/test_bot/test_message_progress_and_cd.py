"""Tests for progress-update HTML escaping and cd-path extraction."""

from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from src.bot.handlers.message import (
    _format_progress_update,
    _update_working_directory_from_claude_response,
)


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


@pytest.fixture
def approved_dir(tmp_path: Path) -> Path:
    root = tmp_path / "approved"
    root.mkdir()
    return root


def _run(content: str, approved_dir: Path, current: Optional[Path] = None) -> Path:
    """Run the extractor and return the resulting current_directory."""
    context = SimpleNamespace(user_data={"current_directory": current or approved_dir})
    settings = SimpleNamespace(approved_directory=approved_dir)
    claude_response = SimpleNamespace(content=content)
    _update_working_directory_from_claude_response(
        claude_response, context, settings, user_id=1
    )
    return context.user_data["current_directory"]


class TestCdExtraction:
    """The cd heuristic must handle quoted paths and trailing punctuation."""

    def test_quoted_path_with_spaces(self, approved_dir: Path) -> None:
        target = approved_dir / "path with spaces"
        target.mkdir()
        assert _run(f'\ncd "{target}"\n', approved_dir) == target

    def test_single_quoted_path_with_spaces(self, approved_dir: Path) -> None:
        target = approved_dir / "my project"
        target.mkdir()
        assert _run(f"\ncd '{target}'\n", approved_dir) == target

    def test_trailing_period_is_stripped(self, approved_dir: Path) -> None:
        target = approved_dir / "src"
        target.mkdir()
        assert _run("\ncd src.\n", approved_dir) == target

    def test_trailing_comma_is_stripped(self, approved_dir: Path) -> None:
        target = approved_dir / "src"
        target.mkdir()
        assert _run("\ncd src, then run the tests\n", approved_dir) == target

    def test_dot_dot_is_not_mangled(self, approved_dir: Path) -> None:
        child = approved_dir / "child"
        child.mkdir()
        assert _run("\ncd ..\n", approved_dir, current=child) == approved_dir

    def test_plain_relative_path_still_works(self, approved_dir: Path) -> None:
        target = approved_dir / "src"
        target.mkdir()
        assert _run("\ncd src\n", approved_dir) == target

    def test_path_outside_approved_dir_is_ignored(
        self, approved_dir: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        assert _run(f'\ncd "{outside}"\n', approved_dir) == approved_dir
