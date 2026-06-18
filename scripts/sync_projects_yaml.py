#!/usr/bin/env python3
"""Sync config/projects.yaml with actual directories in APPROVED_DIRECTORY.

Adds new directories, removes entries for missing directories.
Preserves manually set slug/name/enabled for existing entries.
"""

import os
import re
import sys
from pathlib import Path, PurePosixPath

import yaml

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache"}
SKIP_PREFIXES = (".", "_")


def slug_from_path(rel_path: str) -> str:
    """Convert relative path to slug: 'site/univer' -> 'site-univer', 'update_fix' -> 'update-fix'."""
    return re.sub(r"[_/\\]+", "-", rel_path)


def name_from_path(rel_path: str) -> str:
    """Convert relative path to display name: 'update_fix' -> 'Update Fix'."""
    base = Path(rel_path).name
    return base.replace("_", " ").replace("-", " ").title()


def scan_directories(approved_dir: Path) -> list[str]:
    """Find top-level project directories (those with .git)."""
    results = []
    for entry in sorted(approved_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in SKIP_DIRS or entry.name.startswith(SKIP_PREFIXES):
            continue
        if (entry / ".git").exists():
            results.append(entry.name)
    return results


def sync(config_path: Path, approved_dir: Path) -> bool:
    """Sync projects.yaml with filesystem. Returns True if changes were made."""
    # Load existing config
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        existing = data.get("projects", []) or []
    else:
        existing = []

    # Index existing entries by path
    by_path: dict[str, dict] = {}
    for entry in existing:
        p = str(entry.get("path", "")).strip()
        if p:
            by_path[p] = entry

    # Scan top-level directories with .git
    actual_paths = set(scan_directories(approved_dir))

    # Collect existing nested paths (like site/lingva) that still exist on disk
    existing_nested = set()
    for p in by_path:
        if len(PurePosixPath(p).parts) > 1 and (approved_dir / p).is_dir():
            existing_nested.add(p)

    # Exclude top-level dirs that serve as parents for existing nested paths
    parent_dirs = {PurePosixPath(p).parts[0] for p in existing_nested}
    actual_paths -= parent_dirs

    all_paths = actual_paths | existing_nested

    # Build new list: keep existing entries for dirs that still exist, add new ones
    new_projects = []
    for rel_path in sorted(all_paths):
        if rel_path in by_path:
            new_projects.append(by_path[rel_path])
        else:
            new_projects.append(
                {
                    "slug": slug_from_path(rel_path),
                    "name": name_from_path(rel_path),
                    "path": rel_path,
                    "enabled": True,
                }
            )

    # Check for changes: compare full normalized content (fields + order + duplicates),
    # not just the set of paths.
    def _normalize(entry: dict) -> dict:
        return {
            "slug": str(entry.get("slug", "")),
            "name": str(entry.get("name", "")),
            "path": str(entry.get("path", "")),
            "enabled": bool(entry.get("enabled", True)),
        }

    old_norm = [_normalize(e) for e in existing]
    new_norm = [_normalize(e) for e in new_projects]

    if old_norm == new_norm:
        print("projects.yaml is up to date, no changes needed.")
        return False

    old_paths = {e["path"] for e in old_norm}
    new_paths = {e["path"] for e in new_norm}
    added = new_paths - old_paths
    removed = old_paths - new_paths

    if added:
        print(f"Added: {', '.join(sorted(added))}")
    if removed:
        print(f"Removed: {', '.join(sorted(removed))}")

    # Write updated config, preserving field schema and project order.
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"projects": new_norm}
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(
            payload,
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )

    print(f"Updated {config_path} ({len(new_projects)} projects)")
    return True


def main() -> None:
    # Resolve paths
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent
    config_path = project_root / "config" / "projects.yaml"

    approved_dir_env = os.getenv("APPROVED_DIRECTORY", "")
    if not approved_dir_env:
        # Try to read from .env
        env_file = project_root / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("APPROVED_DIRECTORY="):
                    approved_dir_env = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break

    if not approved_dir_env:
        print("ERROR: APPROVED_DIRECTORY not set", file=sys.stderr)
        sys.exit(1)

    approved_dir = Path(approved_dir_env)
    if not approved_dir.exists():
        print(f"ERROR: APPROVED_DIRECTORY does not exist: {approved_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning: {approved_dir}")
    changed = sync(config_path, approved_dir)
    sys.exit(2 if changed else 0)


if __name__ == "__main__":
    main()
