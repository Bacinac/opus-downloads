"""a run carries that it was asked to stop, so a restart finishes dropping it

Revision ID: 0010_runs_cancelled
Revises: 0009_library_token_configured
Create Date: 2026-09-16 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0010_runs_cancelled'
down_revision = '0009_library_token_configured'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('runs', sa.Column('cancelled', sa.Boolean(), nullable=False,
                                    server_default=sa.false()))


def downgrade():
    op.drop_column('runs', 'cancelled')
