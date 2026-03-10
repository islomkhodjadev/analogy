"""add capture_mode field to jobs

Revision ID: 008
Revises: 007
Create Date: 2026-03-10
"""
from alembic import op
import sqlalchemy as sa

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "jobs",
        sa.Column("capture_mode", sa.String(20), server_default="smart", nullable=False),
    )


def downgrade():
    op.drop_column("jobs", "capture_mode")
