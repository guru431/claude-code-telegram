"""YAML-backed project registry for thread mode."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union

import structlog
import yaml

logger = structlog.get_logger()

_TRUE_VALUES = frozenset({"true", "yes", "y", "on", "1"})
_FALSE_VALUES = frozenset({"false", "no", "n", "off", "0"})

# Whether the platform's filesystem treats 'Foo' and 'foo' as the same
# directory. Only there may a path key be case-folded: on a case-sensitive
# filesystem 'Foo' and 'foo' are two valid, distinct project directories, and
# folding them onto one key makes discovery silently skip one of them and the
# registry reject the pair as a duplicate path.
_CASE_INSENSITIVE_FS = os.name == "nt" or sys.platform == "darwin"


def canonical_path_key(path: Union[str, Path]) -> str:
    """Return the identity key for a filesystem path.

    Single source of truth for "are these two paths the same directory",
    shared by the registry's duplicate check and discovery's dedupe so both
    writers of projects.yaml agree. Case is folded only on case-insensitive
    platforms (see :data:`_CASE_INSENSITIVE_FS`).
    """
    text = os.path.normcase(os.fspath(path))
    return text.casefold() if _CASE_INSENSITIVE_FS else text


def parse_enabled(value: object, context: str) -> bool:
    """Parse an ``enabled`` flag strictly.

    ``bool(value)`` is wrong here: YAML quoting turns ``enabled: "false"`` into
    the non-empty string ``"false"``, which is truthy, so a project the user
    disabled would silently stay enabled. Only recognised spellings are
    accepted; anything else is a config error rather than a silent default.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)

    raise ValueError(
        f"{context} has invalid 'enabled' value {value!r}; "
        f"expected a boolean (true/false)"
    )


@dataclass(frozen=True)
class ProjectDefinition:
    """Project entry from YAML configuration."""

    slug: str
    name: str
    relative_path: Path
    absolute_path: Path
    enabled: bool = True


class ProjectRegistry:
    """In-memory validated project registry."""

    def __init__(self, projects: List[ProjectDefinition]) -> None:
        self._projects = projects
        self._by_slug: Dict[str, ProjectDefinition] = {p.slug: p for p in projects}

    @property
    def projects(self) -> List[ProjectDefinition]:
        """Return all projects."""
        return list(self._projects)

    def list_enabled(self) -> List[ProjectDefinition]:
        """Return enabled projects only."""
        return [p for p in self._projects if p.enabled]

    def get_by_slug(self, slug: str) -> Optional[ProjectDefinition]:
        """Get project by slug."""
        return self._by_slug.get(slug)


def load_project_registry(
    config_path: Path, approved_directory: Path
) -> ProjectRegistry:
    """Load and validate project definitions from YAML."""
    if not config_path.exists():
        raise ValueError(f"Projects config file does not exist: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError("Projects config must be a YAML object")

    raw_projects = data.get("projects")
    if not isinstance(raw_projects, list) or not raw_projects:
        raise ValueError("Projects config must contain a non-empty 'projects' list")

    approved_root = approved_directory.resolve()
    seen_slugs = set()
    seen_names = set()
    seen_abs_paths: set[str] = set()
    projects: List[ProjectDefinition] = []

    for idx, raw in enumerate(raw_projects):
        if not isinstance(raw, dict):
            raise ValueError(f"Project entry at index {idx} must be an object")

        slug = str(raw.get("slug", "")).strip()
        name = str(raw.get("name", "")).strip()
        rel_path_raw = str(raw.get("path", "")).strip()
        enabled = parse_enabled(
            raw.get("enabled", True), f"Project entry at index {idx}"
        )

        if not slug:
            raise ValueError(f"Project entry at index {idx} is missing 'slug'")
        if not name:
            raise ValueError(f"Project '{slug}' is missing 'name'")
        if not rel_path_raw:
            raise ValueError(f"Project '{slug}' is missing 'path'")

        rel_path = Path(rel_path_raw)
        if rel_path.is_absolute():
            raise ValueError(f"Project '{slug}' path must be relative: {rel_path_raw}")

        absolute_path = (approved_root / rel_path).resolve()

        try:
            absolute_path.relative_to(approved_root)
        except ValueError as e:
            raise ValueError(
                f"Project '{slug}' path outside approved " f"directory: {rel_path_raw}"
            ) from e

        if not absolute_path.exists() or not absolute_path.is_dir():
            logger.warning(
                "Project path does not exist or is not a directory, "
                "skipping stale project",
                slug=slug,
                path=str(absolute_path),
            )
            continue

        abs_path_key = canonical_path_key(absolute_path)
        if slug in seen_slugs:
            raise ValueError(f"Duplicate project slug: {slug}")
        if name in seen_names:
            raise ValueError(f"Duplicate project name: {name}")
        if abs_path_key in seen_abs_paths:
            raise ValueError(f"Duplicate project path: {rel_path_raw}")

        seen_slugs.add(slug)
        seen_names.add(name)
        seen_abs_paths.add(abs_path_key)

        projects.append(
            ProjectDefinition(
                slug=slug,
                name=name,
                relative_path=rel_path,
                absolute_path=absolute_path,
                enabled=enabled,
            )
        )

    return ProjectRegistry(projects)
