"""Auto-discovery of new project directories.

Scans APPROVED_DIRECTORY for subdirectories not yet listed in projects.yaml
and adds them automatically.
"""

import os
import re
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

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


def slugify(name: str) -> str:
    """Convert a directory name or relative path to a URL-friendly slug.

    Underscores, path separators and any other non-alphanumeric characters
    collapse to hyphens (e.g. ``real_project`` -> ``real-project``,
    ``site/univer`` -> ``site-univer``), so slugs are consistently hyphenated.
    Leading/trailing separators are stripped.

    This is the single source of truth for slug generation: both auto-discovery
    and ``scripts/sync_projects_yaml.py`` use it so the same directory always
    yields the same slug. Slugs already present in projects.yaml are never
    recomputed (both writers dedupe by ``path``), so existing entries such as
    ``digital_me`` keep their historical slug.
    """
    slug = name.lower().strip().strip("_").strip("-")
    slug = re.sub(r"[^a-z0-9-]", "-", slug)
    slug = re.sub(r"[-]+", "-", slug)
    slug = slug.strip("-")
    return slug or name.lower()


def path_dedupe_key(approved_root: Path, rel_path: str) -> Optional[str]:
    """Return a canonical key identifying the directory ``rel_path`` points at.

    Registry paths are hand-editable, so the same directory can be spelled
    several ways: ``foo``, ``./foo``, ``Foo``, ``site\\lingva``, ``a/../foo``.
    Comparing the raw strings lets a second entry for the same directory slip
    into projects.yaml, which then makes ``load_project_registry`` raise
    "Duplicate project path" and takes the bot down on the next startup.

    Resolving and casefolding collapses all those spellings onto one key.
    Casefold is required because Windows paths are case-insensitive and
    ``Path.resolve()`` only restores the on-disk casing for paths that already
    exist.

    Returns ``None`` for paths that must never be registered: absolute paths
    and anything resolving to (or outside of) ``approved_root``.
    """
    raw = rel_path.strip().replace("\\", "/")
    if not raw:
        return None

    candidate = Path(raw)
    if candidate.is_absolute():
        return None

    resolved = (approved_root / candidate).resolve()
    if resolved == approved_root:
        return None
    try:
        resolved.relative_to(approved_root)
    except ValueError:
        return None

    return str(resolved).casefold()


def iter_project_dirs(approved_root: Path) -> Iterator[Path]:
    """Yield first-level directories of ``approved_root`` that may be projects.

    Single source of truth for "which directories are scan candidates", shared
    with ``scripts/sync_projects_yaml.py`` so both writers of projects.yaml
    agree on what exists. Skips symlinks (they alias a directory that is either
    already registered under its real path or lives outside the approved root),
    dot-directories and known non-project directories.

    Callers may narrow the result further; the sync script additionally
    requires a ``.git`` directory before registering a *new* project.
    """
    if not approved_root.exists():
        return

    for entry in sorted(approved_root.iterdir()):
        if entry.is_symlink():
            continue
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        if entry.name in SKIP_DIRS:
            continue
        yield entry


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

    # Collect existing paths (resolved + casefolded) to avoid duplicates. The
    # registry dedupes by resolved absolute path, so discovery must use the same
    # notion of identity or it will append an entry that breaks the next load.
    existing_paths: Set[str] = set()
    existing_slugs: Set[str] = set()
    existing_names: Set[str] = set()

    for proj in existing_projects:
        p = str(proj.get("path", "")).strip()
        if p:
            key = path_dedupe_key(approved_root, p)
            if key:
                existing_paths.add(key)
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

    for entry in iter_project_dirs(approved_root):
        dir_name = entry.name
        rel_path = dir_name  # first-level only

        path_key = path_dedupe_key(approved_root, rel_path)
        if path_key is None or path_key in existing_paths:
            continue

        slug = slugify(dir_name)
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
        existing_paths.add(path_key)
        existing_slugs.add(slug)
        existing_names.add(name)

    if not new_projects:
        logger.info("No new projects discovered")
        return [], len(existing_projects)

    # Append new projects and write back
    existing_projects.extend(new_projects)
    data["projects"] = existing_projects

    # Atomic write: dump to a temp file in the same dir, then os.replace. A crash
    # mid-write must not leave a truncated projects.yaml that fails to load on the
    # next startup (which would take the bot down).
    tmp_path = config_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.dump(
            data, f, default_flow_style=False, allow_unicode=True, sort_keys=False
        )
    os.replace(tmp_path, config_path)

    logger.info(
        "New projects discovered and added to config",
        new_count=len(new_projects),
        new_slugs=[p["slug"] for p in new_projects],
        total=len(existing_projects),
    )

    return new_projects, len(existing_projects)
