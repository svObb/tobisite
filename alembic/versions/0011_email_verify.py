"""Проверка адреса получателя на контактах (9.29)

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op

from models import VERIFY_STATUSES, in_list

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

COLUMNS = [
    ("verify_status", sa.String(16)),
    ("verify_note", sa.Text),
    ("verified_at", sa.DateTime(timezone=True)),
]


def upgrade():
    for name, type_ in COLUMNS:
        op.add_column("contacts", sa.Column(name, type_))
    # NULL проходит IN-констрейнт: контакты, заведённые до проверки, не
    # проверялись, и выдумывать им статус нечем
    op.create_check_constraint(
        "ck_contacts_verify_status", "contacts",
        in_list("verify_status", VERIFY_STATUSES),
    )


def downgrade():
    op.drop_constraint("ck_contacts_verify_status", "contacts", type_="check")
    for name, _ in COLUMNS:
        op.drop_column("contacts", name)
