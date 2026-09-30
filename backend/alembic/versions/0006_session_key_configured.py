"""sessions are signed by configuration alone

The key the three modules sign one session with has to be the same in all three,
so it can only come from their shared configuration. The copy this install once
generated for itself signed cookies nobody else could read.

Revision ID: 0006_session_key_configured
Revises: 0005_roster_leaves
Create Date: 2026-09-16 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0006_session_key_configured'
down_revision = '0005_roster_leaves'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("DELETE FROM settings WHERE key = 'access_session_secret'"))


def downgrade():
    pass
