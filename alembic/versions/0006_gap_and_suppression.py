"""Наблюдение на лидах и стоп-лист suppression (7.2–7.8, 7.22)

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

from models import GAP_TYPE_KEYS, SUPPRESSION_KINDS, in_list

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

NOW = sa.text("now()")

# (имя, тип): объект Column одноразовый — он привязывается к таблице на первом
# же add_column, поэтому колонки собираются заново в каждой функции
GAP_COLUMNS = [
    ("gap_type", sa.String(32)),
    ("gap_value", sa.String(160)),
    ("gap_note", sa.String(120)),
    ("gap_screenshot", sa.Text),
    ("gap_captured_at", sa.DateTime(timezone=True)),
    ("gap_seconds", sa.Integer),
    ("gap_auto_verified", sa.Boolean),
]


def upgrade():
    for name, type_ in GAP_COLUMNS:
        op.add_column("leads", sa.Column(name, type_))
    op.create_check_constraint(
        "ck_leads_gap_type", "leads", in_list("gap_type", GAP_TYPE_KEYS)
    )
    # NOT VALID: лиды, собранные до наблюдения, уже лежат в verified, и
    # проверить их нечем — сайты с тех пор могли починить. Новые и любые
    # обновляемые строки констрейнт обязан пройти.
    op.create_check_constraint(
        "ck_leads_verified_needs_gap", "leads",
        "status <> 'verified' OR gap_type IS NOT NULL",
        postgresql_not_valid=True,
    )
    op.create_table(
        "suppression",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("value_norm", sa.Text, nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("source", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.CheckConstraint(in_list("kind", SUPPRESSION_KINDS),
                           name="ck_suppression_kind"),
        sa.UniqueConstraint("kind", "value_norm", name="uq_suppression_kind_value"),
    )


def downgrade():
    op.drop_table("suppression")
    op.drop_constraint("ck_leads_verified_needs_gap", "leads", type_="check")
    op.drop_constraint("ck_leads_gap_type", "leads", type_="check")
    for name, _ in reversed(GAP_COLUMNS):
        op.drop_column("leads", name)
