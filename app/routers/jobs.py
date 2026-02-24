import shutil
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.dependencies import get_db, get_current_user
from app.config import settings
from app.models.user import User
from app.models.job import Job, JobStatus
from app.models.screenshot import Screenshot
from app.schemas.job import JobCreate, JobResponse, JobListResponse
from app.schemas.miro import MiroExportRequest, MiroExportResponse
from app.worker.tasks import run_screenshot_job

router = APIRouter()


def _job_to_response(job: Job) -> JobResponse:
    return JobResponse(
        id=str(job.id),
        url=job.url,
        depth=job.depth,
        model=job.model or "gpt-4.1",
        browser_engine=job.browser_engine or "playwright",
        status=job.status.value if job.status else "pending",
        total_screenshots=job.total_screenshots or 0,
        total_themes=job.total_themes or 0,
        error_message=job.error_message,
        miro_board_url=job.miro_board_url,
        profile_id=str(job.profile_id) if job.profile_id else None,
        started_at=job.started_at,
        completed_at=job.completed_at,
        created_at=job.created_at,
    )


@router.post("/", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
def create_job(
    body: JobCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Resolve OpenAI key
    openai_key = current_user.openai_api_key or settings.default_openai_api_key
    if not openai_key:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="No OpenAI API key configured. Set one via PATCH /auth/me",
        )

    if body.depth < 1 or body.depth > 10:
        raise HTTPException(status_code=400, detail="Depth must be between 1 and 10")

    # Validate profile_id if provided
    profile_id = None
    if body.profile_id:
        from app.models.profile import BrowserProfile

        profile = (
            db.query(BrowserProfile)
            .filter(
                BrowserProfile.id == body.profile_id,
                BrowserProfile.user_id == current_user.id,
                BrowserProfile.is_active == True,
            )
            .first()
        )
        if not profile:
            raise HTTPException(status_code=404, detail="Browser profile not found")
        profile_id = profile.id

    job = Job(
        user_id=current_user.id,
        url=str(body.url),
        depth=body.depth,
        model=body.model,
        browser_engine=body.browser_engine,
        target_login=body.target_login,
        target_password=body.target_password,
        profile_id=profile_id,
        status=JobStatus.PENDING,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Dispatch Celery task
    task = run_screenshot_job.delay(str(job.id), openai_key)
    job.celery_task_id = task.id
    db.commit()

    return _job_to_response(job)


@router.get("/", response_model=JobListResponse)
def list_jobs(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Job).filter(Job.user_id == current_user.id)

    if status_filter:
        try:
            js = JobStatus(status_filter)
            query = query.filter(Job.status == js)
        except ValueError:
            pass

    total = query.count()
    jobs = (
        query.order_by(Job.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return JobListResponse(
        jobs=[_job_to_response(j) for j in jobs],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == current_user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_to_response(job)


@router.delete("/{job_id}")
def delete_job(
    job_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == current_user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Revoke Celery task if running
    if job.celery_task_id and job.status in (JobStatus.PENDING, JobStatus.RUNNING):
        from app.worker.celery_app import celery_app

        celery_app.control.revoke(job.celery_task_id, terminate=True)

    # Delete screenshot files
    import os

    job_dir = os.path.join(settings.screenshots_root, str(current_user.id), str(job.id))
    if os.path.isdir(job_dir):
        shutil.rmtree(job_dir, ignore_errors=True)

    # Delete DB records (screenshots cascade)
    db.delete(job)
    db.commit()

    return {"detail": "Job deleted"}


@router.post("/{job_id}/export/miro", response_model=MiroExportResponse)
def export_to_miro(
    job_id: UUID,
    body: MiroExportRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == current_user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != JobStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Job must be completed before exporting to Miro",
        )

    if not current_user.miro_access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No Miro access token configured. Set one via PATCH /auth/me",
        )

    screenshots = (
        db.query(Screenshot)
        .filter(Screenshot.job_id == job_id)
        .order_by(Screenshot.order_index)
        .all()
    )
    if not screenshots:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Job has no screenshots to export",
        )

    prompt = body.prompt if body else None
    use_ai = bool(prompt and prompt.strip())

    if use_ai:
        openai_key = current_user.openai_api_key or settings.default_openai_api_key
        if not openai_key:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="No OpenAI API key configured. Set one via PATCH /auth/me",
            )

    board_name = (
        body.board_name
        if body and body.board_name
        else urlparse(job.url).netloc or "Auto Screen Export"
    )

    from app.services.miro import MiroExporter, MiroExportError
    from app.services.board_planner import BoardPlanner, BoardPlannerError

    exporter = MiroExporter(access_token=current_user.miro_access_token)
    try:
        existing_board_id = body.board_id if body else None

        if use_ai:
            planner = BoardPlanner(openai_api_key=openai_key, model="gpt-4.1")
            plan = planner.generate_plan(
                prompt=prompt.strip(),
                screenshots=screenshots,
                site_url=job.url,
            )
            if not (body and body.board_name):
                board_name = plan.board_title
            board_id, board_url = exporter.export_from_plan(
                plan,
                screenshots,
                existing_board_id,
            )
        else:
            board_id, board_url = exporter.export_job(
                board_name,
                screenshots,
                existing_board_id,
            )
    except MiroExportError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except BoardPlannerError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail="Export failed: {}".format(str(e)[:500]),
        )
    finally:
        exporter.close()

    job.miro_board_id = board_id
    job.miro_board_url = board_url
    db.commit()

    return MiroExportResponse(
        board_id=board_id,
        board_url=board_url,
        message="Successfully exported {} screenshots to Miro board".format(
            len(screenshots)
        ),
    )
