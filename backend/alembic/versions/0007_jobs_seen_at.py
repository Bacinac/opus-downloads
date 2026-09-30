"""jobs remember when their engine last saw them

A job an engine has lost used to read as queued for ever. The time the engine
last had any record of it is what lets a lost one fail instead.

Revision ID: 0007_jobs_seen_at
Revises: 0006_session_key_configured
Create Date: 2026-09-16 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0007_jobs_seen_at'
down_revision = '0006_session_key_configured'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('jobs', sa.Column('seen_at', sa.DateTime(timezone=True),
                                    server_default=sa.text('now()'), nullable=False))


def downgrade():
    op.drop_column('jobs', 'seen_at')
