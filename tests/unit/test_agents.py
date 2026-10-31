"""Backend contracts: provider isolation, durable routing and Codex events."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.codex import CodexManager, codex_command
from src.agents.requests import decision_handler, preference_directory
from src.agents.router import AgentRouter
from src.agents.storage import AgentState, ProviderSessionStorage
from src.agents.tools import ProjectTools
from src.agents.types import AgentResponse
from src.bot.orchestrator import AgentDecision, MessageOrchestrator
from src.bot.update_processor import StopAwareUpdateProcessor
from src.bot.utils.image_extractor import validate_image_path
from src.claude.facade import ClaudeIntegration
from src.claude.session import ClaudeSession
from src.config import create_test_config
from src.exceptions import ClaudeProcessError
from src.security.validators import SecurityValidator
from src.storage.database import DatabaseManager
from src.storage.session_storage import SQLiteSessionStorage


@pytest.fixture
def settings(tmp_path):
    return create_test_config(approved_directory=str(tmp_path), enable_mcp=False)


@pytest.fixture
async def state(tmp_path, migrated_db):
    db = DatabaseManager(str(migrated_db(tmp_path / "agents.db")))
    await db.initialize()
    yield AgentState(db)
    await db.close()


async def test_preferences_survive_restart_and_are_scoped(state, settings, tmp_path):
    backend = MagicMock(spec=ClaudeIntegration)
    router = AgentRouter(settings, state, {"claude": backend, "codex": backend})
    await state.set_provider(1, tmp_path, "codex")
    reopened = AgentState(state.db)
    assert await reopened.get_provider(1, tmp_path, "claude") == "codex"
    assert await router.get_provider(2, tmp_path) == "claude"
    assert await router.get_provider(1, tmp_path / "other") == "claude"


async def test_provider_storage_never_loads_or_evicts_other_backend(
    state, settings, tmp_path
):
    shared = SQLiteSessionStorage(state.db)
    now = datetime(2026, 10, 30, tzinfo=UTC)
    for session_id in ("claude-one", "codex:thread-one"):
        await shared.save_session(ClaudeSession(session_id, 1, tmp_path, now, now))
    claude = ProviderSessionStorage(shared, "claude")
    codex = ProviderSessionStorage(shared, "codex")
    assert [s.session_id for s in await claude.get_user_sessions(1)] == ["claude-one"]
    assert await claude.load_session("codex:thread-one", 1) is None
    assert await codex.load_session("codex:thread-one", 2) is None
    assert await claude.session_known("codex:unknown")
    await claude.delete_session("codex:thread-one")
    assert await codex.load_session("codex:thread-one", 1) is not None
    with pytest.raises(ValueError, match="different provider"):
        await codex.save_session(ClaudeSession("wrong", 1, tmp_path, now, now))


async def test_router_discards_stale_id_on_switch_and_enforces_token_budget(
    state, settings, tmp_path
):
    claude, codex = MagicMock(), MagicMock()
    codex.run_command = AsyncMock(
        return_value=AgentResponse("ok", "codex:new", 0, 1, 1)
    )
    router = AgentRouter(settings, state, {"claude": claude, "codex": codex})
    await state.set_provider(1, tmp_path, "codex")
    await router.run_command("hello", tmp_path, 1, session_id="old-claude")
    assert codex.run_command.await_args.kwargs["session_id"] is None
    state.daily_codex_tokens = AsyncMock(
        return_value=settings.codex_max_tokens_per_user_per_day
    )
    with pytest.raises(ClaudeProcessError, match="budget"):
        await router.run_command("hello", tmp_path, 1)
    claude.run_command.assert_not_called()


class FakeRPC:
    events_to_send = []
    config = {"sandbox_mode": None, "mcp_servers": {"unrelated": {}}}
    instance = None

    def __init__(self, command, cwd, env):
        FakeRPC.instance = self
        self.command, self.cwd, self.env = command, cwd, env
        self.events = asyncio.Queue()
        self.calls = []
        self.replies = []
        self.closed = False
        self.profile = next(
            value.split("=", 1)[1].strip('"')
            for value in command
            if value.startswith("default_permissions=")
        )

    async def start(self):
        pass

    async def request(self, method, params=None):
        self.calls.append((method, params))
        if method == "config/read":
            return {
                "config": {
                    **self.config,
                    "default_permissions": self.profile,
                    "features": {
                        name: False
                        for name in (
                            "shell_tool",
                            "hooks",
                            "apps",
                            "plugins",
                            "memories",
                            "multi_agent",
                        )
                    },
                }
            }
        if method in {"thread/start", "thread/resume"}:
            return {
                "thread": {"id": "native-one"},
                "model": "test-model",
                "activePermissionProfile": {"id": self.profile},
            }
        if method == "turn/start":
            for event in self.events_to_send:
                await self.events.put(event)
            return {"turn": {"id": "turn-one"}}
        if method == "turn/interrupt":
            await self.events.put(
                {
                    "method": "turn/completed",
                    "params": {"turn": {"status": "interrupted"}},
                }
            )
        return {}

    async def send(self, reply):
        self.replies.append(reply)

    async def close(self):
        self.closed = True


def event(method, **params):
    return {"method": method, "params": params}


@pytest.fixture
def manager(monkeypatch, settings, state):
    monkeypatch.setattr("src.agents.codex.CodexRPC", FakeRPC)
    monkeypatch.setattr("src.agents.codex.codex_command", lambda _: ["codex"])
    monkeypatch.setattr(FakeRPC, "events_to_send", [])
    monkeypatch.setattr(
        FakeRPC, "config", {"sandbox_mode": None, "mcp_servers": {"unrelated": {}}}
    )
    return CodexManager(settings, SecurityValidator(settings.approved_directory), state)


async def test_codex_stream_final_answer_usage_and_isolated_config(
    manager, monkeypatch, tmp_path
):
    totals = {"inputTokens": 120, "cachedInputTokens": 20, "outputTokens": 30}
    monkeypatch.setattr(
        FakeRPC,
        "events_to_send",
        [
            event("item/agentMessage/delta", itemId="comment", delta="Working..."),
            event(
                "item/completed",
                item={
                    "id": "comment",
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": "Working...",
                },
            ),
            event(
                "item/started",
                item={
                    "id": "image",
                    "type": "mcpToolCall",
                    "server": "telegram",
                    "tool": "send_image_to_user",
                    "arguments": {"file_path": "image.png"},
                },
            ),
            event("item/agentMessage/delta", itemId="answer", delta="Done"),
            event(
                "item/completed",
                item={
                    "id": "answer",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "Done",
                },
            ),
            event("thread/tokenUsage/updated", tokenUsage={"total": totals}),
            event("turn/completed", turn={"status": "completed"}),
        ],
    )
    streamed = []
    result = await manager.execute_command(
        "hello",
        tmp_path,
        stream_callback=streamed.append,
        images=[{"media_type": "image/png", "data": "cGl4ZWw="}],
    )
    assert result.content == "Done" and result.session_id == "codex:native-one"
    assert result.token_usage == totals and not result.cost_known
    assert not result.is_error and result.model == "test-model"
    assert any(s.content == "Done" for s in streamed)
    assert result.tools_used[0]["name"].endswith("__send_image_to_user")
    start = next(p for name, p in FakeRPC.instance.calls if name == "thread/start")
    assert start["config"]["mcp_servers"]["unrelated"]["enabled"] is False
    turn = next(p for name, p in FakeRPC.instance.calls if name == "turn/start")
    assert turn["input"][1] == {
        "type": "image",
        "url": "data:image/png;base64,cGl4ZWw=",
    }
    assert FakeRPC.instance.closed


async def test_codex_resumes_native_id_and_counts_only_new_tokens(
    manager, monkeypatch, tmp_path
):
    await manager.state.save_thread_tokens(
        "codex:native-one",
        {"inputTokens": 100, "cachedInputTokens": 10, "outputTokens": 20},
    )
    monkeypatch.setattr(
        FakeRPC,
        "events_to_send",
        [
            event(
                "thread/tokenUsage/updated",
                tokenUsage={
                    "total": {
                        "inputTokens": 150,
                        "cachedInputTokens": 30,
                        "outputTokens": 30,
                    }
                },
            ),
            event("turn/completed", turn={"status": "completed"}),
        ],
    )
    result = await manager.execute_command(
        "next", tmp_path, session_id="codex:native-one", continue_session=True
    )
    resume = next(p for name, p in FakeRPC.instance.calls if name == "thread/resume")
    assert resume["threadId"] == "native-one"
    assert result.token_usage == {
        "inputTokens": 50,
        "cachedInputTokens": 20,
        "outputTokens": 10,
    }


@pytest.mark.parametrize(
    "filename,delivered", [("image.png", True), (".ssh/image.png", False)]
)
async def test_codex_image_interception_resolves_paths_and_checks_secrets(
    manager, monkeypatch, tmp_path, filename, delivered
):
    (tmp_path / filename).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / filename).write_bytes(b"test-image")
    monkeypatch.setattr(
        FakeRPC,
        "events_to_send",
        [
            event(
                "item/started",
                item={
                    "id": "image",
                    "type": "dynamicToolCall",
                    "tool": "send_image_to_user",
                    "arguments": {"file_path": filename, "caption": ""},
                },
            ),
            event("turn/completed", turn={"status": "completed"}),
        ],
    )
    attachments = []

    def stream(update):
        for call in update.tool_calls or []:
            image = validate_image_path(call["input"]["file_path"], tmp_path)
            if image:
                attachments.append(image)

    await manager.execute_command("send image", tmp_path, stream_callback=stream)
    assert bool(attachments) is delivered


async def test_codex_token_limit_interrupts_without_replaying(
    manager, monkeypatch, tmp_path
):
    manager.config.codex_max_tokens_per_request = 10
    monkeypatch.setattr(
        FakeRPC,
        "events_to_send",
        [
            event(
                "thread/tokenUsage/updated", tokenUsage={"total": {"inputTokens": 11}}
            ),
        ],
    )
    result = await manager.execute_command("hello", tmp_path)
    assert result.is_error and not result.interrupted
    assert result.error_type == "token_budget_exceeded"
    assert sum(name == "turn/start" for name, _ in FakeRPC.instance.calls) == 1


async def test_codex_stop_closes_process_and_keeps_session(manager, tmp_path):
    stop = asyncio.Event()
    stop.set()
    result = await manager.execute_command("hello", tmp_path, interrupt_event=stop)
    assert result.interrupted and result.session_id == "codex:native-one"
    assert FakeRPC.instance.closed


async def test_codex_failure_is_not_success_or_retried(manager, monkeypatch, tmp_path):
    monkeypatch.setattr(
        FakeRPC,
        "events_to_send",
        [
            event(
                "turn/completed",
                turn={"status": "failed", "error": {"message": "model unavailable"}},
            ),
        ],
    )
    result = await manager.execute_command("hello", tmp_path)
    assert result.is_error and result.error_type == "codex_turn_failed"
    assert "model unavailable" in result.content
    assert sum(name == "turn/start" for name, _ in FakeRPC.instance.calls) == 1


async def test_codex_rejects_legacy_sandbox_and_cleans_up(
    manager, monkeypatch, tmp_path
):
    monkeypatch.setattr(FakeRPC, "config", {"sandbox_mode": "danger-full-access"})
    with pytest.raises(ClaudeProcessError, match="legacy sandbox"):
        await manager.execute_command("hello", tmp_path)
    assert FakeRPC.instance.closed
    assert not any(n == "thread/start" for n, _ in FakeRPC.instance.calls)


async def test_codex_unattended_requests_have_no_shell_or_approval(
    manager, monkeypatch, tmp_path
):
    callback = AsyncMock(return_value={"decision": "accept"})
    token = decision_handler.set(callback)
    try:
        response = await manager._server_request(
            {
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "git status"},
            },
        )
    finally:
        decision_handler.reset(token)
    assert response == {"decision": "decline"}
    callback.assert_not_called()
    command = " ".join(manager._command(tmp_path, True))
    assert "features.shell_tool=false" in command
    assert "network.enabled=false" in command


async def test_project_tools_validate_before_read_write_or_execute(settings, tmp_path):
    rpc = SimpleNamespace(request=AsyncMock(return_value={"exitCode": 0}))
    tools = ProjectTools(
        rpc, SecurityValidator(tmp_path), tmp_path, tmp_path, False, False
    )
    (tmp_path / ".env").write_text("secret-canary", encoding="utf-8")
    for name, arguments in (
        ("read_file", {"path": ".env"}),
        ("read_file", {"path": str(tmp_path.parent / "outside.txt")}),
        ("write_file", {"path": "../outside.txt", "content": "no"}),
        ("run_command", {"command": "cat ../outside.txt"}),
    ):
        result = await tools.call(name, arguments)
        assert not result["success"]
        assert "secret-canary" not in str(result)
    rpc.request.assert_not_called()
    assert (await tools.call("write_file", {"path": "file.txt", "content": "before"}))[
        "success"
    ]
    assert (
        await tools.call(
            "edit_file", {"path": "file.txt", "old_text": "before", "new_text": "after"}
        )
    )["success"]
    assert (tmp_path / "file.txt").read_text(encoding="utf-8") == "after"


async def test_readonly_tool_overrides_cannot_be_widened(settings, tmp_path):
    tools = ProjectTools(
        None,
        SecurityValidator(tmp_path),
        tmp_path,
        tmp_path,
        True,
        False,
        allowed_tools_override=[],
    )
    assert not tools.specs()
    assert not (await tools.call("read_file", {"path": "file.txt"}))["success"]
    tools.allowed_tools_override = ["Read"]
    assert [spec["name"] for spec in tools.specs()] == ["read_file"]
    assert not (await tools.call("write_file", {"path": "file.txt", "content": "no"}))[
        "success"
    ]


async def test_shell_stop_terminates_the_running_command(settings, tmp_path):
    started, stopped = asyncio.Event(), asyncio.Event()
    interrupt = asyncio.Event()
    calls = []

    async def request(method, params):
        calls.append((method, params))
        if method == "command/exec":
            started.set()
            await stopped.wait()
            return {"exitCode": -1}
        if method == "command/exec/terminate":
            stopped.set()
            return {}

    tools = ProjectTools(
        SimpleNamespace(request=request),
        SecurityValidator(tmp_path),
        tmp_path,
        tmp_path,
        False,
        False,
        interrupt,
    )
    task = asyncio.create_task(tools.call("run_command", {"command": "git status"}))
    await started.wait()
    interrupt.set()
    await task
    assert calls[0][1]["processId"] == calls[1][1]["processId"]
    assert calls[1][0] == "command/exec/terminate"
    assert calls[0][1]["sandboxPolicy"]["networkAccess"] is False


async def test_windows_unsupported_termination_aborts_only_its_run(tmp_path):
    started, stopped = asyncio.Event(), asyncio.Event()
    interrupt = asyncio.Event()

    async def request(method, params):
        if method == "command/exec":
            started.set()
            await stopped.wait()
            raise ClaudeProcessError("process exited")
        raise ClaudeProcessError("not supported for windows sandbox processes")

    async def abort():
        stopped.set()

    rpc = SimpleNamespace(request=request, abort=AsyncMock(side_effect=abort))
    tools = ProjectTools(
        rpc, SecurityValidator(tmp_path), tmp_path, tmp_path, False, False, interrupt
    )
    task = asyncio.create_task(tools.call("run_command", {"command": "git status"}))
    await started.wait()
    interrupt.set()
    assert not (await task)["success"]
    rpc.abort.assert_awaited_once()


@pytest.mark.parametrize(
    "overrides",
    [
        {"default_agent": "codex", "codex_enabled": False},
        {"codex_input_cost_per_million": 1},
        {"codex_max_tokens_per_request": 0},
    ],
)
def test_invalid_codex_configuration_is_rejected(tmp_path, overrides):
    with pytest.raises(ValueError):
        create_test_config(approved_directory=str(tmp_path), **overrides)


async def test_usage_is_durable_and_not_counted_twice(state, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 30, 12, tzinfo=UTC)

    monkeypatch.setattr("src.agents.storage.datetime", Clock)
    usage = {"inputTokens": 100, "cachedInputTokens": 50, "outputTokens": 20}
    await state.save_thread_tokens("codex:one", usage, user_id=7)
    await state.save_thread_tokens("codex:one", usage, user_id=7)
    assert await AgentState(state.db).daily_codex_tokens(7) == 120
    assert await state.daily_codex_tokens(8) == 0
    await state.save_thread_tokens(
        "codex:one", {**usage, "outputTokens": 30}, user_id=7
    )
    assert await state.daily_codex_tokens(7) == 130


async def test_topic_choice_applies_in_subdirectories(state, settings, tmp_path):
    router = AgentRouter(settings, state, {})
    await state.set_provider(1, tmp_path, "codex")
    token = preference_directory.set(tmp_path)
    try:
        assert await router.get_provider(1, tmp_path / "src") == "codex"
        assert await router.get_provider(1, tmp_path.parent) == "claude"
    finally:
        preference_directory.reset(token)


async def test_decision_callbacks_cannot_be_answered_by_another_user(settings):
    bot = MessageOrchestrator(settings, {})
    future = asyncio.get_running_loop().create_future()
    message = SimpleNamespace(chat_id=1, chat=SimpleNamespace(id=1), message_id=10)
    bot._agent_decisions["pending"] = AgentDecision(
        7, 1, future, ["Approve", "Decline"], message
    )
    query = SimpleNamespace(
        data="agent_decision:pending:0",
        from_user=SimpleNamespace(id=8),
        message=message,
        answer=AsyncMock(),
    )
    await bot._agent_decision_callback(SimpleNamespace(callback_query=query), None)
    assert not future.done()
    query.from_user.id = 7
    await bot._agent_decision_callback(SimpleNamespace(callback_query=query), None)
    assert future.result() == "Approve"


async def test_registered_answers_bypass_only_their_owners_queue():
    from telegram import Update

    processor = StopAwareUpdateProcessor()
    lock = asyncio.Lock()
    processor._user_locks[7] = lock
    processor.priority_replies[(1, 10)] = 7
    update = MagicMock(spec=Update)
    update.callback_query = None
    update.effective_user = SimpleNamespace(id=7)
    update.effective_message = SimpleNamespace(
        chat_id=1, reply_to_message=SimpleNamespace(message_id=10)
    )
    await lock.acquire()
    ran = AsyncMock()
    await asyncio.wait_for(processor.do_process_update(update, ran()), 0.5)
    ran.assert_awaited_once()
    update.effective_message.reply_to_message.message_id = 11
    queued = asyncio.create_task(processor.do_process_update(update, ran()))
    await asyncio.sleep(0)
    assert not queued.done()
    lock.release()
    await queued


def test_windows_npm_shim_does_not_use_a_shell(monkeypatch, tmp_path):
    shim = tmp_path / "codex.cmd"
    script = tmp_path / "node_modules/@openai/codex/bin/codex.js"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr("src.agents.codex.shutil.which", lambda _: "node.exe")
    assert codex_command(str(shim)) == ["node.exe", str(script)]
