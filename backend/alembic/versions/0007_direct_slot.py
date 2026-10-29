"""Persist the explicit Apply-by-URL slot independently of workflow status."""
from alembic import op
import sqlalchemy as sa

revision = "0007_direct_slot"
down_revision = "0006_semantic_controls"
branch_labels = None
depends_on = None


def upgrade():
    # Native ADD COLUMN avoids rebuilding a parent table with cascading children.
    op.add_column("applications", sa.Column("direct_slot_active", sa.Boolean(), nullable=True))
    op.create_index("uq_candidate_direct_slot", "applications", ["candidate_id", "direct_slot_active"], unique=True)
    # Recover the most recent direct session once; never infer selection on reads.
    op.execute("""
        UPDATE applications SET direct_slot_active = 1 WHERE id IN (
            SELECT MAX(a.id) FROM applications a JOIN jobs j ON j.id = a.job_id
            WHERE j.source = 'direct' GROUP BY a.candidate_id
        )
    """)


def downgrade():
    op.drop_index("uq_candidate_direct_slot", table_name="applications")
    op.drop_column("applications", "direct_slot_active")
