"""users: the one roster the three modules share

The install already had a credential, and whoever holds it is the owner. It is
carried across here rather than asked for again, because a migration that logs
someone out of their own server is a migration that arrives as a fault.

Revision ID: 0004_users
Revises: 0003_runs
Create Date: 2026-08-28 00:00:00.000000

"""
import hashlib
import secrets

import sqlalchemy as sa
from alembic import op

revision = '0004_users'
down_revision = '0003_runs'
branch_labels = None
depends_on = None

SCRYPT = dict(n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)


def _hash(secret: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(secret.encode(), salt=salt, dklen=32, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${salt.hex()}${dk.hex()}"


def upgrade():
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

    bind = op.get_bind()
    said = dict(bind.execute(sa.text(
        "SELECT key, value FROM settings "
        "WHERE key IN ('access_username', 'access_password')")).all())
    password = (said.get('access_password') or '').strip()
    if password:
        name = ' '.join((said.get('access_username') or 'opus').split()).lower()
        bind.execute(
            sa.text("INSERT INTO users (name, display, secret, owner) "
                    "VALUES (:name, '', :secret, true)"),
            {"name": name or 'opus', "secret": _hash(password)},
        )
    # sessions were signed with a secret derived from the password, which no
    # longer decides who is inside. One per install from here, so a person
    # changing their own password cannot sign everybody else out.
    bind.execute(sa.text(
        "INSERT INTO settings (key, value) VALUES ('access_session_secret', :v) "
        "ON CONFLICT (key) DO NOTHING"), {"v": secrets.token_hex(32)})


def downgrade():
    op.drop_table('users')
