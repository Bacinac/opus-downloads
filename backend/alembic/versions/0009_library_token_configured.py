"""the token Library is asked with comes from the environment

Revision ID: 0009_library_token_configured
Revises: 0008_no_requests
Create Date: 2026-09-16 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0009_library_token_configured'
down_revision = '0008_no_requests'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("DELETE FROM settings WHERE key = 'access_library_token'"))


def downgrade():
    pass
