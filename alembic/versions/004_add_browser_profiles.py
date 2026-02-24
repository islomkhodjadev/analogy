"""add browser_profiles table and profile_id to jobs

Revision ID: 004
Revises: 003
Create Date: 2025-01-01 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade():
    # Create browser_profiles table
    op.create_table(
        "browser_profiles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("domain", sa.String(255), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("cookies_json", sa.Text, nullable=True),
        sa.Column("local_storage_json", sa.Text, nullable=True),
        sa.Column("session_storage_json", sa.Text, nullable=True),
        sa.Column("login_email", sa.String(255), nullable=True),
        sa.Column("login_password", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("last_used_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime),
        sa.Column("updated_at", sa.DateTime),
    )

    # Add profile_id reference to jobs table
    op.add_column(
        "jobs",
        sa.Column("profile_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_jobs_profile_id",
        "jobs",
        "browser_profiles",
        ["profile_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade():
    op.drop_constraint("fk_jobs_profile_id", "jobs", type_="foreignkey")
    op.drop_column("jobs", "profile_id")
    op.drop_table("browser_profiles")
