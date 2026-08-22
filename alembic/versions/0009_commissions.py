"""Комиссии работников, продажи и статус replied_interested (7.9–7.17, 7.19)

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

from models import (
    DEFAULT_COMMISSION_PCT, LEAD_STATUS_KEYS, PCT_RANGE, in_list,
)

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

NOW = sa.text("now()")
NEW_STATUS = "replied_interested"


def upgrade():
    op.add_column("workers", sa.Column(
        "commission_pct", sa.SmallInteger, nullable=False,
        server_default=str(DEFAULT_COMMISSION_PCT),
    ))
    op.create_check_constraint("ck_workers_commission_pct", "workers",
                               f"commission_pct {PCT_RANGE}")
    op.create_table(
        "sales",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("lead_id", sa.BigInteger, sa.ForeignKey("leads.id"),
                  nullable=False),
        sa.Column("worker_id", sa.BigInteger, sa.ForeignKey("workers.id"),
                  nullable=False),
        sa.Column("deal_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.Text, nullable=False, server_default="USD"),
        sa.Column("rate_pct", sa.SmallInteger, nullable=False),
        sa.Column("amount_due", sa.Numeric(10, 2), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.CheckConstraint(f"rate_pct {PCT_RANGE}", name="ck_sales_rate_pct"),
        sa.CheckConstraint("deal_amount > 0", name="ck_sales_deal_amount"),
        sa.UniqueConstraint("lead_id", name="uq_sales_lead"),
    )
    op.create_index("ix_sales_worker_id", "sales", ["worker_id"])
    op.create_index("ix_sales_created_at", "sales", ["created_at"])
    op.create_table(
        "commission_changes",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("worker_id", sa.BigInteger, sa.ForeignKey("workers.id"),
                  nullable=False),
        sa.Column("old_pct", sa.SmallInteger, nullable=False),
        sa.Column("new_pct", sa.SmallInteger, nullable=False),
        sa.Column("changed_by", sa.BigInteger, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
    )
    op.create_index("ix_commission_changes_worker_id", "commission_changes",
                    ["worker_id"])
    # констрейнт собирается из текущего config.STATUSES — как в 0001 и 0004;
    # на живой базе именно этот шаг впускает replied_interested
    op.drop_constraint("ck_leads_status", "leads", type_="check")
    op.create_check_constraint(
        "ck_leads_status", "leads", in_list("status", LEAD_STATUS_KEYS)
    )


def downgrade():
    # откат сузил бы список статусов, и лиды в replied_interested перестали бы
    # проходить констрейнт. Молча переставить им статус — потерять данные,
    # поэтому откат честно отказывается, пока такие лиды есть
    left = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM leads WHERE status = :s"),
        {"s": NEW_STATUS},
    )
    if left:
        raise RuntimeError(
            f"Откат 0009 невозможен: {left} лид(ов) в статусе {NEW_STATUS}. "
            "Переведите их в другой статус и повторите."
        )
    op.drop_constraint("ck_leads_status", "leads", type_="check")
    op.create_check_constraint(
        "ck_leads_status", "leads",
        in_list("status", [k for k in LEAD_STATUS_KEYS if k != NEW_STATUS]),
    )
    op.drop_table("commission_changes")
    op.drop_table("sales")
    op.drop_constraint("ck_workers_commission_pct", "workers", type_="check")
    op.drop_column("workers", "commission_pct")
