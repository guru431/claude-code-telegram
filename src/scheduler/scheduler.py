"""Job scheduler for recurring agent tasks.

Wraps APScheduler's AsyncIOScheduler and publishes ScheduledEvents
to the event bus when jobs fire.
"""

import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from apscheduler.schedulers.asyncio import (
    AsyncIOScheduler,  # type: ignore[import-untyped]
)
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from ..events.bus import EventBus
from ..events.types import ScheduledEvent
from ..storage.database import DatabaseManager

logger = structlog.get_logger()


class JobScheduler:
    """Cron scheduler that publishes ScheduledEvents to the event bus."""

    def __init__(
        self,
        event_bus: EventBus,
        db_manager: DatabaseManager,
        default_working_directory: Path,
    ) -> None:
        self.event_bus = event_bus
        self.db_manager = db_manager
        self.default_working_directory = default_working_directory
        # misfire_grace_time=1h + coalesce: this bot runs as a Windows Scheduled
        # Task on a workstation that sleeps/resumes. Without this, triggers that
        # fire while busy or after a resume (past the 1s default grace) are
        # silently skipped; coalesce collapses a backlog into a single run.
        self._scheduler = AsyncIOScheduler(
            job_defaults={"misfire_grace_time": 3600, "coalesce": True}
        )

    async def start(self) -> None:
        """Load persisted jobs and start the scheduler."""
        await self._load_jobs_from_db()
        self._scheduler.start()
        logger.info("Job scheduler started")

    async def stop(self) -> None:
        """Shutdown the scheduler gracefully."""
        self._scheduler.shutdown(wait=False)
        logger.info("Job scheduler stopped")

    async def add_job(
        self,
        job_name: str,
        cron_expression: str,
        prompt: str,
        target_chat_ids: Optional[List[int]] = None,
        working_directory: Optional[Path] = None,
        skill_name: Optional[str] = None,
        created_by: int = 0,
    ) -> str:
        """Add a new scheduled job.

        Args:
            job_name: Human-readable job name.
            cron_expression: Cron-style schedule (e.g. "0 9 * * 1-5").
            prompt: The prompt to send to Claude when the job fires.
            target_chat_ids: Telegram chat IDs to send the response to.
            working_directory: Working directory for Claude execution.
            skill_name: Optional skill to invoke.
            created_by: Telegram user ID of the creator.

        Returns:
            The job ID.
        """
        trigger = CronTrigger.from_crontab(cron_expression)
        work_dir = working_directory or self.default_working_directory
        job_id = uuid.uuid4().hex

        # Persist to the database first so a scheduler-side failure cannot leave
        # an in-memory-only job that is lost on restart.
        await self._save_job(
            job_id=job_id,
            job_name=job_name,
            cron_expression=cron_expression,
            prompt=prompt,
            target_chat_ids=target_chat_ids or [],
            working_directory=str(work_dir),
            skill_name=skill_name,
            created_by=created_by,
        )

        try:
            self._scheduler.add_job(
                self._fire_event,
                trigger=trigger,
                kwargs={
                    "job_id": job_id,
                    "job_name": job_name,
                    "prompt": prompt,
                    "working_directory": str(work_dir),
                    "target_chat_ids": target_chat_ids or [],
                    "skill_name": skill_name,
                },
                id=job_id,
                name=job_name,
            )
        except Exception:
            # Roll back the persisted row so the DB does not keep a job that
            # the scheduler refused to register.
            await self._delete_job(job_id)
            raise

        logger.info(
            "Scheduled job added",
            job_id=job_id,
            job_name=job_name,
            cron=cron_expression,
        )
        return job_id

    async def remove_job(self, job_id: str) -> bool:
        """Remove a scheduled job.

        Returns True only if an active job row was actually deactivated, so the
        caller can tell "removed" apart from "no such job".
        """
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            logger.warning("Job not found in scheduler", job_id=job_id)

        removed = await self._delete_job(job_id)
        if removed:
            logger.info("Scheduled job removed", job_id=job_id)
        else:
            logger.warning("Scheduled job not found in database", job_id=job_id)
        return removed

    async def list_jobs(self) -> List[Dict[str, Any]]:
        """List all scheduled jobs from the database."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM scheduled_jobs WHERE is_active = 1 ORDER BY created_at"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def _fire_event(
        self,
        job_name: str,
        prompt: str,
        working_directory: str,
        target_chat_ids: List[int],
        skill_name: Optional[str],
        job_id: str = "",
    ) -> None:
        """Called by APScheduler when a job triggers. Publishes a ScheduledEvent."""
        event = ScheduledEvent(
            job_id=job_id,
            job_name=job_name,
            prompt=prompt,
            working_directory=Path(working_directory),
            target_chat_ids=target_chat_ids,
            skill_name=skill_name,
        )

        logger.info(
            "Scheduled job fired",
            job_id=job_id,
            job_name=job_name,
            event_id=event.id,
        )

        await self.event_bus.publish(event)

    async def _load_jobs_from_db(self) -> None:
        """Load persisted jobs and re-register them with APScheduler."""
        try:
            async with self.db_manager.get_connection() as conn:
                cursor = await conn.execute(
                    "SELECT * FROM scheduled_jobs WHERE is_active = 1"
                )
                rows = list(await cursor.fetchall())

            for row in rows:
                row_dict = dict(row)
                try:
                    trigger = CronTrigger.from_crontab(row_dict["cron_expression"])

                    # Parse target_chat_ids from stored string. Tolerate junk
                    # in the DB: strip each token and skip non-numeric ones
                    # (with a warning) instead of failing the whole job.
                    chat_ids_str = row_dict.get("target_chat_ids", "")
                    chat_ids: List[int] = []
                    for token in (chat_ids_str or "").split(","):
                        token = token.strip()
                        if not token:
                            continue
                        try:
                            chat_ids.append(int(token))
                        except ValueError:
                            logger.warning(
                                "Skipping non-numeric target_chat_id token",
                                job_id=row_dict.get("job_id"),
                                token=token,
                            )

                    self._scheduler.add_job(
                        self._fire_event,
                        trigger=trigger,
                        kwargs={
                            "job_id": row_dict["job_id"],
                            "job_name": row_dict["job_name"],
                            "prompt": row_dict["prompt"],
                            "working_directory": row_dict["working_directory"],
                            "target_chat_ids": chat_ids,
                            "skill_name": row_dict.get("skill_name"),
                        },
                        id=row_dict["job_id"],
                        name=row_dict["job_name"],
                        replace_existing=True,
                    )
                    logger.debug(
                        "Loaded scheduled job from DB",
                        job_id=row_dict["job_id"],
                        job_name=row_dict["job_name"],
                    )
                except Exception:
                    logger.exception(
                        "Failed to load scheduled job",
                        job_id=row_dict.get("job_id"),
                    )

            logger.info("Loaded scheduled jobs from database", count=len(rows))
        except sqlite3.OperationalError as e:
            # Only a genuinely missing table is a benign fresh-start. Anything
            # else (e.g. "database is locked") must NOT be swallowed as "no
            # table" — that would silently schedule zero jobs while active rows
            # exist.
            if "no such table" in str(e).lower():
                logger.debug("No scheduled_jobs table found, starting fresh")
            else:
                logger.error(
                    "Failed to load scheduled jobs from database", error=str(e)
                )
                raise
        except Exception:
            logger.error("Failed to load scheduled jobs from database", exc_info=True)
            raise

    async def _save_job(
        self,
        job_id: str,
        job_name: str,
        cron_expression: str,
        prompt: str,
        target_chat_ids: List[int],
        working_directory: str,
        skill_name: Optional[str],
        created_by: int,
    ) -> None:
        """Persist a job definition to the database."""
        chat_ids_str = ",".join(str(cid) for cid in target_chat_ids)
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT OR REPLACE INTO scheduled_jobs
                (job_id, job_name, cron_expression, prompt, target_chat_ids,
                 working_directory, skill_name, created_by, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    job_id,
                    job_name,
                    cron_expression,
                    prompt,
                    chat_ids_str,
                    working_directory,
                    skill_name,
                    created_by,
                ),
            )
            await conn.commit()

    async def _delete_job(self, job_id: str) -> bool:
        """Soft-delete a job from the database.

        Returns True if an active row was deactivated, False if there was
        nothing to deactivate.
        """
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "UPDATE scheduled_jobs SET is_active = 0 "
                "WHERE job_id = ? AND is_active = 1",
                (job_id,),
            )
            await conn.commit()
            return bool(cursor.rowcount)
