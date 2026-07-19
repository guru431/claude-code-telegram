#!/usr/bin/env python3
"""Sync config/projects.yaml with actual directories in APPROVED_DIRECTORY.

Additive by default: adds newly discovered directories and never drops an entry
whose directory still exists on disk. Pass --prune to also drop entries whose
directory is gone. Preserves manually set slug/name/enabled for existing entries.
"""

import argparse
import os
import sys
from pathlib import Path, PurePosixPath

import yaml

# Run standalone (python scripts/sync_projects_yaml.py) but still share the
# canonical slug generator and directory scanner with src/projects/discovery.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.projects.discovery import iter_project_dirs  # noqa: E402
from src.projects.discovery import slugify as slug_from_path  # noqa: E402
from src.projects.registry import parse_enabled  # noqa: E402


def name_from_path(rel_path: str) -> str:
    """Convert relative path to display name: 'update_fix' -> 'Update Fix'."""
    base = Path(rel_path).name
    return base.replace("_", " ").replace("-", " ").title()


def scan_directories(approved_dir: Path) -> list[str]:
    """Find top-level project directories (those with .git).

    Candidate directories come from the shared scanner in discovery.py so both
    writers of projects.yaml agree on what exists; requiring .git is this
    script's own extra filter for *registering new* projects.
    """
    return [
        entry.name
        for entry in iter_project_dirs(approved_dir)
        if (entry / ".git").exists()
    ]


def sync(config_path: Path, approved_dir: Path, prune: bool = False) -> bool:
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

    # Every existing entry whose directory is still on disk is preserved. The
    # scanner deliberately does not see every valid entry (nested paths, dirs
    # without .git), so dropping whatever it missed would silently delete live
    # projects -- it previously removed _boss, _other_ai and site this way.
    kept_paths = {p for p in by_path if (approved_dir / p).is_dir()}
    stale_paths = set(by_path) - kept_paths

    # Never re-add a top-level dir that a kept nested entry already covers
    # (e.g. do not add 'site' when only 'site/lingva' is registered).
    covered_parents = {
        PurePosixPath(p).parts[0] for p in kept_paths if len(PurePosixPath(p).parts) > 1
    }
    discovered_paths = actual_paths - covered_parents - kept_paths

    all_paths = kept_paths | discovered_paths
    if not prune:
        all_paths |= stale_paths

    # Preserve the order of existing entries, append newly found ones sorted.
    ordered = [p for p in by_path if p in all_paths]
    ordered += sorted(all_paths - set(ordered))

    new_projects = []
    for rel_path in ordered:
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
        path = str(entry.get("path", ""))
        return {
            "slug": str(entry.get("slug", "")),
            "name": str(entry.get("name", "")),
            "path": path,
            # Strict: bool("false") is True and would silently re-enable a
            # project the user disabled, since this dict is written back.
            "enabled": parse_enabled(entry.get("enabled", True), f"Project {path!r}"),
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Also remove entries whose directory no longer exists on disk "
        "(off by default: projects.yaml is not tracked in git)",
    )
    args = parser.parse_args()

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
                    approved_dir_env = (
                        line.split("=", 1)[1].strip().strip('"').strip("'")
                    )
                    break

    if not approved_dir_env:
        print("ERROR: APPROVED_DIRECTORY not set", file=sys.stderr)
        sys.exit(1)

    approved_dir = Path(approved_dir_env)
    if not approved_dir.exists():
        print(
            f"ERROR: APPROVED_DIRECTORY does not exist: {approved_dir}", file=sys.stderr
        )
        sys.exit(1)

    print(f"Scanning: {approved_dir}")
    try:
        changed = sync(config_path, approved_dir, prune=args.prune)
    except ValueError as e:
        # Refuse to rewrite the file from a config we could not parse.
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    sys.exit(2 if changed else 0)


if __name__ == "__main__":
    main()
