"""Tests for JobScheduler.remove_job."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.scheduler.scheduler import JobScheduler
from src.storage.database import DatabaseManager


@pytest.fixture
async def scheduler(tmp_path: Path, migrated_db):
    """A scheduler over a migrated database; never started, so nothing fires."""
    db_manager = DatabaseManager(f"sqlite:///{migrated_db(tmp_path / 'test.db')}")
    await db_manager.initialize()
    yield JobScheduler(
        event_bus=MagicMock(),
        db_manager=db_manager,
        default_working_directory=tmp_path,
    )
    await db_manager.close()


async def test_remove_job_unknown_to_the_scheduler_still_deactivates_its_row(
    scheduler: JobScheduler,
) -> None:
    """A job APScheduler no longer knows is benign: only the row is left to clean."""
    job_id = await scheduler.add_job("nightly", "0 3 * * *", "run checks")
    scheduler._scheduler.remove_job(job_id)

    assert await scheduler.remove_job(job_id) is True
    assert await scheduler.list_jobs() == []


async def test_remove_job_keeps_the_row_when_the_scheduler_fails(
    scheduler: JobScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any other failure leaves the job live in APScheduler. Deactivating the
    row anyway hid a job that kept firing from the list used to remove it."""
    job_id = await scheduler.add_job("nightly", "0 3 * * *", "run checks")
    monkeypatch.setattr(
        scheduler._scheduler,
        "remove_job",
        MagicMock(side_effect=RuntimeError("job store unavailable")),
    )

    with pytest.raises(RuntimeError):
        await scheduler.remove_job(job_id)

    assert [job["job_id"] for job in await scheduler.list_jobs()] == [job_id]
