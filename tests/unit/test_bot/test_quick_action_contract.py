"""Contract test between published quick-action buttons and the registry.

The static keyboard in ``ResponseFormatter`` and the dynamic one built by
``QuickActionManager`` both emit ``quick:<id>`` callbacks that the callback
handler resolves against ``QuickActionManager.actions``. Nothing forced the two
to agree, and they drifted: the static board offered ``find_todos``, ``build``
and ``git_status``, none of which were ever registered, so those buttons could
only ever fail. This test fails if any published id stops existing.
"""

from unittest.mock import Mock

from src.bot.features.quick_actions import QuickActionManager
from src.bot.utils.formatting import ResponseFormatter
from src.config.settings import Settings


def _published_ids() -> set[str]:
    """Ids the static keyboard actually offers to users."""
    settings = Mock(spec=Settings)
    settings.enable_quick_actions = True
    markup = ResponseFormatter(settings)._get_quick_actions_keyboard()
    return {
        button.callback_data.split(":", 1)[1]
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("quick:")
    }


def test_every_published_button_is_registered() -> None:
    registered = set(QuickActionManager().actions)
    unknown = _published_ids() - registered
    assert not unknown, f"keyboard offers unregistered quick actions: {sorted(unknown)}"


def test_registry_ids_match_their_keys() -> None:
    # The handler looks up by dict key but reads ``action.id`` back out; a
    # mismatch would route a button to the wrong action.
    for key, action in QuickActionManager().actions.items():
        assert key == action.id


def test_published_ids_are_not_empty() -> None:
    # Guards against the keyboard silently becoming empty and the contract
    # above passing vacuously.
    assert _published_ids()
