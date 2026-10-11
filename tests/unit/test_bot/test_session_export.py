"""Tests for SessionExporter."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.features.session_export import ExportFormat, SessionExporter


def _exporter(session_id: str) -> SessionExporter:
    storage = MagicMock()
    storage.sessions.get_session = AsyncMock(
        return_value=SimpleNamespace(
            session_id=session_id,
            user_id=1,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            last_used=None,
        )
    )
    storage.messages.get_session_messages = AsyncMock(return_value=[])
    return SessionExporter(storage)


@pytest.mark.parametrize(
    "session_id,expected_segment",
    [
        ("abcd-123ef", "session_abcd-123_"),
        ("../../x/yy", "session_x_"),
        ("/\\:*?<>|", "session_unknown_"),
    ],
)
async def test_filename_session_segment_is_sanitized(
    session_id: str, expected_segment: str
) -> None:
    exported = await _exporter(session_id).export_session(
        1, session_id, ExportFormat.JSON
    )

    assert exported.filename.startswith(expected_segment)
    assert exported.filename.endswith(".json")
    assert "/" not in exported.filename and "\\" not in exported.filename


def test_html_export_keeps_heading_levels() -> None:
    """``##`` used to come out as <h3>, flattening the document outline."""
    html = SessionExporter(MagicMock())._markdown_to_html(
        "# Title\n## Section\n### Message"
    )

    assert "<h1>Title</h1>" in html
    assert "<h2>Section</h2>" in html
    assert "<h3>Message</h3>" in html
