"""remember a caller's acquire request key

Revision ID: 0012_job_idempotency
Revises: 0011_no_engine_login
Create Date: 2026-09-18 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = "0012_job_idempotency"
down_revision = "0011_no_engine_login"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    op.add_column("jobs", sa.Column("idempotency_fingerprint", sa.String(length=64), nullable=True))
    op.create_unique_constraint("uq_jobs_app_idempotency_key", "jobs",
                                ["app", "idempotency_key"])


def downgrade():
    op.drop_constraint("uq_jobs_app_idempotency_key", "jobs", type_="unique")
    op.drop_column("jobs", "idempotency_fingerprint")
    op.drop_column("jobs", "idempotency_key")
