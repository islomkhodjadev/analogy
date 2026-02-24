from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.dependencies import get_db, get_current_user
from app.models.user import User
from app.models.job import Job
from app.models.screenshot import Screenshot
from app.schemas.screenshot import ScreenshotResponse, ScreenshotListResponse

router = APIRouter()


def _screenshot_to_response(s: Screenshot) -> ScreenshotResponse:
    return ScreenshotResponse(
        id=str(s.id),
        url=s.url,
        title=s.title,
        description=s.description,
        theme=s.theme,
        file_url="/static/{}".format(s.file_path),
        file_size_bytes=s.file_size_bytes,
        order_index=s.order_index or 0,
        created_at=s.created_at,
    )


@router.get("/jobs/{job_id}/screenshots", response_model=ScreenshotListResponse)
def list_screenshots(
    job_id: UUID,
    theme: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Verify job belongs to user
    job = db.query(Job).filter(Job.id == job_id, Job.user_id == current_user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    query = db.query(Screenshot).filter(Screenshot.job_id == job_id)
    if theme:
        query = query.filter(Screenshot.theme == theme)

    screenshots = query.order_by(Screenshot.order_index).all()

    return ScreenshotListResponse(
        screenshots=[_screenshot_to_response(s) for s in screenshots],
        total=len(screenshots),
    )


@router.get("/screenshots/{screenshot_id}", response_model=ScreenshotResponse)
def get_screenshot(
    screenshot_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    screenshot = db.query(Screenshot).filter(Screenshot.id == screenshot_id).first()
    if not screenshot:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    # Verify ownership through job
    job = db.query(Job).filter(Job.id == screenshot.job_id, Job.user_id == current_user.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    return _screenshot_to_response(screenshot)
