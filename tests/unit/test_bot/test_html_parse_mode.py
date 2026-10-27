"""Every reply whose text carries HTML tags must be sent with a parse mode.

Without ``parse_mode="HTML"`` Telegram shows ``<b>…</b>`` to the user verbatim.
Eleven classic-mode replies in ``command.py`` did that until 2026-10-27.
"""

import ast
import re
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parents[3] / "src" / "bot"
SEND_METHODS = {"reply_text", "edit_text", "edit_message_text", "send_message"}
HTML_TAG_RE = re.compile(r"</?(b|i|code|pre|a)\b")


def _html_calls_without_parse_mode(source: str) -> list[int]:
    """Line numbers of send calls with tagged literal text and no parse_mode."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in SEND_METHODS
        ):
            continue
        # ``**kwargs`` may carry it; that is not ours to judge.
        if any(kw.arg in ("parse_mode", None) for kw in node.keywords):
            continue
        text = node.args[0] if node.args else None
        if text is None:
            continue
        literal = "".join(
            sub.value
            for sub in ast.walk(text)
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
        )
        if HTML_TAG_RE.search(literal):
            lines.append(node.lineno)
    return lines


def test_detector_flags_a_tagged_reply_without_parse_mode():
    """Guard the guard: a broken detector would make the test below vacuous."""
    source = (
        "async def f(update):\n"
        "    await update.message.reply_text('<b>Hi</b>')\n"
        "    await update.message.reply_text('<b>Hi</b>', parse_mode='HTML')\n"
    )
    assert _html_calls_without_parse_mode(source) == [2]


def test_no_tagged_reply_without_parse_mode():
    offenders = [
        f"{path.relative_to(BOT_DIR.parent.parent)}:{line}"
        for path in sorted(BOT_DIR.rglob("*.py"))
        for line in _html_calls_without_parse_mode(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
