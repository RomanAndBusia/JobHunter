"""Add per-application CV selection and richer clarification metadata."""
from alembic import op
import sqlalchemy as sa


revision = "0006_semantic_controls"
down_revision = "0005_application_sessions"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("applications") as batch:
        batch.add_column(sa.Column("selected_cv_document_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_applications_selected_cv_document_id_candidate_documents",
            "candidate_documents", ["selected_cv_document_id"], ["id"], ondelete="SET NULL",
        )
    with op.batch_alter_table("application_question_requests") as batch:
        batch.add_column(sa.Column("question_category", sa.String(length=40), nullable=False, server_default="FACTUAL"))
        batch.add_column(sa.Column("suggested_value", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("suggestion_reason", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("application_question_requests") as batch:
        batch.drop_column("suggestion_reason")
        batch.drop_column("suggested_value")
        batch.drop_column("question_category")
    with op.batch_alter_table("applications") as batch:
        batch.drop_constraint(
            "fk_applications_selected_cv_document_id_candidate_documents", type_="foreignkey",
        )
        batch.drop_column("selected_cv_document_id")
