"""Bounded, guarded document lookup must work without command execution."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agents.tools import ProjectTools
from src.claude.exceptions import ClaudeProcessError
from src.security.validators import SecurityValidator


@pytest.fixture
def tools(tmp_path):
    rpc = SimpleNamespace(request=AsyncMock())
    return ProjectTools(
        rpc, SecurityValidator(tmp_path), tmp_path, tmp_path, False, False
    )


async def test_read_file_pages_and_bounds_output_without_shell(tools, tmp_path):
    (tmp_path / "trip.md").write_text(
        "\n".join(f"row {i}" for i in range(1, 301)), encoding="utf-8"
    )
    result = await tools.call(
        "read_file", {"path": "trip.md", "start_line": 121, "max_lines": 2}
    )
    assert result["success"]
    assert result["contentItems"][0]["text"] == (
        "trip.md: lines 121-122 of 300\n121: row 121\n122: row 122"
        "\n[Continue with start_line=123]"
    )
    (tmp_path / "long.md").write_text("x" * 20_000, encoding="utf-8")
    result = await tools.call("read_file", {"path": "long.md"})
    assert len(result["contentItems"][0]["text"]) < 13_000
    tools.rpc.request.assert_not_called()


async def test_search_returns_evidence_and_skips_secrets_and_outside_paths(
    tools, tmp_path
):
    (tmp_path / "ticket.md").write_text(
        "Sichuan Airlines\nChecked BAGGAGE: 23 kg\nCabin: 8 kg\n", encoding="utf-8"
    )
    for folder in (".ssh", ".git"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "hidden.md").write_text(
            "baggage secret-canary", encoding="utf-8"
        )
    (tmp_path / ".env").write_text("baggage secret-canary", encoding="utf-8")
    result = await tools.call(
        "search_files", {"path": ".", "query": "baggage", "file_glob": "*"}
    )
    text = result["contentItems"][0]["text"]
    assert result["success"] and "ticket.md:2: Checked BAGGAGE: 23 kg" in text
    assert "Cabin: 8 kg" in text and "secret-canary" not in text
    for path in (".env", "../outside.md"):
        assert not (
            await tools.call("search_files", {"path": path, "query": "baggage"})
        )["success"]
    tools.rpc.request.assert_not_called()


async def test_search_is_literal_and_stops_at_the_requested_match_limit(
    tools, tmp_path
):
    (tmp_path / "notes.md").write_text("[fare]\nsecond [fare]\n", encoding="utf-8")
    result = await tools.call(
        "search_files", {"path": ".", "query": "[fare]", "max_results": 1}
    )
    assert result["success"]
    assert "[Match limit reached]" in result["contentItems"][0]["text"]
    tools.allowed_tools_override = ["Grep"]
    assert {spec["name"] for spec in tools.specs()} == {"search_files"}
    tools.disallowed_tools = ["Grep(secret:*)"]
    assert not (await tools.call("search_files", {"path": ".", "query": "[fare]"}))[
        "success"
    ]


@pytest.mark.parametrize(
    "options", [{"start_line": True}, {"start_line": 0}, {"max_lines": 401}]
)
async def test_read_rejects_invalid_page_arguments(tools, options):
    assert not (await tools.call("read_file", {"path": "trip.md", **options}))[
        "success"
    ]


async def test_sandbox_failure_does_not_trigger_more_shell_attempts(tools):
    tools.rpc.request.side_effect = ClaudeProcessError(
        "exec failed: windows sandbox: timed out connecting runner pipe-in"
    )
    first = await tools.call("run_command", {"command": "pwd"})
    second = await tools.call("run_command", {"command": "git status"})
    assert not first["success"] and not second["success"]
    assert "search_files" in first["contentItems"][0]["text"]
    assert "runner pipe-in" in first["contentItems"][0]["text"]
    tools.rpc.request.assert_awaited_once()


async def test_unicode_lookup_finds_a_fragment_inside_a_long_line(tools, tmp_path):
    (tmp_path / "trip.md").write_text(
        "x" * 20_000 + "Багаж: 23 кг; ручная кладь 8 кг." + "y" * 20_000,
        encoding="utf-8",
    )
    result = await tools.call("search_files", {"path": "trip.md", "query": "багаж"})
    assert result["success"]
    text = result["contentItems"][0]["text"]
    assert "Багаж: 23 кг; ручная кладь 8 кг." in text
    assert len(text) < 1500


async def test_truncated_page_reports_the_actual_last_line_and_continuation(
    tools, tmp_path
):
    (tmp_path / "guide.md").write_text(("x" * 5000 + "\n") * 4, encoding="utf-8")
    result = await tools.call("read_file", {"path": "guide.md"})
    text = result["contentItems"][0]["text"]
    assert "lines 1-2 of 4" in text and "start_line=3" in text
    result = await tools.call("read_file", {"path": "guide.md", "start_line": 3})
    assert "lines 3-4 of 4" in result["contentItems"][0]["text"]


async def test_command_output_is_bounded_before_it_reaches_the_model(tools):
    tools.rpc.request.return_value = {
        "exitCode": 0,
        "stdout": "x" * 30_000,
        "stderr": "y" * 30_000,
    }
    result = await tools.call("run_command", {"command": "pwd"})
    assert result["success"]
    assert len(result["contentItems"][0]["text"]) < 13_000
    assert "Output truncated" in result["contentItems"][0]["text"]


async def test_search_paths_are_relative_to_working_directory_not_outer_boundary(
    tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "ticket.md").write_text("Baggage 23 kg", encoding="utf-8")
    tools = ProjectTools(
        None, SecurityValidator(tmp_path), project, tmp_path, True, False
    )
    result = await tools.call("search_files", {"path": ".", "query": "baggage"})
    assert result["contentItems"][0]["text"] == "ticket.md:1: Baggage 23 kg"
