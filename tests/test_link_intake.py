import json

import pytest

from src.bot.features import link_intake


def test_plain_text_is_not_a_link_task():
    assert link_intake.looks_like_link_task("почему упал деплой?") is False


def test_message_with_link_is_a_link_task():
    assert link_intake.looks_like_link_task(
        "проанализируй это видео https://youtu.be/X"
    ) is True


@pytest.mark.asyncio
async def test_known_project_goes_straight_to_analysis():
    def runner(cmd, **kw):
        class R:
            returncode = 0
            stdout = json.dumps({"project": "vllm", "confidence": 0.9,
                                 "dir": "/tmp/x", "duplicate": None}).encode()
            stderr = b""

        return R()

    res = await link_intake.handle("https://youtu.be/X про кванты",
                                   project_hint="vllm", runner=runner)
    assert res["action"] == "analyze"
    assert res["project"] == "vllm"
    assert res["prompt"].startswith("/link-analysis")
    assert "vllm" in res["receipt"]


@pytest.mark.asyncio
async def test_unknown_project_asks_instead_of_guessing():
    def runner(cmd, **kw):
        class R:
            returncode = 0
            stdout = json.dumps({"project": None, "confidence": 0.2,
                                 "dir": "/tmp/x", "duplicate": None}).encode()
            stderr = b""

        return R()

    res = await link_intake.handle("https://youtu.be/X", project_hint=None,
                                   runner=runner)
    assert res["action"] == "ask_project"
    assert res["project"] is None


@pytest.mark.asyncio
async def test_duplicate_link_reports_previous_report():
    def runner(cmd, **kw):
        class R:
            returncode = 0
            stdout = json.dumps({
                "project": "_boss", "confidence": 1.0, "dir": "/tmp/x",
                "duplicate": {"report": "docs/analysis/2026-08-17-x.md",
                              "date": "2026-08-17", "project": "_boss"},
            }).encode()
            stderr = b""

        return R()

    res = await link_intake.handle("https://youtu.be/oDxODi3X2Mg",
                                   project_hint=None, runner=runner)
    assert res["action"] == "duplicate"
    assert res["report"] == "docs/analysis/2026-08-17-x.md"
    assert "2026-08-17" in res["receipt"]


@pytest.mark.asyncio
async def test_accepted_link_is_queued_before_analysis():
    """Очередь заполняется ДО разбора: иначе исчерпанное окно теряет ссылку."""
    seen: list[list[str]] = []

    def runner(cmd, **kw):
        seen.append(cmd)

        class R:
            returncode = 0
            stdout = json.dumps({"project": "vllm", "confidence": 0.9,
                                 "dir": "/tmp/x", "duplicate": None}).encode()
            stderr = b""

        return R()

    await link_intake.handle("https://youtu.be/X", project_hint="vllm",
                             runner=runner)
    assert any("--enqueue" in cmd for cmd in seen)


def test_project_path_resolves_registry_name(tmp_path):
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"projects": {"vllm": {"path": "c:/AI/projects/vLLM"}}}),
                   encoding="utf-8")
    assert link_intake.project_path("vllm", registry=reg) == "c:/AI/projects/vLLM"
    assert link_intake.project_path("нет-такого", registry=reg) is None
