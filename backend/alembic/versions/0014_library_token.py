"""the token Library calls with is named after Library

Revision ID: 0014_library_token
Revises: 0013_job_events
Create Date: 2026-09-23 00:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = "0014_library_token"
down_revision = "0013_job_events"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("UPDATE settings SET key = 'access_library_token' "
                       "WHERE key = 'access_service_token'"))


def downgrade():
    op.execute(sa.text("UPDATE settings SET key = 'access_service_token' "
                       "WHERE key = 'access_library_token'"))
