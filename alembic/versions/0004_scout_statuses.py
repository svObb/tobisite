"""Статусы raw/candidate и флаг has_ads для лид-скаута (раздел 15)

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

from models import LEAD_STATUS_KEYS, in_list

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    # Констрейнт собирается из текущего config.STATUSES — как в 0001. На свежей
    # базе 0001 уже создал его с raw/candidate, и пересоздание здесь — no-op;
    # на живой базе именно этот шаг впускает новые статусы.
    op.drop_constraint("ck_leads_status", "leads", type_="check")
    op.create_check_constraint(
        "ck_leads_status", "leads", in_list("status", LEAD_STATUS_KEYS)
    )
    op.add_column("leads", sa.Column(
        "has_ads", sa.Boolean, nullable=False, server_default=sa.false()
    ))


def downgrade():
    op.drop_column("leads", "has_ads")
    op.drop_constraint("ck_leads_status", "leads", type_="check")
    op.create_check_constraint(
        "ck_leads_status", "leads",
        in_list("status", [k for k in LEAD_STATUS_KEYS
                           if k not in ("raw", "candidate")]),
    )
