"""
BrowserProfile — persists browser login state per user + domain.

Inspired by Browser Use Cloud's Profile concept: a reusable set of cookies
and localStorage data that can be loaded into new browser sessions to skip
re-authentication.

Cookies are stored as JSON (list of dicts with name/value/domain/path/etc).
localStorage is stored as a JSON object { key: value, ... }.
"""
import uuid
from datetime import datetime

from sqlalchemy import Column, String, DateTime, ForeignKey, Text, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class BrowserProfile(Base):
    __tablename__ = "browser_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    # Domain this profile applies to (e.g. "example.com")
    domain = Column(String(255), nullable=False, index=True)
    # Human-friendly label
    name = Column(String(255), nullable=True)

    # Serialised browser state
    cookies_json = Column(Text, nullable=True)          # JSON list of cookie dicts
    local_storage_json = Column(Text, nullable=True)    # JSON object
    session_storage_json = Column(Text, nullable=True)  # JSON object

    # Login credentials (optional — avoids re-entering on every job)
    login_email = Column(String(255), nullable=True)
    login_password = Column(String(255), nullable=True)

    # Metadata
    is_active = Column(Boolean, default=True)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="profiles")
