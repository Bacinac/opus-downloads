from alembic import op
import sqlalchemy as sa

revision = "0015_landing_ownership"
down_revision = "0014_library_token"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("landing_claim", sa.Text(), nullable=True))
    op.create_unique_constraint("uq_jobs_landing_claim", "jobs", ["landing_claim"])


def downgrade():
    op.drop_constraint("uq_jobs_landing_claim", "jobs", type_="unique")
    op.drop_column("jobs", "landing_claim")
