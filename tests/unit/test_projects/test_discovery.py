"""Tests for project auto-discovery."""

from pathlib import Path

import yaml

from src.projects.discovery import (
    _make_display_name,
    discover_new_projects,
    iter_project_dirs,
    path_dedupe_key,
    slugify,
)
from src.projects.registry import load_project_registry


class TestSlugify:
    def test_simple_name(self) -> None:
        assert slugify("boss") == "boss"

    def test_underscore_prefix(self) -> None:
        assert slugify("_boss") == "boss"

    def test_spaces_and_caps(self) -> None:
        assert slugify("My Project") == "my-project"

    def test_special_chars(self) -> None:
        assert slugify("proj@2.0") == "proj-2-0"

    def test_path_separator(self) -> None:
        assert slugify("site/univer") == "site-univer"

    def test_shared_with_sync_script(self) -> None:
        """scripts/sync_projects_yaml.py must use the same slug generator."""
        from scripts.sync_projects_yaml import slug_from_path

        assert slug_from_path is slugify


class TestPathDedupeKey:
    def test_spelling_variants_collapse(self, tmp_path: Path) -> None:
        root = tmp_path.resolve()
        (root / "alpha").mkdir()
        base = path_dedupe_key(root, "alpha")

        assert base is not None
        for variant in ["./alpha", "ALPHA", "alpha/", "beta/../alpha"]:
            assert path_dedupe_key(root, variant) == base

    def test_nested_separator_variants(self, tmp_path: Path) -> None:
        root = tmp_path.resolve()
        (root / "site" / "lingva").mkdir(parents=True)

        assert path_dedupe_key(root, "site/lingva") == path_dedupe_key(
            root, "site\\lingva"
        )

    def test_distinct_dirs_differ(self, tmp_path: Path) -> None:
        root = tmp_path.resolve()
        assert path_dedupe_key(root, "alpha") != path_dedupe_key(root, "beta")

    def test_rejects_absolute_root_and_outside(self, tmp_path: Path) -> None:
        root = (tmp_path / "projects").resolve()
        root.mkdir()

        assert path_dedupe_key(root, str(root / "alpha")) is None
        assert path_dedupe_key(root, "") is None
        assert path_dedupe_key(root, ".") is None
        assert path_dedupe_key(root, "..") is None
        assert path_dedupe_key(root, "../outside") is None


class TestIterProjectDirs:
    def test_filters_and_sorts(self, tmp_path: Path) -> None:
        root = tmp_path / "projects"
        root.mkdir()
        for name in ["beta", "alpha", "_boss", "__pycache__", ".git", "node_modules"]:
            (root / name).mkdir()
        (root / "readme.txt").write_text("hi", encoding="utf-8")

        assert [d.name for d in iter_project_dirs(root)] == ["_boss", "alpha", "beta"]

    def test_missing_root_yields_nothing(self, tmp_path: Path) -> None:
        assert list(iter_project_dirs(tmp_path / "nope")) == []


