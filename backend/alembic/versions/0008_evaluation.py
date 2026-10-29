"""Persist structured job suitability without duplicating job descriptions."""
from alembic import op
import sqlalchemy as sa

revision = "0008_evaluation"
down_revision = "0007_direct_slot"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("job_matches", sa.Column("evaluation", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    op.drop_column("job_matches", "evaluation")
