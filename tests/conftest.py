"""Pytest configuration and fixtures."""

import sqlite3
from pathlib import Path
from typing import Callable

import pytest

_DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _dotenv_keys() -> list[str]:
    """Return the variable names defined in the project ``.env`` (if any)."""
    keys: list[str] = []
    if not _DOTENV_PATH.is_file():
        return keys
    for line in _DOTENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.append(line.split("=", 1)[0].strip())
    return keys


@pytest.fixture(autouse=True)
def _isolate_dotenv(monkeypatch):
    """Stop the developer's real ``.env`` from leaking into ``Settings``.

    Two leak paths are closed:

    1. ``Settings`` has ``model_config["env_file"] = ".env"``, so unset fields
       (``ENABLE_PROJECT_THREADS``, ``PROJECT_THREADS_*``, API keys, …) are read
       straight from the local ``.env``. We blank ``env_file`` for the test.
    2. ``load_config()`` calls ``load_dotenv()``, which copies ``.env`` into
       ``os.environ`` permanently. Once any test triggers that, every later
       test sees those values. We delete each ``.env`` key from the environment
       at the start of every test (monkeypatch restores them at teardown, and
       the next test's setup deletes them again — self-correcting).

    Net effect: ``Settings`` construction depends only on explicit kwargs and
    ``TestingConfig`` defaults, independent of the operator's real bot config.
    """
    from src.config.settings import Settings

    cfg = dict(Settings.model_config)
    cfg["env_file"] = None
    monkeypatch.setattr(Settings, "model_config", cfg)

    for key in _dotenv_keys():
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(scope="session")
def _migrated_db_image(tmp_path_factory) -> bytes:
    """Byte image of a fully migrated database, built once per session.

    ``DatabaseManager.initialize()`` runs nine migrations, each committing on
    its own; on Windows that costs ~0.4 s per database. Roughly ninety tests
    build one, which alone blew the 60 s budget for the fast suite. The
    migration list is replayed here once (synchronously — same SQL, same
    ``schema_version`` rows as the async path) and the resulting file is handed
    to those tests as bytes: ``initialize()`` then finds version 9 and runs no
    migration at all.

    Databases that must start empty or on an *older* schema (the migration
    tests in ``test_fk_integrity.py``) deliberately do not use this.
    """
    from src.storage.database import DatabaseManager

    db_path = tmp_path_factory.mktemp("db-template") / "template.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
        )
        for version, migration in DatabaseManager(
            "sqlite:///:memory:"
        )._get_migrations():
            conn.executescript(migration)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        conn.commit()
    finally:
        conn.close()
    return db_path.read_bytes()


@pytest.fixture
def migrated_db(_migrated_db_image) -> Callable[[Path], Path]:
    """Return a factory that seeds *path* with an already-migrated database.

    Use it right before ``DatabaseManager``/``Storage`` initialization when the
    test only needs the current schema, not the migration run itself.
    """

    def _seed(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_migrated_db_image)
        return path

    return _seed


@pytest.fixture
def sample_user_id():
    """Sample Telegram user ID for testing."""
    return 123456789


@pytest.fixture
def sample_config():
    """Sample configuration for testing."""
    return {
        "telegram_bot_token": "test_token",
        "telegram_bot_username": "test_bot",
        "approved_directory": "/tmp/test_projects",
        "allowed_users": [123456789],
    }
