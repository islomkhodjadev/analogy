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
        logger.warning("Job {} hit soft time limit - saving partial results".format(job_id))

        from app.models.screenshot import Screenshot as ScreenshotModel
        existing_count = db.query(ScreenshotModel).filter(
            ScreenshotModel.job_id == job.id
        ).count()

        if existing_count > 0:
            job.status = JobStatus.COMPLETED
            job.total_screenshots = existing_count
            job.error_message = "Partial capture: timed out after {} screenshots".format(existing_count)
        else:
            job.status = JobStatus.FAILED
            job.error_message = "Job timed out before capturing any screenshots (15 min limit)"

        job.completed_at = datetime.utcnow()
        db.commit()
        logger.warning("Job {} timed out with {} screenshots saved".format(job_id, existing_count))

    except Exception as e:
        logger.exception("Job {} failed".format(job_id))
        job.status = JobStatus.FAILED
        job.error_message = str(e)[:2000]
        job.completed_at = datetime.utcnow()
        db.commit()


@celery_app.task(bind=True, base=DatabaseTask, name="run_miro_export")
def run_miro_export(
    self,
    job_id: str,
    miro_access_token: str,
    openai_api_key: str | None,
    board_name: str,
    board_id: str | None,
    prompt: str | None,
):
    """Async Celery task: export screenshots to Miro in the background."""
    db = self.db
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        logger.error("Miro export: job {} not found".format(job_id))
        return

    job.miro_export_status = "running"
    db.commit()

    try:
        from app.models.screenshot import Screenshot as ScreenshotModel
        from app.services.miro import MiroExporter

        screenshots = (
            db.query(ScreenshotModel)
            .filter(ScreenshotModel.job_id == job.id)
            .order_by(ScreenshotModel.order_index)
            .all()
        )

        exporter = MiroExporter(access_token=miro_access_token)
        try:
            use_ai = bool(prompt and prompt.strip())
            if use_ai:
                from app.services.board_planner import BoardPlanner

                planner = BoardPlanner(openai_api_key=openai_api_key, model="gpt-4.1")
                plan = planner.generate_plan(
                    prompt=prompt.strip(),
                    screenshots=screenshots,
                    site_url=job.url,
                )
                result_board_id, result_board_url = exporter.export_from_plan(
                    plan, screenshots, board_id
                )
            else:
                result_board_id, result_board_url = exporter.export_job(
                    board_name, screenshots, board_id
                )
        finally:
            exporter.close()

        job.miro_board_id = result_board_id
        job.miro_board_url = result_board_url
        job.miro_export_status = "completed"
        db.commit()
        logger.info("Miro export for job {} completed: {}".format(job_id, result_board_url))

    except Exception as e:
        logger.exception("Miro export for job {} failed".format(job_id))
        job.miro_export_status = "failed"
        job.miro_export_error = str(e)[:2000]
        db.commit()
