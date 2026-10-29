"""Add AI form-plan provenance and requested candidate fact paths."""
from alembic import op
import sqlalchemy as sa


revision = "0004_ai_form_planner"
down_revision = "0003_candidate_fact_concept_index"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("application_answers") as batch:
        batch.add_column(sa.Column("semantic_meaning", sa.Text(), nullable=True))
        batch.add_column(sa.Column("source_paths", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("confidence", sa.Float(), nullable=True))
        batch.add_column(sa.Column("reason", sa.Text(), nullable=True))
        batch.add_column(sa.Column("status", sa.String(length=32), nullable=False, server_default="PLANNED"))
    with op.batch_alter_table("application_question_requests") as batch:
        batch.add_column(sa.Column("fact_path", sa.String(length=300), nullable=True))


def downgrade():
    with op.batch_alter_table("application_question_requests") as batch:
        batch.drop_column("fact_path")
    with op.batch_alter_table("application_answers") as batch:
        batch.drop_column("status")
        batch.drop_column("reason")
        batch.drop_column("confidence")
        batch.drop_column("source_paths")
        batch.drop_column("semantic_meaning")
