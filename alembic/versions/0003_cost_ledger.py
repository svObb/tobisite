"""cost_ledger: журнал расходов на ИИ и платные API (раздел 20 плана)

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

from models import COST_OPS, in_list

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

NOW = sa.text("now()")


def upgrade():
    op.create_table(
        "cost_ledger",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("op", sa.Text, nullable=False),
        sa.Column("model", sa.Text),
        sa.Column("input_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("api_calls", sa.Integer, nullable=False, server_default="1"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("lead_id", sa.BigInteger, sa.ForeignKey("leads.id")),
        sa.Column("batch_id", sa.Text),
        sa.Column("note", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(in_list("op", COST_OPS), name="ck_cost_ledger_op"),
    )
    op.create_index("ix_cost_ledger_created_at", "cost_ledger", ["created_at"])
    op.create_index("ix_cost_ledger_op", "cost_ledger", ["op"])


def downgrade():
    op.drop_table("cost_ledger")
