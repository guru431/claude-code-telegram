"""Project registry and Telegram thread management."""

from .discovery import discover_new_projects
from .registry import ProjectDefinition, ProjectRegistry, load_project_registry
from .thread_manager import (
    PrivateTopicsUnavailableError,
    ProjectThreadManager,
)

__all__ = [
    "ProjectDefinition",
    "ProjectRegistry",
    "discover_new_projects",
    "load_project_registry",
    "ProjectThreadManager",
    "PrivateTopicsUnavailableError",
]
