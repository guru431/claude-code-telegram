"""Format bot responses for optimal display."""

import re
from dataclasses import dataclass
from typing import List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ...config.settings import Settings
from .html_format import escape_html, markdown_to_telegram_html, tg_len


@dataclass
class FormattedMessage:
    """Represents a formatted message for Telegram."""

    text: str
    parse_mode: str = "HTML"
    reply_markup: Optional[InlineKeyboardMarkup] = None

    def __len__(self) -> int:
        """Return length of message text."""
        return len(self.text)


class ResponseFormatter:
    """Format Claude responses for Telegram display."""

    def __init__(self, settings: Settings):
        """Initialize formatter with settings."""
        self.settings = settings
        self.max_message_length = 4000  # Telegram limit is 4096, leave some buffer
        self.max_code_block_length = (
            15000  # Max length for individual code blocks before splitting
        )

    def format_claude_response(
        self, text: str, context: Optional[dict] = None
    ) -> List[FormattedMessage]:
        """Convert Claude's markdown to HTML and split tag-safely for Telegram."""
        # Clean and prepare text (markdown -> Telegram HTML)
        text = self._clean_text(text)

        # Truncate oversized code blocks, then split with the tag-aware splitter.
        # Splitting must run on the converted HTML so it never cuts inside a
        # <pre>/<b>/<a> tag (semantic chunking on markers no longer present in
        # the HTML would split mid-tag and trigger Telegram 400s).
        text = self._format_code_blocks(text)
        messages = self._split_message(text)

        # Add context-aware quick actions to the last message
        if messages and self.settings.enable_quick_actions:
            messages[-1].reply_markup = self._get_contextual_keyboard(context)

        # Filter out any empty messages produced by formatting/splitting
        messages = [m for m in messages if m.text and m.text.strip()]

        return (
            messages
            if messages
            else [FormattedMessage("<i>(No content to display)</i>")]
        )

    def format_error_message(
        self, error: str, error_type: str = "Error"
    ) -> FormattedMessage:
        """Format error message with appropriate styling."""
        icon = {
            "Error": "❌",
            "Warning": "⚠️",
            "Info": "ℹ️",
            "Security": "🛡️",
            "Rate Limit": "⏱️",
        }.get(error_type, "❌")

        text = f"{icon} <b>{escape_html(error_type)}</b>\n\n{escape_html(error)}"

        return FormattedMessage(text, parse_mode="HTML")

    def format_success_message(
        self, message: str, title: str = "Success"
    ) -> FormattedMessage:
        """Format success message with appropriate styling."""
        text = f"✅ <b>{escape_html(title)}</b>\n\n{escape_html(message)}"
        return FormattedMessage(text, parse_mode="HTML")

    def format_info_message(
        self, message: str, title: str = "Info"
    ) -> FormattedMessage:
        """Format info message with appropriate styling."""
        text = f"ℹ️ <b>{escape_html(title)}</b>\n\n{escape_html(message)}"
        return FormattedMessage(text, parse_mode="HTML")

    def format_code_output(
        self, output: str, language: str = "", title: str = "Output"
    ) -> List[FormattedMessage]:
        """Format code output with syntax highlighting."""
        if not output.strip():
            return [
                FormattedMessage(
                    f"📄 <b>{escape_html(title)}</b>\n\n<i>(empty output)</i>"
                )
            ]

        escaped_output = escape_html(output)

        # Check if the code block is too long
        if len(escaped_output) > self.max_code_block_length:
            escaped_output = (
                escape_html(output[: self.max_code_block_length - 100])
                + "\n... (output truncated)"
            )

        if language:
            code_block = f'<pre><code class="language-{escape_html(language)}">{escaped_output}</code></pre>'
        else:
            code_block = f"<pre><code>{escaped_output}</code></pre>"

        text = f"📄 <b>{escape_html(title)}</b>\n\n{code_block}"

        return self._split_message(text)

    def format_file_list(
        self, files: List[str], directory: str = ""
    ) -> FormattedMessage:
        """Format file listing with appropriate icons."""
        safe_dir = escape_html(directory)
        if not files:
            text = f"📂 <b>{safe_dir}</b>\n\n<i>(empty directory)</i>"
        else:
            file_lines = []
            for file in files[:50]:  # Limit to 50 items
                safe_file = escape_html(file)
                if file.endswith("/"):
                    file_lines.append(f"📁 {safe_file}")
                else:
                    file_lines.append(f"📄 {safe_file}")

            file_text = "\n".join(file_lines)
            if len(files) > 50:
                file_text += f"\n\n<i>... and {len(files) - 50} more items</i>"

            text = f"📂 <b>{safe_dir}</b>\n\n{file_text}"

        return FormattedMessage(text, parse_mode="HTML")

    def format_progress_message(
        self, message: str, percentage: Optional[float] = None
    ) -> FormattedMessage:
        """Format progress message with optional progress bar."""
        safe_msg = escape_html(message)
        if percentage is not None:
            # Create simple progress bar
            filled = int(percentage / 10)
            empty = 10 - filled
            progress_bar = "▓" * filled + "░" * empty
            text = f"🔄 <b>{safe_msg}</b>\n\n{progress_bar} {percentage:.0f}%"
        else:
            text = f"🔄 <b>{safe_msg}</b>"

        return FormattedMessage(text, parse_mode="HTML")

    def _get_contextual_keyboard(
        self, context: Optional[dict]
    ) -> Optional[InlineKeyboardMarkup]:
        """Get context-aware quick action keyboard."""
        if not context:
            return self._get_quick_actions_keyboard()

        buttons = []

        # Add context-specific buttons
        if context.get("has_code"):
            buttons.append(
                [InlineKeyboardButton("💾 Save Code", callback_data="save_code")]
            )

        if context.get("has_file_operations"):
            buttons.append(
                [InlineKeyboardButton("📁 Show Files", callback_data="show_files")]
            )

        if context.get("has_errors"):
            buttons.append([InlineKeyboardButton("🔧 Debug", callback_data="debug")])

        # Add default actions
        default_buttons = [
            [InlineKeyboardButton("🔄 Continue", callback_data="continue")],
            [InlineKeyboardButton("💡 Explain", callback_data="explain")],
        ]
        buttons.extend(default_buttons)

        return InlineKeyboardMarkup(buttons) if buttons else None

    def _clean_text(self, text: str) -> str:
        """Clean text for Telegram display."""
        # Remove excessive whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)

        # Convert markdown to Telegram HTML
        text = markdown_to_telegram_html(text)

        return text.strip()

    def _format_code_blocks(self, text: str) -> str:
        """Ensure code blocks are properly formatted for Telegram.

        With HTML mode, markdown_to_telegram_html already handles code blocks.
        This method now just truncates oversized code blocks.
        """

        def _truncate_code(m: re.Match) -> str:  # type: ignore[type-arg]
            full = m.group(0)
            if len(full) > self.max_code_block_length:
                # Re-extract and truncate the inner content
                inner = m.group(1)
                truncated = inner[: self.max_code_block_length - 80]
                return (
                    f"<pre><code>{escape_html(truncated)}\n... (truncated)</code></pre>"
                )
            return full

        return re.sub(
            r"<pre><code[^>]*>(.*?)</code></pre>",
            _truncate_code,
            text,
            flags=re.DOTALL,
        )

    def _split_long_line(self, line: str) -> List[str]:
        """Split a single overlong line into tag-safe chunks.

        Prefer breaking at the last whitespace that is not inside an HTML
        ``<...>`` tag or an ``&...;`` entity before the offset; fall back to a
        hard offset cut when no safe boundary exists. Length is measured in
        Telegram's UTF-16 units via ``tg_len``.
        """
        step = self.max_message_length - 100
        chunks: List[str] = []
        rest = line

        while tg_len(rest) > step:
            # Candidate hard cut at code-point offset `step` (UTF-16 length is
            # >= code-point length, so this stays within the budget).
            cut = step
            # Find the last whitespace before `cut` that is not inside a
            # `<...>` tag or a `&...;` entity.
            safe = -1
            in_tag = False
            in_entity = False
            for i, ch in enumerate(rest[:cut]):
                if ch == "<":
                    in_tag = True
                elif ch == ">":
                    in_tag = False
                elif ch == "&":
                    in_entity = True
                elif ch == ";":
                    in_entity = False
                elif ch.isspace() and not in_tag and not in_entity:
                    safe = i
            split_at = safe if safe > 0 else cut
            chunks.append(rest[:split_at])
            rest = rest[split_at:]

        if rest:
            chunks.append(rest)
        return chunks

    def _split_message(self, text: str) -> List[FormattedMessage]:
        """Split long messages while preserving formatting."""
        if not text or not text.strip():
            return []
        if tg_len(text) <= self.max_message_length:
            return [FormattedMessage(text)]

        messages = []
        current_lines: List[str] = []
        current_length = 0
        in_code_block = False

        lines = text.split("\n")

        for line in lines:
            line_length = tg_len(line) + 1  # +1 for newline

            # Track HTML <pre> code block state
            if "<pre>" in line or "<pre><code" in line:
                in_code_block = True
            if "</pre>" in line:
                in_code_block = False

            # If this is a very long line that exceeds limit by itself, split it
            if line_length > self.max_message_length:
                chunks = self._split_long_line(line)

                for chunk in chunks:
                    chunk_length = tg_len(chunk) + 1

                    if (
                        current_length + chunk_length > self.max_message_length
                        and current_lines
                    ):
                        if in_code_block:
                            current_lines.append("</code></pre>")
                        messages.append(FormattedMessage("\n".join(current_lines)))

                        current_lines = []
                        current_length = 0
                        if in_code_block:
                            current_lines.append("<pre><code>")
                            current_length = 12

                    current_lines.append(chunk)
                    current_length += chunk_length
                continue

            # Check if adding this line would exceed the limit
            if current_length + line_length > self.max_message_length and current_lines:
                if in_code_block:
                    current_lines.append("</code></pre>")

                messages.append(FormattedMessage("\n".join(current_lines)))

                current_lines = []
                current_length = 0

                if in_code_block:
                    current_lines.append("<pre><code>")
                    current_length = 12

            current_lines.append(line)
            current_length += line_length

        # Add remaining content
        if current_lines:
            messages.append(FormattedMessage("\n".join(current_lines)))

        return messages

    def _get_quick_actions_keyboard(self) -> InlineKeyboardMarkup:
        """Get quick actions inline keyboard."""
        keyboard = [
            [
                InlineKeyboardButton("🧪 Test", callback_data="quick:test"),
                InlineKeyboardButton("📦 Install", callback_data="quick:install"),
                InlineKeyboardButton("🎨 Format", callback_data="quick:format"),
            ],
            [
                InlineKeyboardButton("🔍 Find TODOs", callback_data="quick:find_todos"),
                InlineKeyboardButton("🔨 Build", callback_data="quick:build"),
                InlineKeyboardButton("📊 Git Status", callback_data="quick:git_status"),
            ],
        ]

        return InlineKeyboardMarkup(keyboard)

    def create_confirmation_keyboard(
        self, confirm_data: str, cancel_data: str = "confirm:no"
    ) -> InlineKeyboardMarkup:
        """Create a confirmation keyboard."""
        keyboard = [
            [
                InlineKeyboardButton("✅ Yes", callback_data=confirm_data),
                InlineKeyboardButton("❌ No", callback_data=cancel_data),
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    def create_navigation_keyboard(self, options: List[tuple]) -> InlineKeyboardMarkup:
        """Create navigation keyboard from options list.

        Args:
            options: List of (text, callback_data) tuples
        """
        keyboard = []
        current_row = []

        for text, callback_data in options:
            current_row.append(InlineKeyboardButton(text, callback_data=callback_data))

            # Create rows of 2 buttons
            if len(current_row) == 2:
                keyboard.append(current_row)
                current_row = []

        # Add remaining button if any
        if current_row:
            keyboard.append(current_row)

        return InlineKeyboardMarkup(keyboard)


class ProgressIndicator:
    """Helper for creating progress indicators."""

    @staticmethod
    def create_bar(
        percentage: float,
        length: int = 10,
        filled_char: str = "▓",
        empty_char: str = "░",
    ) -> str:
        """Create a progress bar."""
        filled = int((percentage / 100) * length)
        empty = length - filled
        return filled_char * filled + empty_char * empty

    @staticmethod
    def create_spinner(step: int) -> str:
        """Create a spinning indicator."""
        spinners = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        return spinners[step % len(spinners)]

    @staticmethod
    def create_dots(step: int) -> str:
        """Create a dots indicator."""
        dots = ["", ".", "..", "..."]
        return dots[step % len(dots)]


class CodeHighlighter:
    """Simple code highlighting for common languages."""

    # Language file extensions mapping
    LANGUAGE_EXTENSIONS = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".jsx": "javascript",
        ".tsx": "typescript",
        ".java": "java",
        ".cpp": "cpp",
        ".c": "c",
        ".cs": "csharp",
        ".go": "go",
        ".rs": "rust",
        ".rb": "ruby",
        ".php": "php",
        ".swift": "swift",
        ".kt": "kotlin",
        ".scala": "scala",
        ".sh": "bash",
        ".bash": "bash",
        ".zsh": "bash",
        ".sql": "sql",
        ".json": "json",
        ".xml": "xml",
        ".html": "html",
        ".css": "css",
        ".scss": "scss",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".md": "markdown",
    }

    @classmethod
    def detect_language(cls, filename: str) -> str:
        """Detect programming language from filename."""
        from pathlib import Path

        ext = Path(filename).suffix.lower()
        return cls.LANGUAGE_EXTENSIONS.get(ext, "")

    @classmethod
    def format_code(cls, code: str, language: str = "", filename: str = "") -> str:
        """Format code with language detection, using HTML tags."""
        if not language and filename:
            language = cls.detect_language(filename)

        escaped_code = escape_html(code)
        if language:
            return f'<pre><code class="language-{escape_html(language)}">{escaped_code}</code></pre>'
        else:
            return f"<pre><code>{escaped_code}</code></pre>"
