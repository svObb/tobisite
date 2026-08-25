"""Счета подписки: таблица invoices и календарь цикла на продаже (12.29)

Revision ID: 0018
Revises: 0017
"""
import sqlalchemy as sa
from alembic import op

from models import INVOICE_STATUSES, in_list

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sales", sa.Column("sub_amount", sa.Numeric(10, 2)))
    for name in ("sub_started_at", "sub_next_at", "sub_notified_at",
                 "sub_cancelled_at"):
        op.add_column("sales", sa.Column(name, sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_sales_sub_amount", "sales", "sub_amount IS NULL OR sub_amount > 0"
    )

    op.create_table(
        "invoices",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("lead_id", sa.BigInteger, sa.ForeignKey("leads.id"),
                  nullable=False),
        sa.Column("sale_id", sa.BigInteger, sa.ForeignKey("sales.id"),
                  nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.Text, nullable=False, server_default="USD"),
        sa.Column("status", sa.Text, nullable=False, server_default="issued"),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("reminded_at", sa.DateTime(timezone=True)),
        sa.Column("reminders", sa.Integer, nullable=False, server_default="0"),
        sa.Column("note", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(in_list("status", INVOICE_STATUSES),
                           name="ck_invoices_status"),
        sa.CheckConstraint("amount > 0", name="ck_invoices_amount"),
        sa.UniqueConstraint("sale_id", "period_start",
                            name="uq_invoices_sale_period"),
    )
    op.create_index("ix_invoices_lead_id", "invoices", ["lead_id"])
    op.create_index("ix_invoices_status", "invoices", ["status"])


def downgrade():
    op.drop_table("invoices")
    op.drop_constraint("ck_sales_sub_amount", "sales", type_="check")
    for name in ("sub_cancelled_at", "sub_notified_at", "sub_next_at",
                 "sub_started_at", "sub_amount"):
        op.drop_column("sales", name)
