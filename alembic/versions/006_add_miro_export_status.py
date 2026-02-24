"""add miro export status fields

Revision ID: 006
Revises: 005
Create Date: 2026-02-24
"""
from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("miro_export_status", sa.String(20), nullable=True))
    op.add_column("jobs", sa.Column("miro_export_error", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("miro_celery_task_id", sa.String(255), nullable=True))


def downgrade():
    op.drop_column("jobs", "miro_celery_task_id")
    op.drop_column("jobs", "miro_export_error")
    op.drop_column("jobs", "miro_export_status")
