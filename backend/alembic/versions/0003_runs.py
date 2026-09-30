"""runs: the downloads OPUS performs in its own process

Revision ID: 0003_runs
Revises: 0002_requests
Create Date: 2026-08-14 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0003_runs'
down_revision = '0002_requests'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'runs',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('engine', sa.String(length=32), nullable=False),
        sa.Column('workdir', sa.Text(), nullable=False),
        # the same vocabulary a sidecar's job reports in, so a caller cannot
        # tell from the state which family fetched its file
        sa.Column(
            'state',
            postgresql.ENUM('queued', 'downloading', 'complete', 'failed',
                            name='jobstate', create_type=False),
            nullable=False,
        ),
        sa.Column('progress', sa.Float(), nullable=False),
        sa.Column('speed_bps', sa.BigInteger(), nullable=True),
        sa.Column('eta_seconds', sa.Integer(), nullable=True),
        sa.Column('detail', sa.Text(), nullable=False),
        sa.Column('landing_path', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    # startup sweeps by state to fail whatever a restart interrupted
    op.create_index('ix_runs_state', 'runs', ['state'])

    # Tidal is linked now, not typed: the placeholder credential it carried
    # before the client existed has no reader left
    op.execute("DELETE FROM settings WHERE key = 'tidal_session_token'")


def downgrade():
    op.drop_index('ix_runs_state', table_name='runs')
    op.drop_table('runs')
