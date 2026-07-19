"""HTML formatting utilities for Telegram messages.

Telegram's HTML mode only requires escaping 3 characters (<, >, &) vs the many
ambiguous Markdown v1 metacharacters, making it far more robust for rendering
Claude's output which contains underscores, asterisks, brackets, etc.
"""

import re
from collections import deque
from typing import Deque, FrozenSet, Iterator, List, Optional, Tuple

import structlog

from ...utils.constants import TELEGRAM_MAX_MESSAGE_LENGTH

logger = structlog.get_logger()

__all__ = [
    "TELEGRAM_MAX_MESSAGE_LENGTH",
    "escape_html",
    "markdown_to_telegram_html",
    "split_telegram_html",
    "tg_len",
    "utf16_cut",
]

# The complete set of tags Telegram accepts in HTML parse mode (Bot API,
# "HTML style"). Anything outside this set is not a formatting entity, so the
# splitter treats it as literal text and never pushes it onto the open-tag
# stack — which also makes void elements (<br>, <img>) harmless.
_ALLOWED_HTML_TAGS: FrozenSet[str] = frozenset(
    {
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
    }
)

# One token = a whole HTML tag, a whole character entity, or a run of literal
# text. Tags and entities are atomic: a cut is never allowed inside them.
_TOKEN_RE = re.compile(
    r"(?P<tag></?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^<>]*)?>)"
    r"|(?P<entity>&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[a-zA-Z][a-zA-Z0-9]{1,31});)"
)

_TAG_NAME_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)")


