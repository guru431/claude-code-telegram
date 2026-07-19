"""Regression tests for callback-path message truncation.

The quick-action, follow-up and git-diff callbacks used to trim their output
with ``response_text[:4000]``. Slicing HTML by character position can land
inside a tag or an entity, which leaves the message unbalanced and makes
Telegram reject it with a 400 — the exact failure the shared splitter was
introduced to remove everywhere else. These tests pin the replacement helper.
"""

import pytest

from src.bot.handlers.callback import _first_chunk
from src.bot.utils.html_format import tg_len
from src.utils.constants import TELEGRAM_MAX_MESSAGE_LENGTH

from .test_html_splitter import assert_balanced

_NOTICE = "\n\n<i>(Response truncated)</i>"


class TestShortInputUntouched:
    """Anything that already fits must come back byte for byte."""

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "plain short text",
            "<b>bold</b> and <a href='http://x/'>link</a>",
            "A" * (TELEGRAM_MAX_MESSAGE_LENGTH - tg_len(_NOTICE)),
        ],
    )
    def test_returns_input_unchanged(self, text: str) -> None:
        assert _first_chunk(text) == text


class TestTruncation:
    """Over-long input is cut without breaking markup."""

    def test_result_fits_telegram_limit(self) -> None:
        text = "<b>" + ("word " * 3000) + "</b>"
        assert tg_len(_first_chunk(text)) <= TELEGRAM_MAX_MESSAGE_LENGTH

    def test_result_is_balanced(self) -> None:
        # A bold run spanning the cut: naive slicing leaves a dangling <b>.
        text = "<b>" + ("word " * 3000) + "</b>"
        assert_balanced(_first_chunk(text))

    def test_notice_is_appended_only_when_truncated(self) -> None:
        long_text = "x" * (TELEGRAM_MAX_MESSAGE_LENGTH * 2)
        assert _first_chunk(long_text).endswith(_NOTICE)
        assert not _first_chunk("short").endswith(_NOTICE)

    def test_entity_is_never_split(self) -> None:
        # &amp; repeated: a positional cut can leave a half-written "&am".
        text = "&amp;" * 2000
        result = _first_chunk(text)
        body = result[: -len(_NOTICE)] if result.endswith(_NOTICE) else result
        assert "&" not in body.replace("&amp;", "")

    def test_pre_code_block_is_reopened_not_broken(self) -> None:
        # Mirrors the git-diff callback, which wraps output in <pre><code>.
        text = "<pre><code>" + ("diff line\n" * 800) + "</code></pre>"
        result = _first_chunk(text, notice="\n\n<i>(output truncated)</i>")
        assert tg_len(result) <= TELEGRAM_MAX_MESSAGE_LENGTH
        assert_balanced(result)

    def test_astral_chars_stay_within_utf16_budget(self) -> None:
        # Emoji count as 2 UTF-16 units; a code-point budget would overshoot.
        text = "🔥" * (TELEGRAM_MAX_MESSAGE_LENGTH)
        assert tg_len(_first_chunk(text)) <= TELEGRAM_MAX_MESSAGE_LENGTH