class TestDiscoveryPathNormalization:
    """Regression: discovery compared raw strings, registry resolved paths."""

    def _config(self, tmp_path: Path, registered: str) -> tuple[Path, Path]:
        approved = tmp_path / "projects"
        approved.mkdir()
        (approved / "alpha").mkdir()
        config = tmp_path / "projects.yaml"
        config.write_text(
            yaml.dump(
                {
                    "projects": [
                        {
                            "slug": "a",
                            "name": "A",
                            "path": registered,
                            "enabled": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return approved, config

    def test_dot_slash_prefix_is_not_duplicated(self, tmp_path: Path) -> None:
        approved, config = self._config(tmp_path, "./alpha")

        new, total = discover_new_projects(approved, config)

        assert new == []
        assert total == 1
        # The registry must still load; a duplicate used to break startup.
        assert len(load_project_registry(config, approved).projects) == 1

    def test_case_variant_is_not_duplicated(self, tmp_path: Path) -> None:
        approved, config = self._config(tmp_path, "Alpha")

        new, _ = discover_new_projects(approved, config)

        assert new == []
        assert len(load_project_registry(config, approved).projects) == 1

    def test_genuinely_new_dir_still_added(self, tmp_path: Path) -> None:
        approved, config = self._config(tmp_path, "./alpha")
        (approved / "beta").mkdir()

        new, total = discover_new_projects(approved, config)

        assert [p["path"] for p in new] == ["beta"]
        assert total == 2
        assert len(load_project_registry(config, approved).projects) == 2


class TestDisplayName:
    def test_preserves_underscore(self) -> None:
        assert _make_display_name("_boss") == "_boss"

    def test_preserves_name(self) -> None:
        assert _make_display_name("MyProject") == "MyProject"


class TestDiscoverNewProjects:
    def test_discovers_new_dirs(self, tmp_path: Path) -> None:
        approved = tmp_path / "projects"
        approved.mkdir()
        (approved / "alpha").mkdir()
        (approved / "beta").mkdir()

        config = tmp_path / "projects.yaml"
        config.write_text(
            yaml.dump(
                {
                    "projects": [
                        {
                            "slug": "alpha",
                            "name": "Alpha",
                            "path": "alpha",
                            "enabled": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        new, total = discover_new_projects(approved, config)

        assert len(new) == 1
        assert new[0]["slug"] == "beta"
        assert new[0]["path"] == "beta"
        assert total == 2

        # Verify YAML was updated
        with open(config, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert len(data["projects"]) == 2

    def test_no_new_projects(self, tmp_path: Path) -> None:
        approved = tmp_path / "projects"
        approved.mkdir()
        (approved / "alpha").mkdir()

        config = tmp_path / "projects.yaml"
        config.write_text(
            yaml.dump(
                {
                    "projects": [
                        {
                            "slug": "alpha",
                            "name": "Alpha",
                            "path": "alpha",
                            "enabled": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        new, total = discover_new_projects(approved, config)
        assert len(new) == 0
        assert total == 1

    def test_skips_hidden_dirs(self, tmp_path: Path) -> None:
        approved = tmp_path / "projects"
        approved.mkdir()
        (approved / ".git").mkdir()
        (approved / ".vscode").mkdir()
        (approved / "real_project").mkdir()

        config = tmp_path / "projects.yaml"
        config.write_text(yaml.dump({"projects": []}), encoding="utf-8")

        new, total = discover_new_projects(approved, config)
        assert len(new) == 1
        assert new[0]["slug"] == "real-project"

    def test_skips_non_project_dirs(self, tmp_path: Path) -> None:
        approved = tmp_path / "projects"
        approved.mkdir()
        (approved / "__pycache__").mkdir()
        (approved / "node_modules").mkdir()
        (approved / "actual").mkdir()

        config = tmp_path / "projects.yaml"
        config.write_text(yaml.dump({"projects": []}), encoding="utf-8")

        new, total = discover_new_projects(approved, config)
        assert len(new) == 1
        assert new[0]["slug"] == "actual"

    def test_creates_config_if_empty(self, tmp_path: Path) -> None:
        approved = tmp_path / "projects"
        approved.mkdir()
        (approved / "foo").mkdir()

        config = tmp_path / "projects.yaml"
        # Empty YAML file
        config.write_text("", encoding="utf-8")

        new, total = discover_new_projects(approved, config)
        assert len(new) == 1
        assert total == 1

    def test_slug_uniqueness(self, tmp_path: Path) -> None:
        approved = tmp_path / "projects"
        approved.mkdir()
        (approved / "test").mkdir()

        config = tmp_path / "projects.yaml"
        config.write_text(
            yaml.dump(
                {
                    "projects": [
                        {
                            "slug": "test",
                            "name": "Test Existing",
                            "path": "other_test",
                            "enabled": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        new, total = discover_new_projects(approved, config)
        assert len(new) == 1
        assert new[0]["slug"] == "test-2"

    def test_underscore_prefix_dirs(self, tmp_path: Path) -> None:
        """Directories starting with _ should be discovered."""
        approved = tmp_path / "projects"
        approved.mkdir()
        (approved / "_boss").mkdir()
        (approved / "_admin").mkdir()

        config = tmp_path / "projects.yaml"
        config.write_text(yaml.dump({"projects": []}), encoding="utf-8")

        new, total = discover_new_projects(approved, config)
        assert len(new) == 2
        slugs = {p["slug"] for p in new}
        assert "boss" in slugs or "admin" in slugs
        names = {p["name"] for p in new}
        assert "_boss" in names
        assert "_admin" in names

    def test_skips_files(self, tmp_path: Path) -> None:
        approved = tmp_path / "projects"
        approved.mkdir()
        (approved / "readme.txt").write_text("hi")
        (approved / "real_dir").mkdir()

        config = tmp_path / "projects.yaml"
        config.write_text(yaml.dump({"projects": []}), encoding="utf-8")

        new, total = discover_new_projects(approved, config)
        assert len(new) == 1
