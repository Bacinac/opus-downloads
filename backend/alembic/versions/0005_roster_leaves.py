"""the roster leaves for Library

It was here because this module happened to answer /auth/verify first — and this
is the module you can switch off without anybody noticing until something new
fails to arrive. It belongs with the thing that has to be running for anything to
work at all, and beside the faces the library already knows by name.

The rows are carried across before this runs, hashes and all, so nobody has to
choose a new password. What is dropped here is the copy.

Revision ID: 0005_roster_leaves
Revises: 0004_users
Create Date: 2026-08-28 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '0005_roster_leaves'
down_revision = '0004_users'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table('users')


def downgrade():
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('display', sa.String(length=120), nullable=False,
                  server_default=''),
        sa.Column('secret', sa.Text(), nullable=False),
        sa.Column('owner', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('disabled', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
