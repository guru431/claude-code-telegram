"""Приём присланной ссылки: решить, что с ней делать, и подготовить промпт."""

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import structlog

logger = structlog.get_logger()

CLASSIFY_TIMEOUT = 600

_URL_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class LinkIntakeConfig:
    """Где живёт (внешний по отношению к репозиторию) пайплайн link-analysis.

    Умышленно без значений по умолчанию. Добытчик — внешний инструмент, его
    раскладка различается от машины к машине; зашитые абсолютные пути одного
    развёртывания и утекали в публичный репозиторий, и делали фичу мёртвой
    везде, кроме одной машины: каждая ссылка запускала subprocess по
    несуществующему скрипту и возвращалась как «не смог добыть материал».
    ``Settings`` требует все четыре значения при ``ENABLE_LINK_INTAKE`` и
    проверяет существование скрипта на старте.
    """

    python: Path
    fetch_script: Path
    work_root: Path
    registry: Path

    @classmethod
    def from_settings(cls, settings: Any) -> "LinkIntakeConfig":
        """Собрать конфиг из ``Settings`` (поля уже провалидированы на старте)."""
        return cls(
            python=Path(settings.link_intake_python),
            fetch_script=Path(settings.link_intake_fetch_script),
            work_root=Path(settings.link_intake_work_root),
            registry=Path(settings.link_intake_registry),
        )


def looks_like_link_task(text: str) -> bool:
    """Сообщение, которое стоит вести по разбору материала."""
    return bool(_URL_RE.search(text or ""))


def project_path(name: str, *, registry: Path) -> Optional[str]:
    """Каталог проекта по имени из реестра. None, если имя незнакомо."""
    try:
        data = json.loads(registry.read_text(encoding="utf-8")).get("projects", {})
    except (OSError, json.JSONDecodeError):
        return None
    meta = data.get(name)
    return meta.get("path") if meta else None


def _classify(
    text: str,
    project_hint: Optional[str],
    runner: Callable,
    config: LinkIntakeConfig,
) -> Dict[str, Any]:
    cmd = [
        str(config.python),
        str(config.fetch_script),
        text,
        "--out",
        str(config.work_root),
        "--classify",
    ]
    if project_hint:
        cmd += ["--project", project_hint]
    r = runner(cmd, capture_output=True, timeout=CLASSIFY_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"").decode("utf-8", "replace")[:300])
    return json.loads((r.stdout or b"{}").decode("utf-8"))


async def handle(
    text: str,
    *,
    project_hint: Optional[str],
    config: LinkIntakeConfig,
    runner: Callable = subprocess.run,
) -> Dict[str, Any]:
    """Решение по присланной ссылке. Ничего не отправляет — только готовит."""
    decided = await asyncio.to_thread(_classify, text, project_hint, runner, config)

    dup = decided.get("duplicate")
    if dup:
        return {
            "action": "duplicate",
            "project": dup.get("project"),
            "prompt": None,
            "report": dup.get("report"),
            "receipt": (
                f"Этот материал уже разбирался {dup.get('date')} "
                f"в проекте {dup.get('project')}: {dup.get('report')}. "
                "Разобрать заново с новым фокусом?"
            ),
        }

    project = decided.get("project")
    if not project:
        await asyncio.to_thread(_enqueue, text, None, runner, config)
        return {
            "action": "ask_project",
            "project": None,
            "prompt": None,
            "report": None,
            "receipt": "Принял ссылку, но не понял, к какому проекту она относится.",
        }

    # В очередь ставим ДО запуска разбора: если окно Claude исчерпано и разбор
    # не состоится, ссылка не потеряется — её добьёт ClaudeLinkQueue. Успешный
    # разбор фиксируется в реестре, и очередь снимет запись сама.
    await asyncio.to_thread(_enqueue, text, project, runner, config)
    return {
        "action": "analyze",
        "project": project,
        "prompt": f"/link-analysis {text}",
        "report": None,
        "receipt": f"Принял. Разбираю в проекте {project} — пришлю отчёт.",
    }


def _enqueue(
    text: str,
    project: Optional[str],
    runner: Callable,
    config: LinkIntakeConfig,
) -> None:
    cmd = [str(config.python), str(config.fetch_script), text, "--enqueue"]
    if project:
        cmd += ["--project", project]
    try:
        runner(cmd, capture_output=True, timeout=60)
    except Exception as exc:  # очередь — страховка, её отказ не рушит приём
        logger.warning("link_intake: не удалось поставить в очередь", error=str(exc))
