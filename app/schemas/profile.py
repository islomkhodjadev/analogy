"""Pydantic schemas for BrowserProfile CRUD."""
from pydantic import BaseModel
from datetime import datetime


class ProfileCreate(BaseModel):
    domain: str
    name: str | None = None
    login_email: str | None = None
    login_password: str | None = None


class ProfileUpdate(BaseModel):
    name: str | None = None
    login_email: str | None = None
    login_password: str | None = None
    is_active: bool | None = None


class ProfileResponse(BaseModel):
    id: str
    domain: str
    name: str | None
    login_email: str | None
    has_cookies: bool
    has_local_storage: bool
    is_active: bool
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime | None

    class Config:
        from_attributes = True


class ProfileListResponse(BaseModel):
    profiles: list[ProfileResponse]
    total: int
