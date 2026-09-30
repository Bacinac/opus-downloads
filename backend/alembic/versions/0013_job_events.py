"""keep a safe timeline for each acquisition job

Revision ID: 0013_job_events
Revises: 0012_job_idempotency
Create Date: 2026-09-19 00:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = "0013_job_events"
down_revision = "0012_job_idempotency"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "job_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_events_job_id", "job_events", ["job_id"])


def downgrade():
    op.drop_index("ix_job_events_job_id", table_name="job_events")
    op.drop_table("job_events")
