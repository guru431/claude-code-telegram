"""Regression cases reproduced while closing the review backlog."""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.agents.codex import CodexRPC
from src.bot.features.file_handler import FileHandler
from src.bot.handlers.message import _format_error_message
from src.bot.orchestrator import MessageOrchestrator
from src.claude.monitor import check_bash_directory_boundary
from src.exceptions import ClaudeError, ClaudeProcessError
from src.projects.discovery import discover_new_projects
from src.security.audit import AuditLogger
from src.security.secret_patterns import redact_secrets


@pytest.fixture
def workspace(tmp_path):
    with TemporaryDirectory(dir=tmp_path) as directory:
        yield Path(directory)


@pytest.mark.parametrize("document", ["[]", "false", "0", "plain text"])
def test_discovery_rejects_non_mapping_without_rewriting(workspace, document):
    config = workspace / "projects.yaml"
    config.write_text(document, encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        discover_new_projects(workspace, config)
    assert config.read_text(encoding="utf-8") == document


@pytest.mark.parametrize(
    "program",
    [
        'BEGIN { getline line < "/outside/private.txt"; print line }',
        'BEGIN { file="/outside/private.txt"; getline line < file; print line }',
    ],
)
def test_awk_cannot_hide_input_paths(workspace, program):
    valid, _ = check_bash_directory_boundary(f"awk '{program}'", workspace, workspace)
    assert not valid


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32", "latin-1"])
def test_uploaded_text_decodes_supported_encodings(workspace, encoding):
    upload = workspace / "notes.txt"
    upload.write_bytes("café\nsecond line".encode(encoding))
    handler = FileHandler(Mock(), Mock())
    assert handler._detect_file_type(upload) == "text"
    assert handler._read_inline(upload) == "café\nsecond line"


def test_todo_scan_ignores_generated_files_and_identifier_substrings(workspace):
    (workspace / "main.py").write_text(
        "# TODO: fix\n# FIXME: repair\nTODOS = 2\nFIXME_COUNTER = 1\n",
        encoding="utf-8",
    )
    generated = workspace / "node_modules"
    generated.mkdir()
    (generated / "bundle.js").write_text("// TODO: generated", encoding="utf-8")
    assert FileHandler(Mock(), Mock())._find_todos(workspace) == 2


@pytest.mark.parametrize("error_type", [ClaudeError, ClaudeProcessError])
def test_long_errors_never_split_html_entities(error_type):
    result = _format_error_message(error_type("x" * 499 + "&" + "z" * 30))
    assert "x" * 499 + "&amp;..." in result


def test_program_files_risk_uses_directory_boundary():
    logger = AuditLogger(Mock())
    assert logger._assess_file_access_risk("C:/Program Filesx/app.txt", "read") == "low"
    assert (
        logger._assess_file_access_risk("C:/Program Files/app.txt", "read") == "medium"
    )


@pytest.mark.parametrize(
    "name,key",
    [
        ("Bash", "command"),
        ("WebSearch", "query"),
        ("Task", "description"),
        ("Other", "value"),
    ],
)
def test_tool_summaries_redact_before_truncating(name, key):
    token = "-----BEGIN " + "PRIVATE KEY-----\n" + "abcdef" * 80
    token += "\n-----END " + "PRIVATE KEY-----"
    result = MessageOrchestrator._summarize_tool_input(name, {key: token})
    assert result == redact_secrets(token)[: 80 if name == "Bash" else 60]


async def test_codex_reader_recovers_after_invalid_stdout(workspace):
    rpc = CodexRPC(["codex"], workspace, {})
    reader = asyncio.StreamReader()
    reply = asyncio.get_running_loop().create_future()
    rpc.pending[1] = reply
    reader.feed_data(b"library warning\n[]\n")
    reader.feed_data((json.dumps({"id": 1, "result": "ok"}) + "\n").encode())
    reader.feed_eof()
    rpc.process = SimpleNamespace(stdout=reader)
    await rpc._read_stdout()
    assert await reply == "ok"
    assert rpc.events.get_nowait() == {"method": "_process/exited"}
    assert rpc.events.empty()


@pytest.mark.integration
@pytest.mark.parametrize(
    "patterns,blocked",
    [("[\n", True), ("ordinary\n", True), ("unrelated\n", False), (" \n\n", False)],
)
def test_personal_patterns_fail_closed(workspace, monkeypatch, patterns, blocked):
    """Exercise the actual shell hook against an isolated staging area."""
    if os.name == "nt":
        executable = str(
            Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            / "Git/bin/bash.exe"
        )
    else:
        executable = shutil.which("sh")
        if not executable:
            pytest.skip("A POSIX shell is required")
    monkeypatch.setenv("TMPDIR", str(workspace))
    subprocess.run(
        ["git", "init", "-q", str(workspace)],
        check=True,
        capture_output=True,
        timeout=5,
    )
    (workspace / "notes.txt").write_text("ordinary text\n", encoding="utf-8")
    (workspace / ".sanitize-patterns").write_text(patterns, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(workspace), "add", "notes.txt"],
        check=True,
        capture_output=True,
        timeout=5,
    )
    hook = Path(__file__).resolve().parents[2] / ".githooks/pre-commit"
    result = subprocess.run(
        [executable, str(hook)],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert (result.returncode != 0) is blocked
    assert ("BLOCKED" in result.stdout) is blocked
