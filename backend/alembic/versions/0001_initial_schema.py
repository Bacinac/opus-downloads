"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-14 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'settings',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )
    op.create_table(
        'jobs',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('engine', sa.String(length=32), nullable=False),
        sa.Column('app', sa.String(length=32), nullable=True),
        sa.Column('namespace', sa.String(length=128), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('grab_ref', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('job_ref', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            'state',
            sa.Enum('queued', 'downloading', 'complete', 'failed', name='jobstate'),
            nullable=False,
        ),
        sa.Column('progress', sa.Float(), nullable=False),
        sa.Column('landing_path', sa.Text(), nullable=True),
        sa.Column('delivered_path', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('jobs')
    op.execute('DROP TYPE jobstate')
    op.drop_table('settings')
