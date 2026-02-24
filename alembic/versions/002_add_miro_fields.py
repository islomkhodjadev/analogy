"""add miro integration fields

Revision ID: 002
Revises: 001
Create Date: 2026-02-11 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("miro_access_token", sa.String(255), nullable=True))
    op.add_column("jobs", sa.Column("miro_board_id", sa.String(255), nullable=True))
    op.add_column("jobs", sa.Column("miro_board_url", sa.String(2048), nullable=True))


def downgrade():
    op.drop_column("jobs", "miro_board_url")
    op.drop_column("jobs", "miro_board_id")
    op.drop_column("users", "miro_access_token")
