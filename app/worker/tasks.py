import logging
from datetime import datetime

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from app.worker.celery_app import celery_app
from app.database import SessionLocal
from app.models.job import Job, JobStatus
from app.models.user import User  # noqa: F401 - needed for relationship resolution
from app.models.screenshot import Screenshot  # noqa: F401

logger = logging.getLogger("worker.tasks")


class DatabaseTask(Task):
    """Base task that provides a DB session."""
    _db = None

    @property
    def db(self):
        if self._db is None:
            self._db = SessionLocal()
        return self._db

    def after_return(self, *args, **kwargs):
        if self._db is not None:
            self._db.close()
            self._db = None


@celery_app.task(bind=True, base=DatabaseTask, name="run_screenshot_job")
def run_screenshot_job(self, job_id: str, openai_api_key: str):
    """
    Main Celery task: runs the full SiteAgent pipeline for a job.
    """
    db = self.db
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        logger.error("Job {} not found".format(job_id))
        return

    # Mark as running
    job.status = JobStatus.RUNNING
    job.started_at = datetime.utcnow()
    job.celery_task_id = self.request.id
    db.commit()

    try:
        from app.worker.engine import run_agent_for_job
        result = run_agent_for_job(
            db=db,
            job=job,
            openai_api_key=openai_api_key,
        )

        job.status = JobStatus.COMPLETED
        job.total_screenshots = result["total_screenshots"]
        job.total_themes = result["total_themes"]
        job.completed_at = datetime.utcnow()
        db.commit()

        logger.info("Job {} completed: {} screenshots".format(job_id, result["total_screenshots"]))

    except SoftTimeLimitExceeded:
        # Graceful timeout: the agent should have already finished via its
        # internal time budget, but if it didn't, we save whatever we captured.
        logger.warning("Job {} hit soft time limit - saving partial results".format(job_id))

        # Check if we got any screenshots despite the timeout
        from app.models.screenshot import Screenshot as ScreenshotModel
        existing_count = db.query(ScreenshotModel).filter(
            ScreenshotModel.job_id == job.id
        ).count()

        if existing_count > 0:
            # We have partial results - mark as completed with a note
            job.status = JobStatus.COMPLETED
            job.total_screenshots = existing_count
            job.error_message = "Partial capture: timed out after {} screenshots".format(existing_count)
        else:
            job.status = JobStatus.FAILED
            job.error_message = "Job timed out before capturing any screenshots (15 min limit)"

        job.completed_at = datetime.utcnow()
        db.commit()
        logger.warning("Job {} timed out with {} screenshots saved".format(
            job_id, existing_count))

    except Exception as e:
        logger.exception("Job {} failed".format(job_id))
        job.status = JobStatus.FAILED
        job.error_message = str(e)[:2000]
        job.completed_at = datetime.utcnow()
        db.commit()
