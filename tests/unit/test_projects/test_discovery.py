"""Tests for project auto-discovery."""

from pathlib import Path

import yaml

from src.projects.discovery import _make_display_name, _slugify, discover_new_projects


class TestSlugify:
    def test_simple_name(self) -> None:
        assert _slugify("boss") == "boss"

    def test_underscore_prefix(self) -> None:
        assert _slugify("_boss") == "boss"

    def test_spaces_and_caps(self) -> None:
        assert _slugify("My Project") == "my-project"

    def test_special_chars(self) -> None:
        assert _slugify("proj@2.0") == "proj-2-0"


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
