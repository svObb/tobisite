"""Обогащение с сайта в журнале расходов: scrape и enrich (дорожка III)

Revision ID: 0020
Revises: 0019
"""
from alembic import op

from models import COST_OPS, in_list

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

NEW = ("scrape", "enrich")


def upgrade():
    # Констрейнт собирается из текущего COST_OPS — как в 0016: на свежей базе
    # 0003 уже создал его с новыми операциями, здесь это no-op.
    op.drop_constraint("ck_cost_ledger_op", "cost_ledger", type_="check")
    op.create_check_constraint(
        "ck_cost_ledger_op", "cost_ledger", in_list("op", COST_OPS)
    )


def downgrade():
    op.execute("UPDATE cost_ledger SET op = 'other' "
               "WHERE op IN ('scrape', 'enrich')")
    op.drop_constraint("ck_cost_ledger_op", "cost_ledger", type_="check")
    op.create_check_constraint(
        "ck_cost_ledger_op", "cost_ledger",
        in_list("op", [o for o in COST_OPS if o not in NEW]),
    )
