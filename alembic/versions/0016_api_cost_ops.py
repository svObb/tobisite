"""Не-ИИ API отдельными операциями журнала расходов: places, twilio (20.3)

Revision ID: 0016
Revises: 0015
"""
from alembic import op

from models import COST_OPS, in_list

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    # Констрейнт собирается из текущего COST_OPS — как в 0004 у статусов лида:
    # на свежей базе 0003 уже создал его с новыми операциями, здесь это no-op.
    op.drop_constraint("ck_cost_ledger_op", "cost_ledger", type_="check")
    op.create_check_constraint(
        "ck_cost_ledger_op", "cost_ledger", in_list("op", COST_OPS)
    )


def downgrade():
    op.execute("UPDATE cost_ledger SET op = 'other' "
               "WHERE op IN ('places', 'twilio')")
    op.drop_constraint("ck_cost_ledger_op", "cost_ledger", type_="check")
    op.create_check_constraint(
        "ck_cost_ledger_op", "cost_ledger",
        in_list("op", [o for o in COST_OPS if o not in ("places", "twilio")]),
    )
