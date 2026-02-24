import uuid
import enum
from datetime import datetime

from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Job(Base):
    __tablename__ = "jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    url = Column(String(2048), nullable=False)
    depth = Column(Integer, default=3)
    model = Column(String(100), default="gpt-4.1")
    browser_engine = Column(String(20), default="playwright")
    target_login = Column(String(255), nullable=True)
    target_password = Column(String(255), nullable=True)
    profile_id = Column(
        UUID(as_uuid=True),
        ForeignKey("browser_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    status = Column(
        SAEnum(JobStatus, values_callable=lambda x: [e.value for e in x]),
        default=JobStatus.PENDING,
        index=True,
    )
    celery_task_id = Column(String(255), nullable=True)
    total_screenshots = Column(Integer, default=0)
    total_themes = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    miro_board_id = Column(String(255), nullable=True)
    miro_board_url = Column(String(2048), nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="jobs")
    screenshots = relationship(
        "Screenshot", back_populates="job", cascade="all, delete-orphan"
    )
