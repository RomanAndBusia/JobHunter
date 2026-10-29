"""Autonomous profile memory, campaigns, salary policy, and Codex runs.

The original 0001 migration creates live metadata, so this migration is deliberately
idempotent: it works for legacy databases, fresh databases, and interrupted upgrades.
"""
from alembic import op
import sqlalchemy as sa

from app.db.session import Base
from app import models  # noqa: F401


revision = "0002_autonomy_memory_codex"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


NEW_TABLES = (
    "fact_aliases", "question_concepts", "application_question_requests", "role_families",
    "search_campaigns", "search_plans", "search_queries", "salary_policies", "codex_runs",
    "process_statuses",
)

CANDIDATE_COLUMNS = {
    "concept": sa.Column("concept", sa.String(160), nullable=True),
    "user_confirmed": sa.Column("user_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
    "valid_from": sa.Column("valid_from", sa.DateTime(), nullable=True),
    "expires_at": sa.Column("expires_at", sa.DateTime(), nullable=True),
    "last_confirmed_at": sa.Column("last_confirmed_at", sa.DateTime(), nullable=True),
}

MEMORY_COLUMNS = {
    "aliases": sa.Column("aliases", sa.JSON(), nullable=False, server_default="[]"),
    "raw_user_value": sa.Column("raw_user_value", sa.JSON(), nullable=True),
    "presentation_value": sa.Column("presentation_value", sa.JSON(), nullable=True),
    "scope": sa.Column("scope", sa.String(32), nullable=False, server_default="GLOBAL"),
    "scope_key": sa.Column("scope_key", sa.String(200), nullable=True),
    "valid_from": sa.Column("valid_from", sa.DateTime(), nullable=True),
    "expires_at": sa.Column("expires_at", sa.DateTime(), nullable=True),
    "last_confirmed_at": sa.Column("last_confirmed_at", sa.DateTime(), nullable=True),
    "history": sa.Column("history", sa.JSON(), nullable=False, server_default="[]"),
}


def _missing_columns(table_name, definitions):
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}
    return [column.copy() for name, column in definitions.items() if name not in existing]


def upgrade():
    # A prior interrupted SQLite batch operation may leave its temporary table.
    # Drop it only when the authoritative table is present; never discard the
    # only surviving copy of a user's data.
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    for temporary in ("_alembic_tmp_candidate_facts", "_alembic_tmp_question_memory"):
        if temporary in existing_tables and temporary.removeprefix("_alembic_tmp_") in existing_tables:
            op.execute(sa.text("DROP TABLE " + temporary))
    candidate_missing = _missing_columns("candidate_facts", CANDIDATE_COLUMNS)
    if candidate_missing:
        with op.batch_alter_table("candidate_facts") as batch:
            for column in candidate_missing:
                batch.add_column(column)
    memory_missing = _missing_columns("question_memory", MEMORY_COLUMNS)
    if memory_missing:
        with op.batch_alter_table("question_memory") as batch:
            for column in memory_missing:
                batch.add_column(column)
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("candidate_facts")}
    if "ix_candidate_facts_concept" not in indexes:
        op.create_index("ix_candidate_facts_concept", "candidate_facts", ["concept"])
    bind = op.get_bind()
    for table_name in NEW_TABLES:
        Base.metadata.tables[table_name].create(bind=bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    existing_tables = set(sa.inspect(bind).get_table_names())
    for table_name in reversed(NEW_TABLES):
        if table_name in existing_tables:
            op.drop_table(table_name)
    existing_memory = {column["name"] for column in sa.inspect(bind).get_columns("question_memory")}
    with op.batch_alter_table("question_memory") as batch:
        for name in reversed(tuple(MEMORY_COLUMNS)):
            if name in existing_memory:
                batch.drop_column(name)
    existing_candidate = {column["name"] for column in sa.inspect(bind).get_columns("candidate_facts")}
    with op.batch_alter_table("candidate_facts") as batch:
        for name in reversed(tuple(CANDIDATE_COLUMNS)):
            if name in existing_candidate:
                batch.drop_column(name)
