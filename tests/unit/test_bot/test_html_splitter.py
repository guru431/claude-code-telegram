"""Invariant tests for the single shared Telegram HTML splitter.

Both the notification service and the classic-mode ``ResponseFormatter`` route
through :func:`split_telegram_html`. These tests pin the four invariants the
splitter promises, plus regressions for the two bugs the previous pair of
independent splitters had:

* the notification splitter ``.lstrip()``-ed each following chunk, silently
  eating newlines and indentation inside ``<pre>`` blocks;
* the formatter reopened only ``<pre>``/``<code>``, so a ``<b>``/``<i>``/
  ``<a>``/``<s>`` run spanning a boundary produced chunks with a dangling
  opening tag and a stray closing tag, which Telegram rejects with a 400.
"""

import re
from typing import List

import pytest

from src.bot.utils.html_format import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    split_telegram_html,
    tg_len,
)

_ANY_TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]*)?>")

# Every tag the splitter is allowed to balance, per Telegram's "HTML style".
_TRACKED_TAGS = (
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ins",
    "s",
    "strike",
    "del",
    "a",
    "span",
    "tg-spoiler",
    "tg-emoji",
    "code",
    "pre",
    "blockquote",
)


def strip_tags(text: str) -> str:
    """Remove every HTML tag, leaving text content and whitespace untouched."""
    return _ANY_TAG.sub("", text)


def assert_balanced(chunk: str) -> None:
    """Every tracked tag in *chunk* opens and closes within that chunk."""
    stack: List[str] = []
    for m in _ANY_TAG.finditer(chunk):
        tag = m.group(0)
        name = re.match(r"</?([a-zA-Z][a-zA-Z0-9-]*)", tag).group(1).lower()
        if name not in _TRACKED_TAGS:
            continue
        if tag.startswith("</"):
            assert stack, f"stray closing </{name}> in chunk: {chunk[:60]!r}"
            assert stack.pop() == name, f"mismatched </{name}> in {chunk[:60]!r}"
        elif not tag.rstrip(">").rstrip().endswith("/"):
            stack.append(name)
    assert not stack, f"unclosed {stack} in chunk: {chunk[:60]!r}"


def assert_invariants(text: str, chunks: List[str], max_length: int) -> None:
    """Assert all four splitter invariants at once."""
    # 1. Exact reconstruction: whitespace preserved character for character.
    assert strip_tags("".join(chunks)) == strip_tags(text)
    for chunk in chunks:
        # 2. Balanced tags per chunk.
        assert_balanced(chunk)
        # 3. Within Telegram's UTF-16 budget.
        assert tg_len(chunk) <= max_length


class TestPassThrough:
    """Short input and plain text must survive untouched."""

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "plain short text",
            "text with <b>bold</b> and <a href='http://x/'>link</a>",
            "A" * TELEGRAM_MAX_MESSAGE_LENGTH,
            "line one\n\n   indented line\ttabbed\n",
        ],
    )
    def test_under_limit_is_returned_verbatim(self, text: str) -> None:
        assert split_telegram_html(text) == [text]

    def test_plain_text_hard_split_has_no_injected_tags(self) -> None:
        text = "A" * 5000
        chunks = split_telegram_html(text, 4096)
        assert chunks == ["A" * 4096, "A" * 904]

    def test_rejects_non_positive_limit(self) -> None:
        with pytest.raises(ValueError):
            split_telegram_html("x" * 10, 0)


class TestWhitespacePreservation:
    """Regression: the old notification splitter lstrip()-ed each chunk."""

    def test_indentation_inside_pre_is_preserved(self) -> None:
        """Leading indentation of a continued <pre> line must not be eaten."""
        code = "\n".join(f"    indented_line_{i:03d} = compute()" for i in range(200))
        text = f"<pre><code>{code}</code></pre>"
        chunks = split_telegram_html(text, 1000)

        assert len(chunks) > 1
        assert_invariants(text, chunks, 1000)
        # Explicitly: not a single character of the source body is lost.
        assert strip_tags("".join(chunks)) == code

    def test_blank_line_between_paragraphs_is_preserved(self) -> None:
        para = "word " * 400
        text = "\n\n".join(para for _ in range(6))
        chunks = split_telegram_html(text, 1000)

        assert len(chunks) > 1
        assert_invariants(text, chunks, 1000)
        assert "".join(chunks) == text

    def test_trailing_separator_stays_with_the_earlier_chunk(self) -> None:
        """A preferred break keeps its separator rather than dropping it."""
        text = "A" * 3000 + "\n\n" + "B" * 2000
        chunks = split_telegram_html(text, 4096)
        assert chunks[0].endswith("\n\n")
        assert_invariants(text, chunks, 4096)


