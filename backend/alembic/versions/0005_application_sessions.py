"""Add URL-scoped application identity and semantic execution confidence."""
from alembic import op
import sqlalchemy as sa


revision = "0005_application_sessions"
down_revision = "0004_ai_form_planner"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("applications") as batch:
        batch.add_column(sa.Column("url_fingerprint", sa.String(length=64), nullable=True))
        batch.create_index("ix_applications_url_fingerprint", ["url_fingerprint"], unique=False)
    op.execute(
        "UPDATE applications SET url_fingerprint = "
        "(SELECT jobs.content_hash FROM jobs WHERE jobs.id = applications.job_id) "
        "WHERE url_fingerprint IS NULL"
    )
    with op.batch_alter_table("application_answers") as batch:
        batch.add_column(sa.Column("execution_confidence", sa.String(length=32), nullable=False, server_default="UNKNOWN"))
    naming = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
    for table in ("application_steps", "application_answers", "form_snapshots", "application_question_requests"):
        constraint = "fk_%s_application_id_applications" % table
        with op.batch_alter_table(table, naming_convention=naming) as batch:
            batch.drop_constraint(constraint, type_="foreignkey")
            batch.create_foreign_key(
                constraint, "applications", ["application_id"], ["id"], ondelete="CASCADE",
            )


def downgrade():
    naming = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
    for table in ("application_steps", "application_answers", "form_snapshots", "application_question_requests"):
        constraint = "fk_%s_application_id_applications" % table
        with op.batch_alter_table(table, naming_convention=naming) as batch:
            batch.drop_constraint(constraint, type_="foreignkey")
            batch.create_foreign_key(constraint, "applications", ["application_id"], ["id"])
    with op.batch_alter_table("application_answers") as batch:
        batch.drop_column("execution_confidence")
    with op.batch_alter_table("applications") as batch:
        batch.drop_index("ix_applications_url_fingerprint")
        batch.drop_column("url_fingerprint")
