"""add parent_url to screenshots for tree structure

Revision ID: 003
Revises: 002
Create Date: 2026-02-13 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("screenshots", sa.Column("parent_url", sa.String(2048), nullable=True))


def downgrade():
    op.drop_column("screenshots", "parent_url")
