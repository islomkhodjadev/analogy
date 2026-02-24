"""add browser_engine column to jobs

Revision ID: 005
Revises: 004
Create Date: 2025-01-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "jobs",
        sa.Column(
            "browser_engine", sa.String(20), server_default="playwright", nullable=False
        ),
    )


def downgrade():
    op.drop_column("jobs", "browser_engine")
