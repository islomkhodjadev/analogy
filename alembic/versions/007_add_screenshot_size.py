"""add screenshot size fields to jobs

Revision ID: 007
Revises: 006
Create Date: 2026-03-04
"""
from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "jobs",
        sa.Column("screenshot_mode", sa.String(20), server_default="viewport", nullable=False),
    )
    op.add_column("jobs", sa.Column("viewport_width", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("viewport_height", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("jobs", "viewport_height")
    op.drop_column("jobs", "viewport_width")
    op.drop_column("jobs", "screenshot_mode")
