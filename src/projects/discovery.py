"""Auto-discovery of new project directories.

Scans APPROVED_DIRECTORY for subdirectories not yet listed in projects.yaml
and adds them automatically.
"""

import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

import structlog
import yaml

logger = structlog.get_logger()

# Directories to skip during discovery (common non-project dirs)
SKIP_DIRS: Set[str] = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".eggs",
    "dist",
    "build",
    ".idea",
    ".vscode",
}


def _slugify(name: str) -> str:
    """Convert directory name to a URL-friendly slug.

    Underscores and any other non-alphanumeric characters collapse to hyphens
    (e.g. ``real_project`` -> ``real-project``), so slugs are consistently
    hyphenated. Leading/trailing separators are stripped.
    """
    slug = name.lower().strip().strip("_").strip("-")
    slug = re.sub(r"[^a-z0-9-]", "-", slug)
    slug = re.sub(r"[-]+", "-", slug)
    slug = slug.strip("-")
    return slug or name.lower()


def _make_display_name(dir_name: str) -> str:
    """Convert directory name to a human-readable display name.

    Preserves leading underscores (e.g. '_boss' -> '_boss').
    """
    return dir_name


def discover_new_projects(
    approved_directory: Path,
    config_path: Path,
) -> Tuple[List[Dict[str, str]], int]:
    """Scan approved_directory for new project directories.

    Reads the current projects.yaml, finds subdirectories of
    approved_directory that are not yet registered, appends them,
    and writes the updated file.

    Args:
        approved_directory: Root directory to scan.
        config_path: Path to projects.yaml.

    Returns:
        Tuple of (list of newly added project dicts, total project count).
    """
    approved_root = approved_directory.resolve()

    # Load current config
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}

    existing_projects: List[Dict[str, str]] = data.get("projects", [])

    # Collect existing paths (normalized) to avoid duplicates
    existing_paths: Set[str] = set()
    existing_slugs: Set[str] = set()
    existing_names: Set[str] = set()

    for proj in existing_projects:
        p = str(proj.get("path", "")).strip()
        if p:
            existing_paths.add(p)
        s = str(proj.get("slug", "")).strip()
        if s:
            existing_slugs.add(s)
        n = str(proj.get("name", "")).strip()
        if n:
            existing_names.add(n)

    # Scan first-level subdirectories
    new_projects: List[Dict[str, str]] = []

    if not approved_root.exists():
        logger.warning(
            "Approved directory does not exist, skipping discovery",
            path=str(approved_root),
        )
        return [], len(existing_projects)

    for entry in sorted(approved_root.iterdir()):
        if not entry.is_dir():
            continue

        dir_name = entry.name

        # Skip hidden dirs and known non-project dirs
        if dir_name.startswith(".") and not dir_name.startswith("_"):
            continue
        if dir_name in SKIP_DIRS:
            continue

        rel_path = dir_name  # first-level only

        if rel_path in existing_paths:
            continue

        slug = _slugify(dir_name)
        name = _make_display_name(dir_name)

        # Ensure slug uniqueness
        base_slug = slug
        counter = 2
        while slug in existing_slugs:
            slug = f"{base_slug}-{counter}"
            counter += 1

        # Ensure name uniqueness
        base_name = name
        counter = 2
        while name in existing_names:
            name = f"{base_name} ({counter})"
            counter += 1

        new_entry = {
            "slug": slug,
            "name": name,
            "path": rel_path,
            "enabled": True,
        }
        new_projects.append(new_entry)
        existing_paths.add(rel_path)
        existing_slugs.add(slug)
        existing_names.add(name)

    if not new_projects:
        logger.info("No new projects discovered")
        return [], len(existing_projects)

    # Append new projects and write back
    existing_projects.extend(new_projects)
    data["projects"] = existing_projects

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(
            data, f, default_flow_style=False, allow_unicode=True, sort_keys=False
        )

    logger.info(
        "New projects discovered and added to config",
        new_count=len(new_projects),
        new_slugs=[p["slug"] for p in new_projects],
        total=len(existing_projects),
    )

    return new_projects, len(existing_projects)
