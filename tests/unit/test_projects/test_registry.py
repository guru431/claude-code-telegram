"""Tests for YAML project registry loading."""

from pathlib import Path

import pytest

from src.projects.registry import load_project_registry, parse_enabled


class TestParseEnabled:
    @pytest.mark.parametrize("value", [True, "true", "True", "yes", "on", "1", 1])
    def test_truthy(self, value: object) -> None:
        assert parse_enabled(value, "ctx") is True

    @pytest.mark.parametrize("value", [False, "false", "False", "no", "off", "0", 0])
    def test_falsy(self, value: object) -> None:
        assert parse_enabled(value, "ctx") is False

    @pytest.mark.parametrize("value", ["maybe", "", "disabled", 2, None, []])
    def test_unknown_raises(self, value: object) -> None:
        with pytest.raises(ValueError, match="invalid 'enabled' value"):
            parse_enabled(value, "ctx")


def test_quoted_false_disables_project(tmp_path: Path) -> None:
    """Regression: bool("false") is True, silently re-enabling the project."""
    approved = tmp_path / "projects"
    approved.mkdir()
    (approved / "app_one").mkdir()

    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        'projects:\n  - slug: app1\n    name: App One\n    path: app_one\n    enabled: "false"\n',
        encoding="utf-8",
    )

    registry = load_project_registry(config_file, approved)

    assert registry.projects[0].enabled is False
    assert registry.list_enabled() == []


def test_invalid_enabled_value_rejected(tmp_path: Path) -> None:
    approved = tmp_path / "projects"
    approved.mkdir()
    (approved / "app_one").mkdir()

    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n"
        "  - slug: app1\n"
        "    name: App One\n"
        "    path: app_one\n"
        "    enabled: sometimes\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid 'enabled' value"):
        load_project_registry(config_file, approved)


def test_load_project_registry_valid(tmp_path: Path) -> None:
    approved = tmp_path / "projects"
    approved.mkdir()
    (approved / "app_one").mkdir()
    (approved / "app_two").mkdir()

    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n"
        "  - slug: app1\n"
        "    name: App One\n"
        "    path: app_one\n"
        "  - slug: app2\n"
        "    name: App Two\n"
        "    path: app_two\n"
        "    enabled: false\n",
        encoding="utf-8",
    )

    registry = load_project_registry(config_file, approved)

    assert len(registry.projects) == 2
    enabled = registry.list_enabled()
    assert len(enabled) == 1
    assert enabled[0].slug == "app1"


def test_load_project_registry_rejects_duplicate_slug(tmp_path: Path) -> None:
    approved = tmp_path / "projects"
    approved.mkdir()
    (approved / "app_one").mkdir()
    (approved / "app_two").mkdir()

    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n"
        "  - slug: app\n"
        "    name: App One\n"
        "    path: app_one\n"
        "  - slug: app\n"
        "    name: App Two\n"
        "    path: app_two\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_project_registry(config_file, approved)

    assert "Duplicate project slug" in str(exc_info.value)


def test_load_project_registry_rejects_outside_approved_dir(tmp_path: Path) -> None:
    approved = tmp_path / "projects"
    approved.mkdir()

    config_file = tmp_path / "projects.yaml"
    config_file.write_text(
        "projects:\n" "  - slug: app\n" "    name: App\n" "    path: ../outside\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_project_registry(config_file, approved)

    assert "outside approved directory" in str(exc_info.value)
