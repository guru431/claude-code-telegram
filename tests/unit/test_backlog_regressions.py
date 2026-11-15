"""Boundary and failure-path regressions from the November review."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.codex import CodexRPC
from src.agents.tools import ProjectTools
from src.bot.features.conversation_mode import ConversationContext, ConversationEnhancer
from src.bot.features.session_export import ExportFormat, SessionExporter
from src.bot.handlers.callback import handle_callback_query
from src.bot.handlers.message import _send_formatted_messages
from src.claude.exceptions import ClaudeProcessError
from src.claude.monitor import check_bash_directory_boundary
from src.security.audit import AuditLogger
from src.security.validators import SecurityValidator


@pytest.mark.parametrize(
    "command,allowed",
    [
        ("cat ../outside/file=notes.txt", False),
        ("tee ../outside/file=", False),
        ("dd of=../outside/file", False),
        ("cat notes=draft.txt", True),
        ("dd if=notes.txt of=copy.txt", True),
    ],
)
def test_equals_does_not_hide_path_boundary(tmp_path, command, allowed):
    accepted, _ = check_bash_directory_boundary(command, tmp_path, tmp_path)
    assert accepted is allowed


@pytest.mark.parametrize("error", ["RPC rejected", None, {"message": "RPC rejected"}])
async def test_rpc_errors_keep_reader_alive_and_replies_out_of_events(tmp_path, error):
    rpc = CodexRPC(["codex"], tmp_path, {})
    reader = asyncio.StreamReader()
    rpc.process = SimpleNamespace(stdout=reader)
    future = asyncio.get_running_loop().create_future()
    rpc.pending[1] = future
    for message in (
        {"id": 100, "result": {}},  # a late reply to an expired request
        {"id": 1, "error": error},
        {"method": "still/alive"},
    ):
        reader.feed_data((json.dumps(message) + "\n").encode())
    reader.feed_eof()
    await rpc._read_stdout()
    with pytest.raises(ClaudeProcessError, match="RPC"):
        await future
    assert rpc.events.get_nowait() == {"method": "still/alive"}
    assert rpc.events.get_nowait() == {"method": "_process/exited"}
    assert rpc.events.empty()


async def test_rpc_broken_pipe_cleans_pending_request(tmp_path):
    rpc = CodexRPC(["codex"], tmp_path, {})
    rpc.process = SimpleNamespace(
        stdin=SimpleNamespace(
            write=MagicMock(), drain=AsyncMock(side_effect=BrokenPipeError)
        )
    )
    with pytest.raises(ClaudeProcessError, match="Could not send request"):
        await rpc.request("turn/start")
    assert not rpc.pending


async def test_read_file_past_eof_has_no_invalid_page(tmp_path):
    (tmp_path / "short.md").write_text("one\ntwo\n", encoding="utf-8")
    tools = ProjectTools(
        SimpleNamespace(request=AsyncMock()),
        SecurityValidator(tmp_path),
        tmp_path,
        tmp_path,
        False,
        False,
    )
    result = await tools.call("read_file", {"path": "short.md", "start_line": 500})
    text = result["contentItems"][0]["text"]
    assert "beyond end of file (2 lines)" in text
    assert "Continue" not in text and "500-499" not in text


def test_conversation_response_is_html_with_safe_truncation():
    response = SimpleNamespace(
        content="**Result**: x < y & z\n```python\na < b\n```",
        session_id="<unsafe>",
        cost=0,
    )
    enhancer = ConversationEnhancer()
    enhancer.should_show_suggestions = lambda _: False
    content, _ = enhancer.format_response_with_suggestions(
        response,
        ConversationContext(user_id=1, conversation_turn=1),
        max_content_length=38,
    )
    assert "<b>Result</b>" in content
    assert "&lt;" in content and "&amp;" in content
    assert "<unsafe>" not in content
    assert "<i>(response truncated)</i>" in content
    assert content.count("<pre>") == content.count("</pre>")


async def test_export_count_matches_storage_rows():
    now = datetime(2026, 11, 1, tzinfo=UTC)
    storage = MagicMock()
    storage.sessions.get_session = AsyncMock(
        return_value=SimpleNamespace(
            session_id="one",
            user_id=1,
            created_at=now,
            last_used=now,
        )
    )
    storage.messages.get_session_messages = AsyncMock(
        return_value=[
            SimpleNamespace(
                message_id="m1",
                prompt="Question",
                response="Answer",
                timestamp=now,
            )
        ]
    )
    exporter = SessionExporter(storage)
    result = json.loads(
        (await exporter.export_session(1, "one", ExportFormat.JSON)).content
    )
    assert len(result["messages"]) == 2
    assert result["session"]["message_count"] == 1
    markdown = (await exporter.export_session(1, "one")).content
    assert "**Message Count:** 1" in markdown


@pytest.mark.parametrize("data", [None, ""])
async def test_empty_callback_is_acknowledged_without_error_reply(data, monkeypatch):
    log = MagicMock()
    monkeypatch.setattr("src.bot.handlers.callback.logger", log)
    query = SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=1), answer=AsyncMock()
    )
    update = SimpleNamespace(callback_query=query)
    await handle_callback_query(update, MagicMock())
    query.answer.assert_awaited_once_with()
    log.error.assert_not_called()


async def test_delivery_failure_does_not_expose_telegram_exception():
    reply = AsyncMock(
        side_effect=[RuntimeError("markup"), RuntimeError("private-canary"), None]
    )
    update = SimpleNamespace(message=SimpleNamespace(reply_text=reply, message_id=1))
    message = SimpleNamespace(text="<b>reply</b>", parse_mode="HTML", reply_markup=None)
    await _send_formatted_messages(update, [message])
    notice = reply.call_args.args[0]
    assert "Failed to deliver response" in notice and "private-canary" not in notice


def test_file_risk_uses_filename_words_and_sensitive_directories():
    audit = AuditLogger(MagicMock())
    for path in (
        "src/monkey.py",
        "src/tokenizer.py",
        "keynote.txt",
        "config-project/src/main.py",
    ):
        assert audit._assess_file_access_risk(path, "read") == "low"
    for path in (
        "src/api_key.txt",
        "config.yaml",
        "/etc/settings",
        "C:\\Users\\test\\.ssh\\id_rsa",
    ):
        assert audit._assess_file_access_risk(path, "read") == "medium"
        assert audit._assess_file_access_risk(path, "write") == "high"
