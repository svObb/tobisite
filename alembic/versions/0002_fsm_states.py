"""fsm_states: состояние форм переезжает из памяти процесса в базу

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

NOW = sa.text("now()")


def upgrade():
    op.create_table(
        "fsm_states",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("state", sa.Text),
        sa.Column("data", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    )


def downgrade():
    op.drop_table("fsm_states")
