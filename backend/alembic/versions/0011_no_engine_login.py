"""no human login for the engines OPUS runs

Every bundled engine hands its login to the proxy, and the roster lives in
Library, so the shared engine username and password guarded nothing. A bundled
qBittorrent keeps the machine login already recorded for it.

Revision ID: 0011_no_engine_login
Revises: 0010_runs_cancelled
Create Date: 2026-09-16 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0011_no_engine_login'
down_revision = '0010_runs_cancelled'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text(
        "DELETE FROM settings WHERE key IN ('access_username', 'access_password')"))


def downgrade():
    pass
