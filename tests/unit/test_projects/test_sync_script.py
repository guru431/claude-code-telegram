"""Tests for scripts/sync_projects_yaml.py.

The real config/projects.yaml is gitignored and cannot be restored from git, so
these tests only ever operate on tmp_path copies.
"""

from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from scripts.sync_projects_yaml import scan_directories, sync


def _write(config: Path, projects: List[Dict[str, Any]]) -> None:
    config.write_text(yaml.dump({"projects": projects}), encoding="utf-8")


def _paths(config: Path) -> List[str]:
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    return [e["path"] for e in data["projects"]]


@pytest.fixture
def approved(tmp_path: Path) -> Path:
    """Layout mirroring the real registry's awkward cases."""
    root = tmp_path / "projects"
    root.mkdir()
    for name in ["_boss", "_other_ai", "alpha", "beta"]:
        (root / name).mkdir()
        (root / name / ".git").mkdir()
    (root / "site" / "lingva" / ".git").mkdir(parents=True)
    (root / "nogit").mkdir()
    return root


class TestScanDirectories:
    def test_requires_git_and_shares_skip_rules(self, approved: Path) -> None:
        (approved / "__pycache__").mkdir()

        found = scan_directories(approved)

        assert "nogit" not in found
        assert "__pycache__" not in found
        # Shared scanner no longer hides underscore dirs from the sync script.
        assert "_boss" in found
        assert "alpha" in found


class TestSyncIsAdditive:
    def test_keeps_entries_the_scanner_cannot_see(
        self, approved: Path, tmp_path: Path
    ) -> None:
        """Regression: sync deleted _boss, _other_ai, site and non-git dirs."""
        config = tmp_path / "projects.yaml"
        _write(
            config,
            [
                {"slug": "boss", "name": "_boss", "path": "_boss", "enabled": True},
                {
                    "slug": "other-ai",
                    "name": "_other_ai",
                    "path": "_other_ai",
                    "enabled": True,
                },
                {"slug": "site", "name": "site", "path": "site", "enabled": True},
                {
                    "slug": "lingva",
                    "name": "Lingva",
                    "path": "site/lingva",
                    "enabled": True,
                },
                {"slug": "nogit", "name": "Nogit", "path": "nogit", "enabled": True},
            ],
        )

        sync(config, approved)

        for path in ["_boss", "_other_ai", "site", "site/lingva", "nogit"]:
            assert path in _paths(config)

    def test_adds_new_dirs(self, approved: Path, tmp_path: Path) -> None:
        config = tmp_path / "projects.yaml"
        _write(
            config,
            [{"slug": "alpha", "name": "Alpha", "path": "alpha", "enabled": True}],
        )

        assert sync(config, approved) is True
        assert "beta" in _paths(config)

    def test_keeps_missing_dirs_without_prune(
        self, approved: Path, tmp_path: Path
    ) -> None:
        config = tmp_path / "projects.yaml"
        _write(
            config,
            [{"slug": "gone", "name": "Gone", "path": "gone", "enabled": True}],
        )

        sync(config, approved)

        assert "gone" in _paths(config)

    def test_prune_removes_only_missing_dirs(
        self, approved: Path, tmp_path: Path
    ) -> None:
        config = tmp_path / "projects.yaml"
        _write(
            config,
            [
                {"slug": "gone", "name": "Gone", "path": "gone", "enabled": True},
                {"slug": "boss", "name": "_boss", "path": "_boss", "enabled": True},
                {"slug": "nogit", "name": "Nogit", "path": "nogit", "enabled": True},
            ],
        )

        sync(config, approved, prune=True)

        paths = _paths(config)
        assert "gone" not in paths
        assert "_boss" in paths
        assert "nogit" in paths

    def test_does_not_add_parent_of_registered_nested_path(
        self, approved: Path, tmp_path: Path
    ) -> None:
        config = tmp_path / "projects.yaml"
        _write(
            config,
            [
                {
                    "slug": "lingva",
                    "name": "Lingva",
                    "path": "site/lingva",
                    "enabled": True,
                }
            ],
        )

        sync(config, approved)

        assert "site" not in _paths(config)

    def test_preserves_existing_order_and_fields(
        self, approved: Path, tmp_path: Path
    ) -> None:
        config = tmp_path / "projects.yaml"
        _write(
            config,
            [
                {"slug": "zeta", "name": "Zeta", "path": "beta", "enabled": False},
                {"slug": "alpha", "name": "Alpha", "path": "alpha", "enabled": True},
            ],
        )

        sync(config, approved)

        data = yaml.safe_load(config.read_text(encoding="utf-8"))["projects"]
        assert [e["path"] for e in data[:2]] == ["beta", "alpha"]
        # Manual slug/name/enabled survive the rewrite.
        assert data[0]["slug"] == "zeta"
        assert data[0]["enabled"] is False

    def test_quoted_false_is_not_flipped_to_true(
        self, approved: Path, tmp_path: Path
    ) -> None:
        """Regression: bool("false") rewrote a disabled project as enabled."""
        config = tmp_path / "projects.yaml"
        config.write_text(
            'projects:\n  - slug: alpha\n    name: Alpha\n    path: alpha\n    enabled: "false"\n',
            encoding="utf-8",
        )

        sync(config, approved)

        data = yaml.safe_load(config.read_text(encoding="utf-8"))["projects"]
        entry = next(e for e in data if e["path"] == "alpha")
        assert entry["enabled"] is False

    def test_invalid_enabled_aborts_without_writing(
        self, approved: Path, tmp_path: Path
    ) -> None:
        config = tmp_path / "projects.yaml"
        original = (
            "projects:\n"
            "  - slug: alpha\n"
            "    name: Alpha\n"
            "    path: alpha\n"
            "    enabled: sometimes\n"
        )
        config.write_text(original, encoding="utf-8")

        with pytest.raises(ValueError, match="invalid 'enabled' value"):
            sync(config, approved)

        assert config.read_text(encoding="utf-8") == original

    def test_no_changes_reports_false(self, approved: Path, tmp_path: Path) -> None:
        config = tmp_path / "projects.yaml"
        _write(config, [])
        sync(config, approved)

        assert sync(config, approved) is False
