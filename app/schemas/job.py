from pydantic import BaseModel, HttpUrl
from datetime import datetime


class JobCreate(BaseModel):
    url: HttpUrl
    depth: int = 3
    model: str = "gpt-4.1"
    browser_engine: str = "playwright"  # "playwright" or "selenium"
    screenshot_mode: str = "viewport"  # "viewport" or "full_page"
    capture_mode: str = "smart"  # "smart" (diverse) or "exhaustive" (capture everything)
    viewport_width: int | None = None  # custom viewport width (e.g. 375 for mobile)
    viewport_height: int | None = None  # custom viewport height (e.g. 812 for mobile)
    target_login: str | None = None
    target_password: str | None = None
    profile_id: str | None = None  # UUID of BrowserProfile to restore login state


class JobResponse(BaseModel):
    id: str
    url: str
    depth: int
    model: str
    browser_engine: str = "playwright"
    screenshot_mode: str = "viewport"
    capture_mode: str = "smart"
    viewport_width: int | None = None
    viewport_height: int | None = None
    status: str
    total_screenshots: int
    total_themes: int
    error_message: str | None
    miro_board_url: str | None
    miro_export_status: str | None = None
    miro_export_error: str | None = None
    profile_id: str | None = None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total: int
    page: int
    per_page: int
