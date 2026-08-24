"""Причина отклонения лида (6.17)

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

from models import LEAD_REJECT_KEYS, in_list

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("leads", sa.Column("reject_reason", sa.String(32)))
    # NULL проходит IN-констрейнт: у лидов, отклонённых до этой миграции,
    # причины нет и взяться ей неоткуда
    op.create_check_constraint(
        "ck_leads_reject_reason", "leads",
        in_list("reject_reason", LEAD_REJECT_KEYS),
    )


def downgrade():
    op.drop_constraint("ck_leads_reject_reason", "leads", type_="check")
    op.drop_column("leads", "reject_reason")
