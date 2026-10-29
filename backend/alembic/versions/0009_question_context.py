"""Persist application question ownership and live field semantics."""
from alembic import op
import sqlalchemy as sa

revision = '0009_question_context'
down_revision = '0008_evaluation'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('application_question_requests', sa.Column('field_context', sa.JSON(), nullable=False, server_default='{}'))


def downgrade():
    op.drop_column('application_question_requests', 'field_context')