def escape_html(text: str) -> str:
    """Escape the 3 HTML-special characters for Telegram.

    This replaces all 3 _escape_markdown functions previously scattered
    across the codebase.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg_len(text: str) -> int:
    """Length as Telegram counts it: UTF-16 code units, not code points.

    Telegram measures message length in UTF-16 units, so any astral character
    (most emoji) counts as 2. Use this for length checks against the 4096 limit.
    """
    return len(text.encode("utf-16-le")) // 2


def utf16_cut(text: str, max_length: int) -> int:
    """Largest code-point index whose UTF-16 length is <= *max_length*.

    Telegram counts message length in UTF-16 units (most emoji count as 2), so
    slicing by code points can overshoot the budget. Surrogate pairs are never
    broken: an astral character is either wholly included or wholly excluded.
    """
    if tg_len(text) <= max_length:
        return len(text)
    units = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > max_length:
            return i
    return len(text)


def _tokenize_html(text: str) -> Iterator[Tuple[str, str]]:
    """Yield ``(kind, token)`` pairs; kind is ``tag``, ``entity`` or ``text``."""
    pos = 0
    for m in _TOKEN_RE.finditer(text):
        if m.start() > pos:
            yield "text", text[pos : m.start()]
        yield ("tag" if m.group("tag") else "entity"), m.group(0)
        pos = m.end()
    if pos < len(text):
        yield "text", text[pos:]


def _classify_tag(tag: str) -> Tuple[str, Optional[str]]:
    """Classify a raw ``<...>`` token as ``open``/``close``/``text``.

    Self-closing tags and any tag outside Telegram's supported set are reported
    as ``text``: they carry no entity state, so tracking them would corrupt the
    open-tag stack (and never being closed, they would leak into every chunk).
    """
    m = _TAG_NAME_RE.match(tag)
    if not m:
        return "text", None
    name = m.group(1).lower()
    if name not in _ALLOWED_HTML_TAGS:
        return "text", None
    if tag.startswith("</"):
        return "close", name
    if tag.rstrip(">").rstrip().endswith("/"):
        return "text", None
    return "open", name


def _preferred_cut(run: str, hard_cut: int) -> int:
    """Move a hard cut back to the nearest paragraph/line/word boundary.

    The separator stays at the *end* of the current chunk rather than being
    stripped, so concatenating the chunks reproduces the source whitespace
    exactly. A boundary is only accepted in the second half of the window, so a
    single early space cannot collapse chunking into tiny messages.
    """
    window = run[:hard_cut]
    floor = hard_cut // 2
    for sep in ("\n\n", "\n", " "):
        idx = window.rfind(sep)
        if idx != -1 and idx + len(sep) > floor:
            return idx + len(sep)
    return hard_cut


def split_telegram_html(
    text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH
) -> List[str]:
    """Split Telegram HTML into chunks that are individually well-formed.

    This is the single splitter used for every outbound message. It walks the
    text as tags/entities/text runs rather than by position, keeping a stack of
    the formatting tags currently open. At a cut, every open tag is closed at
    the end of the chunk and reopened verbatim (preserving attributes such as an
    anchor's ``href`` or a code block's ``class="language-..."``) at the start
    of the next one.

    Guarantees for every returned chunk:

    * ``tg_len(chunk) <= max_length`` -- length in Telegram's UTF-16 units;
    * supported tags are balanced within the chunk;
    * no cut lands inside a ``<...>`` tag, an ``&...;`` entity, or a surrogate
      pair;
    * concatenating the chunks and removing all tags reproduces the source text
      character for character, whitespace included (nothing is stripped).

    The sole exception is a pathological single token (a malformed tag) longer
    than *max_length*, which cannot be represented at all and is emitted as its
    own oversized chunk with a warning.
    """
    if max_length <= 0:
        raise ValueError("max_length must be positive")
    if tg_len(text) <= max_length:
        return [text]

    chunks: List[str] = []
    stack: List[Tuple[str, str]] = []  # (verbatim opening tag, tag name)
    chunk_prefix = ""  # tags reopened at the start of the current chunk
    prefix_len = 0
    buf: List[str] = []
    body_len = 0
    has_content = False  # buffer holds text/entity content, not just tags

    def closing_len() -> int:
        # "</name>" is pure ASCII, so its UTF-16 length is len(name) + 3.
        return sum(len(name) + 3 for _, name in stack)

    def flush() -> None:
        nonlocal chunk_prefix, prefix_len, body_len, has_content
        if has_content:
            # Skip a chunk that would carry only tags: it renders as nothing in
            # Telegram, and emitting it would waste a message (or, right after
            # an opening tag, spin without making progress).
            closing = "".join(f"</{name}>" for _, name in reversed(stack))
            chunks.append(chunk_prefix + "".join(buf) + closing)
        chunk_prefix = "".join(tag for tag, _ in stack)
        prefix_len = tg_len(chunk_prefix)
        buf.clear()
        body_len = 0
        has_content = False

    pending: Deque[Tuple[str, str]] = deque(_tokenize_html(text))

    while pending:
        kind, token = pending.popleft()
        tag_kind, name = _classify_tag(token) if kind == "tag" else ("text", None)
        token_len = tg_len(token)

        if tag_kind == "close" and name is not None:
            # Pop the nearest matching open tag. A stray close tag (no matching
            # open) is literal text, so fall through to the atomic branch.
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][1] == name:
                    # Closing shrinks the required suffix, so it always fits.
                    buf.append(token)
                    body_len += token_len
                    del stack[i]
                    break
            else:
                tag_kind = "text"
            if tag_kind == "close":
                continue

        if tag_kind == "open" and name is not None:
            # An opening tag must leave room for its own closing tag too.
            need = prefix_len + body_len + token_len + closing_len() + len(name) + 3
            if need <= max_length:
                buf.append(token)
                body_len += token_len
                stack.append((token, name))
                continue
            if buf:
                pending.appendleft((kind, token))
                flush()
                continue
            # Cannot open the tag even on a fresh chunk: emit it as literal
            # text instead of tracking an entity we can never close.
            tag_kind = "text"

        available = max_length - prefix_len - body_len - closing_len()

        if token_len <= available:
            buf.append(token)
            body_len += token_len
            has_content = True
            continue

        if kind == "text" and available > 0:
            # Fill the current chunk to the brim, then continue with the rest.
            # Cutting here (rather than flushing first) matters: flushing with
            # only a just-opened tag buffered would emit an empty "<b></b>".
            cut = _preferred_cut(token, utf16_cut(token, available))
            buf.append(token[:cut])
            body_len += tg_len(token[:cut])
            has_content = True
            flush()
            pending.appendleft(("text", token[cut:]))
            continue

        if buf:
            pending.appendleft((kind, token))
            flush()
            continue

        if stack:
            # Nothing buffered yet and the carried-over tags already consume the
            # whole budget. Drop the carried formatting rather than spin.
            logger.warning(
                "Dropping carried HTML formatting: reopened tags exceed the "
                "message budget",
                open_tags=[n for _, n in stack],
                max_length=max_length,
            )
            stack.clear()
            chunk_prefix = ""
            prefix_len = 0
            pending.appendleft((kind, token))
            continue

        # A single atomic token (a malformed or absurdly long tag) longer than
        # the whole limit, on an otherwise empty chunk. It cannot be split
        # without corrupting it, so emit it alone and move on. A text token can
        # never reach here: with an empty buffer and no carried tags the full
        # budget is available, so the branch above always cuts it.
        logger.warning(
            "Emitting oversized atomic HTML token as its own chunk",
            token_length=token_len,
            max_length=max_length,
        )
        chunks.append(token)

    if buf:
        flush()

    return chunks


def markdown_to_telegram_html(text: str) -> str:
    """Convert Claude's markdown output to Telegram-compatible HTML.

    Telegram supports a narrow HTML subset: <b>, <i>, <code>, <pre>,
    <a href>, <s>, <u>. This function converts common markdown patterns
    to that subset while preserving code blocks verbatim.

    Order of operations:
    1. Extract fenced code blocks -> placeholders
    2. Extract inline code -> placeholders
    3. HTML-escape remaining text
    4. Convert bold (**text** / __text__)
    5. Convert italic (*text*, _text_ with word boundaries)
    6. Convert links [text](url)
    7. Convert headers (# Header -> <b>Header</b>)
    8. Convert strikethrough (~~text~~)
    9. Restore placeholders
    """
    placeholders: List[Tuple[str, str]] = []
    placeholder_counter = 0

    def _make_placeholder(html_content: str) -> str:
        nonlocal placeholder_counter
        key = f"\x00PH{placeholder_counter}\x00"
        placeholder_counter += 1
        placeholders.append((key, html_content))
        return key

    # --- 1. Extract fenced code blocks ---
    def _replace_fenced(m: re.Match) -> str:  # type: ignore[type-arg]
        lang = (m.group(1) or "").strip()
        code = m.group(2)
        escaped_code = escape_html(code)
        if lang:
            html = f'<pre><code class="language-{escape_html(lang)}">{escaped_code}</code></pre>'
        else:
            html = f"<pre><code>{escaped_code}</code></pre>"
        return _make_placeholder(html)

    text = re.sub(
        r"```([^\n`]*)\n(.*?)```",
        _replace_fenced,
        text,
        flags=re.DOTALL,
    )

    # --- 2. Extract inline code ---
    def _replace_inline_code(m: re.Match) -> str:  # type: ignore[type-arg]
        code = m.group(1)
        escaped_code = escape_html(code)
        return _make_placeholder(f"<code>{escaped_code}</code>")

    text = re.sub(r"`([^`\n]+)`", _replace_inline_code, text)

    # --- 3. HTML-escape remaining text ---
    text = escape_html(text)

    # --- 4. Bold: **text** or __text__ ---
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # __text__ only at word boundaries (avoid some_var__thing__more)
    text = re.sub(r"(?<!\w)__(\S.*?\S|\S)__(?!\w)", r"<b>\1</b>", text)

    # --- 5. Italic: *text* (require non-space after/before) ---
    text = re.sub(r"\*(\S.*?\S|\S)\*", r"<i>\1</i>", text)
    # _text_ only at word boundaries (avoid my_var_name)
    text = re.sub(r"(?<!\w)_(\S.*?\S|\S)_(?!\w)", r"<i>\1</i>", text)

    # --- 6. Links: [text](url) ---
    def _replace_link(m: re.Match) -> str:  # type: ignore[type-arg]
        label = m.group(1)
        url = m.group(2)
        # Allowlist URL schemes: only http(s) and tg are clickable.
        # Reject javascript:, data:, etc. to prevent unsafe href injection.
        scheme = url.split(":", 1)[0].strip().lower() if ":" in url else ""
        if scheme not in ("http", "https", "tg"):
            # Disallowed/relative scheme: emit label as plain (already-escaped) text.
            return str(label)
        url = url.replace('"', "&quot;")
        return f'<a href="{url}">{label}</a>'

    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        _replace_link,
        text,
    )

    # --- 7. Headers: # Header -> <b>Header</b> ---
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # --- 8. Strikethrough: ~~text~~ ---
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # --- 9. Restore placeholders ---
    for key, html_content in placeholders:
        text = text.replace(key, html_content)

    return text
