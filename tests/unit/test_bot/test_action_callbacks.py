"""Every ``action:*`` button rendered by the bot must resolve to a handler.

Regression: the classic help keyboard offered "Full Help" and "Main Menu", but
neither name was in the dispatch table, so both buttons always answered
"Unknown Action".
"""

import re
from pathlib import Path

import pytest

from src.bot.handlers.callback import ACTION_HANDLERS

HANDLER_DIR = Path(__file__).resolve().parents[3] / "src" / "bot"
ACTION_DATA_RE = re.compile(r'callback_data="action:([a-z_]+)"')


def _rendered_actions() -> set[str]:
    """Collect every ``action:<name>`` literal used in a keyboard."""
    found: set[str] = set()
    for path in HANDLER_DIR.rglob("*.py"):
        found.update(ACTION_DATA_RE.findall(path.read_text(encoding="utf-8")))
    return found


def test_rendered_actions_are_collected():
    """Guard the guard: a broken regex would make the test below vacuous."""
    actions = _rendered_actions()
    assert "help" in actions
    assert "full_help" in actions


@pytest.mark.parametrize("action", sorted(_rendered_actions()))
def test_every_rendered_action_has_a_handler(action: str):
    assert action in ACTION_HANDLERS, (
        f"Button 'action:{action}' has no handler and falls through to "
        f"'Unknown Action'. Add it to ACTION_HANDLERS or drop the button."
    )
