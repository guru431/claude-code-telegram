"""Tests for application wiring in ``src.main``."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.main import _sync_discovered_project_topics


class _FakeLog:
    """Collects structlog-style calls for assertions."""

    def __init__(self):
        self.warnings = []
        self.infos = []
        self.exceptions = []

    def warning(self, event, **kwargs):
        self.warnings.append((event, kwargs))

    def info(self, event, **kwargs):
        self.infos.append((event, kwargs))

    def exception(self, event, **kwargs):
        self.exceptions.append((event, kwargs))


def _sync_result(created=1):
    return SimpleNamespace(created=created, reused=0, renamed=0, failed=0)


class TestSyncDiscoveredProjectTopics:
    """Nightly discovery must sync topics for every configured chat."""

    async def test_private_mode_syncs_all_allowed_users(self):
        """Regression: only allowed_users[0] was synced, silently starving the rest."""
        config = SimpleNamespace(
            project_threads_mode="private",
            project_threads_chat_id=None,
            allowed_users=[111, 222, 333],
        )
        manager = SimpleNamespace(sync_topics=AsyncMock(return_value=_sync_result()))
        log = _FakeLog()

        failed = await _sync_discovered_project_topics(
            config=config, manager=manager, telegram_bot=object(), log=log
        )

        assert failed == 0
        synced = [c.kwargs["chat_id"] for c in manager.sync_topics.call_args_list]
        assert synced == [111, 222, 333]
        assert len(log.infos) == 3

    async def test_one_user_failure_does_not_abort_the_rest(self):
        config = SimpleNamespace(
            project_threads_mode="private",
            project_threads_chat_id=None,
            allowed_users=[111, 222, 333],
        )

        async def _sync(_bot, chat_id):
            if chat_id == 222:
                raise RuntimeError("user blocked the bot")
            return _sync_result()

        manager = SimpleNamespace(sync_topics=AsyncMock(side_effect=_sync))
        log = _FakeLog()

        failed = await _sync_discovered_project_topics(
            config=config, manager=manager, telegram_bot=object(), log=log
        )

        assert failed == 1
        synced = [c.kwargs["chat_id"] for c in manager.sync_topics.call_args_list]
        assert synced == [111, 222, 333]
        assert len(log.infos) == 2
        assert log.exceptions, "the failing chat must be logged"
        assert any("some chats failed" in event for event, _ in log.warnings)

    async def test_group_mode_uses_group_chat_id(self):
        config = SimpleNamespace(
            project_threads_mode="group",
            project_threads_chat_id=-1001234567890,
            allowed_users=[111, 222],
        )
        manager = SimpleNamespace(sync_topics=AsyncMock(return_value=_sync_result()))
        log = _FakeLog()

        failed = await _sync_discovered_project_topics(
            config=config, manager=manager, telegram_bot=object(), log=log
        )

        assert failed == 0
        manager.sync_topics.assert_awaited_once()
        assert manager.sync_topics.call_args.kwargs["chat_id"] == -1001234567890

    async def test_no_chat_ids_warns_instead_of_silent_skip(self):
        """Empty allowed_users used to skip the sync with no log line at all."""
        config = SimpleNamespace(
            project_threads_mode="private",
            project_threads_chat_id=None,
            allowed_users=[],
        )
        manager = SimpleNamespace(sync_topics=AsyncMock())
        log = _FakeLog()

        failed = await _sync_discovered_project_topics(
            config=config, manager=manager, telegram_bot=object(), log=log
        )

        assert failed == 0
        manager.sync_topics.assert_not_awaited()
        assert any("no chat id to sync" in event for event, _ in log.warnings)

    async def test_group_mode_without_chat_id_warns(self):
        config = SimpleNamespace(
            project_threads_mode="group",
            project_threads_chat_id=None,
            allowed_users=[111],
        )
        manager = SimpleNamespace(sync_topics=AsyncMock())
        log = _FakeLog()

        await _sync_discovered_project_topics(
            config=config, manager=manager, telegram_bot=object(), log=log
        )

        manager.sync_topics.assert_not_awaited()
        assert any("no chat id to sync" in event for event, _ in log.warnings)
