"""no requests, no deliver

Consumers search, grab and track, and move what landed themselves. The request
loop and the move into a library never had a caller.

Revision ID: 0008_no_requests
Revises: 0007_jobs_seen_at
Create Date: 2026-09-16 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0008_no_requests'
down_revision = '0007_jobs_seen_at'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index('ix_requests_state', table_name='requests')
    op.drop_table('requests')
    op.execute('DROP TYPE requeststate')
    op.drop_column('jobs', 'delivered_path')


def downgrade():
    op.add_column('jobs', sa.Column('delivered_path', sa.Text(), nullable=True))
    op.create_table(
        'requests',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('app', sa.String(length=32), nullable=True),
        sa.Column('namespace', sa.String(length=128), nullable=False),
        sa.Column('query', sa.Text(), nullable=False),
        sa.Column('type', sa.String(length=16), nullable=False),
        sa.Column('criteria', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            'state',
            sa.Enum('searching', 'grabbing', 'ready', 'delivered', 'failed',
                    name='requeststate'),
            nullable=False,
        ),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('shortlist', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('attempts', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('job_id', sa.String(length=32), nullable=True),
        sa.Column('landing_path', sa.Text(), nullable=True),
        sa.Column('delivered_path', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_requests_state', 'requests', ['state'])
