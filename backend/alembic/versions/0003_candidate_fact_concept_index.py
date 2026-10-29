"""Complete the provenance concept index after interrupted SQLite upgrades."""
from alembic import op
import sqlalchemy as sa


revision = "0003_candidate_fact_concept_index"
down_revision = "0002_autonomy_memory_codex"
branch_labels = None
depends_on = None


def upgrade():
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("candidate_facts")}
    if "ix_candidate_facts_concept" not in indexes:
        op.create_index("ix_candidate_facts_concept", "candidate_facts", ["concept"])


def downgrade():
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("candidate_facts")}
    if "ix_candidate_facts_concept" in indexes:
        op.drop_index("ix_candidate_facts_concept", table_name="candidate_facts")