class TestTagBalance:
    """Regression: the old formatter reopened only <pre>."""

    @pytest.mark.parametrize(
        "open_tag,name",
        [
            ("<b>", "b"),
            ("<i>", "i"),
            ("<u>", "u"),
            ("<s>", "s"),
            ("<strong>", "strong"),
            ("<em>", "em"),
            ("<code>", "code"),
            ("<blockquote>", "blockquote"),
            ("<tg-spoiler>", "tg-spoiler"),
            ('<span class="tg-spoiler">', "span"),
        ],
    )
    def test_long_run_of_each_tag_stays_balanced(
        self, open_tag: str, name: str
    ) -> None:
        body = "word " * 2000
        text = f"{open_tag}{body}</{name}>"
        chunks = split_telegram_html(text, 4096)

        assert len(chunks) > 1
        assert_invariants(text, chunks, 4096)
        assert chunks[0].endswith(f"</{name}>")
        assert chunks[1].startswith(open_tag)

    def test_anchor_reopens_with_its_href(self) -> None:
        href = '<a href="https://example.com/a/very/long/path?q=1">'
        text = href + "link text " * 900 + "</a>"
        chunks = split_telegram_html(text, 4096)

        assert len(chunks) > 1
        assert_invariants(text, chunks, 4096)
        assert chunks[1].startswith(href)

    def test_nested_tags_close_and_reopen_in_order(self) -> None:
        text = "<b><i><u>" + "x" * 9000 + "</u></i></b>"
        chunks = split_telegram_html(text, 4096)

        assert len(chunks) > 2
        assert_invariants(text, chunks, 4096)
        assert chunks[0].endswith("</u></i></b>")
        assert chunks[1].startswith("<b><i><u>")

    def test_pre_code_language_class_is_reopened(self) -> None:
        open_tag = '<pre><code class="language-python">'
        text = open_tag + "y = 1\n" * 1500 + "</code></pre>"
        chunks = split_telegram_html(text, 4096)

        assert len(chunks) > 1
        assert_invariants(text, chunks, 4096)
        assert chunks[0].endswith("</code></pre>")
        assert chunks[1].startswith(open_tag)

    def test_unsupported_tags_are_literal_text_not_stack_entries(self) -> None:
        """<br>/<div> are not Telegram entities: never tracked, never closed."""
        text = "<div>" + ("<br>text " * 1200) + "</div>"
        chunks = split_telegram_html(text, 4096)

        assert len(chunks) > 1
        for chunk in chunks:
            assert "</div>" not in chunk[:-6] or chunk.count("</div>") == 1
            assert "</br>" not in chunk
            assert tg_len(chunk) <= 4096
        assert "".join(chunks) == text

    def test_stray_closing_tag_is_treated_as_text(self) -> None:
        text = "</b>" + "z" * 5000
        chunks = split_telegram_html(text, 4096)
        assert "".join(chunks) == text
        assert tg_len(chunks[0]) <= 4096


class TestAtomicUnits:
    """Entities, tags and surrogate pairs are never cut in half."""

    def test_entity_is_never_split(self) -> None:
        text = "x" * 4094 + "&amp;" + "y" * 200
        chunks = split_telegram_html(text, 4096)

        assert "".join(chunks) == text
        assert any("&amp;" in c for c in chunks)
        for c in chunks:
            assert not c.endswith("&am")
            assert not c.startswith("p;")
            assert tg_len(c) <= 4096

    def test_numeric_entity_is_never_split(self) -> None:
        text = "x" * 4093 + "&#128512;" + "y" * 200
        chunks = split_telegram_html(text, 4096)
        assert "".join(chunks) == text
        assert any("&#128512;" in c for c in chunks)

    def test_astral_emoji_counted_as_two_units_and_kept_whole(self) -> None:
        """Emoji cost 2 UTF-16 units; a chunk must never end mid-surrogate."""
        text = "😀" * 4000  # 8000 UTF-16 units
        chunks = split_telegram_html(text, 4096)

        assert "".join(chunks) == text
        for c in chunks:
            assert tg_len(c) <= 4096
            # A lone surrogate would fail to round-trip through UTF-8.
            assert c.encode("utf-8").decode("utf-8") == c
        # 4096 units is an even budget, so exactly 2048 emoji fit.
        assert tg_len(chunks[0]) == 4096

    def test_emoji_inside_formatting_stays_balanced(self) -> None:
        text = "<b>" + "😀ok " * 1500 + "</b>"
        chunks = split_telegram_html(text, 4096)
        assert_invariants(text, chunks, 4096)

    def test_tag_boundary_is_never_cut(self) -> None:
        """A cut landing on a tag moves to a token boundary, never inside it."""
        text = "".join(f"<b>{i:04d}</b> " for i in range(700))
        chunks = split_telegram_html(text, 1000)

        assert len(chunks) > 1
        assert_invariants(text, chunks, 1000)
        for c in chunks:
            assert c.count("<") == c.count(">")


class TestTermination:
    """The loop must always make progress, even on pathological input."""

    def test_deeply_nested_tags_exceeding_budget_do_not_hang(self) -> None:
        # 60 nested <b> reopened every chunk would eat a 300-unit budget.
        text = "<b>" * 60 + "x" * 4000 + "</b>" * 60
        chunks = split_telegram_html(text, 300)

        assert strip_tags("".join(chunks)) == "x" * 4000
        for c in chunks:
            assert tg_len(c) <= 300

    def test_oversized_single_tag_is_emitted_alone(self) -> None:
        big_tag = '<a href="' + "u" * 5000 + '">'
        text = big_tag + "tail" * 2000
        chunks = split_telegram_html(text, 4096)

        assert "".join(chunks) == text
        assert chunks[0] == big_tag  # unrepresentable, but never corrupted

    @pytest.mark.parametrize("limit", [64, 65, 128, 257, 999, 1000, 4096])
    def test_invariants_hold_across_limits(self, limit: int) -> None:
        text = (
            "<b>bold</b> intro\n\n"
            + '<a href="https://example.com/x">link</a> '
            + "<pre><code>\n    indented = 1\n    more = 2\n</code></pre>\n"
            + ("plain 😀 text " * 400)
        )
        chunks = split_telegram_html(text, limit)
        assert_invariants(text, chunks, limit)
