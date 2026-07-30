"""Tests for reading session previews out of Claude's JSONL transcripts."""

import json

from src.claude import local_sessions
from src.claude.local_sessions import (
    _encode_path,
    _parse_session_tail,
    load_session_previews,
)


def _write_jsonl(path, entries) -> None:
    path.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n",
        encoding="utf-8",
    )


def _user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _tool_result(output: str) -> dict:
    """Tool results arrive as ``type: user`` entries carrying no prompt text."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": output}
            ],
        },
    }


class TestParseSessionTail:
    def test_returns_the_last_user_prompt(self, tmp_path):
        f = tmp_path / "s.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "summary", "cwd": str(tmp_path), "timestamp": "2026-01-01"},
                _user("first prompt"),
                _assistant("working on it"),
                _user("second prompt"),
                _assistant("done"),
            ],
        )

        assert _parse_session_tail(f) == "second prompt"

    def test_skips_tool_results(self, tmp_path):
        """A trailing tool loop must not be shown as the user's last message."""
        f = tmp_path / "s.jsonl"
        _write_jsonl(
            f,
            [
                _user("run the tests"),
                _assistant("running"),
                _tool_result("42 passed"),
                _tool_result("all green"),
            ],
        )

        assert _parse_session_tail(f) == "run the tests"

    def test_truncates_long_prompts(self, tmp_path):
        f = tmp_path / "s.jsonl"
        _write_jsonl(f, [_user("x" * 200)])

        assert _parse_session_tail(f) == "x" * local_sessions._PREVIEW_CHARS

    def test_reads_only_the_tail(self, tmp_path, monkeypatch):
        """The scan window is bounded, so an early prompt beyond it is invisible."""
        monkeypatch.setattr(local_sessions, "_TAIL_SCAN_BYTES", 200)
        f = tmp_path / "s.jsonl"
        _write_jsonl(
            f,
            [_user("ancient prompt")] + [_assistant("filler " * 20) for _ in range(5)],
        )

        assert _parse_session_tail(f) == ""

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert _parse_session_tail(tmp_path / "nope.jsonl") == ""


class TestLoadSessionPreviews:
    def test_finds_the_transcript_for_a_known_session(self, tmp_path, monkeypatch):
        projects = tmp_path / "projects"
        cwd = tmp_path / "work"
        session_dir = projects / _encode_path(cwd)
        session_dir.mkdir(parents=True)
        _write_jsonl(
            session_dir / "abc-123.jsonl",
            [
                {"type": "summary", "cwd": str(cwd), "timestamp": "2026-01-01"},
                _user("opening question"),
                _assistant("answer"),
                _user("closing question"),
            ],
        )
        monkeypatch.setattr(local_sessions, "_claude_projects_dir", lambda: projects)

        first, last = load_session_previews(cwd, "abc-123")

        assert first == "opening question"
        assert last == "closing question"

    def test_unknown_session_returns_empty_previews(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            local_sessions, "_claude_projects_dir", lambda: tmp_path / "projects"
        )

        assert load_session_previews(tmp_path, "missing") == ("", "")
