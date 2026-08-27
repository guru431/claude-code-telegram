"""Приём присланной ссылки: решить, что с ней делать, и подготовить промпт."""
import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import structlog

logger = structlog.get_logger()

FETCH_SOURCE = r"c:\AI\projects\_boss\cron\link-analysis\fetch_source.py"
PYTHON = r"C:\Program Files\Python314\python.exe"
WORK_ROOT = r"C:\AI\projects\_boss\cron\state\link-analysis\incoming"
PROJECT_REGISTRY = Path(r"c:\AI\projects\_boss\cron\project-registry.json")
CLASSIFY_TIMEOUT = 600

_URL_RE = re.compile(r"https?://\S+")


def looks_like_link_task(text: str) -> bool:
    """Сообщение, которое стоит вести по разбору материала."""
    return bool(_URL_RE.search(text or ""))


def project_path(name: str, *, registry: Path = PROJECT_REGISTRY) -> Optional[str]:
    """Каталог проекта по имени из реестра `_boss`. None, если имя незнакомо."""
    try:
        data = json.loads(registry.read_text(encoding="utf-8")).get("projects", {})
    except (OSError, json.JSONDecodeError):
        return None
    meta = data.get(name)
    return meta.get("path") if meta else None


def _classify(text: str, project_hint: Optional[str], runner) -> Dict[str, Any]:
    cmd = [PYTHON, FETCH_SOURCE, text, "--out", WORK_ROOT, "--classify"]
    if project_hint:
        cmd += ["--project", project_hint]
    r = runner(cmd, capture_output=True, timeout=CLASSIFY_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"").decode("utf-8", "replace")[:300])
    return json.loads((r.stdout or b"{}").decode("utf-8"))


async def handle(text: str, *, project_hint: Optional[str],
                 runner: Callable = subprocess.run) -> Dict[str, Any]:
    """Решение по присланной ссылке. Ничего не отправляет — только готовит."""
    decided = await asyncio.to_thread(_classify, text, project_hint, runner)

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
        await asyncio.to_thread(_enqueue, text, None, runner)
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
    await asyncio.to_thread(_enqueue, text, project, runner)
    return {
        "action": "analyze",
        "project": project,
        "prompt": f"/link-analysis {text}",
        "report": None,
        "receipt": f"Принял. Разбираю в проекте {project} — пришлю отчёт.",
    }


def _enqueue(text: str, project: Optional[str], runner) -> None:
    cmd = [PYTHON, FETCH_SOURCE, text, "--enqueue"]
    if project:
        cmd += ["--project", project]
    try:
        runner(cmd, capture_output=True, timeout=60)
    except Exception as exc:  # очередь — страховка, её отказ не рушит приём
        logger.warning("link_intake: не удалось поставить в очередь", error=str(exc))
