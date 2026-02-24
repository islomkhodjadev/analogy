from pydantic import BaseModel
from datetime import datetime


class ScreenshotResponse(BaseModel):
    id: str
    url: str
    title: str | None
    description: str | None
    theme: str | None
    file_url: str
    file_size_bytes: int | None
    order_index: int
    created_at: datetime

    class Config:
        from_attributes = True


class ScreenshotListResponse(BaseModel):
    screenshots: list[ScreenshotResponse]
    total: int
