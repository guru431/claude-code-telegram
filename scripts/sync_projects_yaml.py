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

from src.projects.discovery import iter_project_dirs, path_dedupe_key  # noqa: E402
from src.projects.discovery import slugify as slug_from_path  # noqa: E402
from src.projects.registry import load_project_registry, parse_enabled  # noqa: E402


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


def _unique(candidate: str, taken: set[str], suffix: str = "-{n}") -> str:
    """Return ``candidate`` made unique against ``taken`` and record it."""
    value = candidate
    counter = 2
    while value in taken:
        value = candidate + suffix.format(n=counter)
        counter += 1
    taken.add(value)
    return value


def sync(config_path: Path, approved_dir: Path, prune: bool = False) -> bool:
    """Sync projects.yaml with filesystem. Returns True if changes were made."""
    approved_root = approved_dir.resolve()

    # Load existing config
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        existing = data.get("projects", []) or []
    else:
        existing = []

    # Index existing entries by canonical directory identity, NOT by the raw
    # path string. The same directory can be spelled 'alpha', './alpha' or
    # 'Alpha' (on Windows); keying by the string keeps both spellings and the
    # written file then fails load_project_registry() with
    # "Duplicate project path", taking the bot down on the next startup.
    by_key: dict[str, dict] = {}
    ordered_keys: list[str] = []
    for entry in existing:
        p = str(entry.get("path", "")).strip()
        if not p:
            continue
        key = path_dedupe_key(approved_root, p)
        if key is None:
            print(f"Dropping unusable project path: {p}", file=sys.stderr)
            continue
        if key in by_key:
            print(f"Dropping duplicate project path: {p}", file=sys.stderr)
            continue
        by_key[key] = entry
        ordered_keys.append(key)

    def _path_of(key: str) -> str:
        return str(by_key[key].get("path", "")).strip().replace("\\", "/")

    # Every existing entry whose directory is still on disk is preserved. The
    # scanner deliberately does not see every valid entry (nested paths, dirs
    # without .git), so dropping whatever it missed would silently delete live
    # projects -- it previously removed _boss, _other_ai and site this way.
    kept_keys = {k for k in ordered_keys if (approved_dir / _path_of(k)).is_dir()}
    stale_keys = set(ordered_keys) - kept_keys

    # Never re-add a top-level dir that a kept nested entry already covers
    # (e.g. do not add 'site' when only 'site/lingva' is registered).
    covered_parents: set[str] = set()
    for key in kept_keys:
        parts = PurePosixPath(_path_of(key)).parts
        if len(parts) > 1:
            parent_key = path_dedupe_key(approved_root, parts[0])
            if parent_key:
                covered_parents.add(parent_key)

    # Scan top-level directories with .git
    discovered: dict[str, str] = {}
    for name in scan_directories(approved_dir):
        key = path_dedupe_key(approved_root, name)
        if key is None or key in by_key or key in covered_parents:
            continue
        discovered.setdefault(key, name)

    all_keys = kept_keys | set(discovered)
    if not prune:
        all_keys |= stale_keys

    # Preserve the order of existing entries, append newly found ones sorted.
    ordered = [k for k in ordered_keys if k in all_keys]
    ordered += sorted(set(discovered) - set(ordered), key=lambda k: discovered[k])

    # Slug and name must be unique too: load_project_registry() rejects
    # duplicates of either, so a newly discovered directory that slugifies onto
    # an existing entry would break the same startup path as a duplicate path.
    taken_slugs = {
        str(by_key[k].get("slug", "")).strip()
        for k in ordered
        if k in by_key and str(by_key[k].get("slug", "")).strip()
    }
    taken_names = {
        str(by_key[k].get("name", "")).strip()
        for k in ordered
        if k in by_key and str(by_key[k].get("name", "")).strip()
    }

    new_projects = []
    for key in ordered:
        if key in by_key:
            new_projects.append(by_key[key])
        else:
            rel_path = discovered[key]
            new_projects.append(
                {
                    "slug": _unique(slug_from_path(rel_path), taken_slugs),
                    "name": _unique(
                        name_from_path(rel_path), taken_names, suffix=" ({n})"
                    ),
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

    _write_config(config_path, {"projects": new_norm}, approved_dir)

    print(f"Updated {config_path} ({len(new_projects)} projects)")
    return True


def _write_config(config_path: Path, payload: dict, approved_dir: Path) -> None:
    """Validate the candidate config, then replace the file atomically.

    The candidate is first written to a sibling temp file and loaded through the
    very loader the bot uses at startup. Writing straight over projects.yaml
    would let a config this script considers fine but the loader rejects (or a
    half-written file, if the process dies mid-dump) become the config the bot
    fails to start on.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.dump(
            payload,
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )

    if payload["projects"]:
        try:
            load_project_registry(tmp_path, approved_dir)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    os.replace(tmp_path, config_path)


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
