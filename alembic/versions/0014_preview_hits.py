"""Открытия превью и отметка первого открытия на лиде (10.20, 10.22)

Revision ID: 0014
Revises: 0013
"""
import sqlalchemy as sa
from alembic import op

from models import PREVIEW_EVENTS, in_list

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

NOW = sa.text("now()")


def upgrade():
    op.add_column("leads",
                  sa.Column("preview_opened_at", sa.DateTime(timezone=True)))
    op.create_table(
        "preview_hits",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("lead_id", sa.BigInteger, sa.ForeignKey("leads.id"),
                  nullable=False),
        sa.Column("slug", sa.Text, nullable=False),
        sa.Column("event", sa.String(16), nullable=False),
        sa.Column("happened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("object_key", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.CheckConstraint(in_list("event", PREVIEW_EVENTS),
                           name="ck_preview_hits_event"),
    )
    # объект бакета разбирается один раз: не удалился — не задвоится
    op.create_index("uq_preview_hits_object_key", "preview_hits",
                    ["object_key"], unique=True)
    op.create_index("ix_preview_hits_lead_id", "preview_hits", ["lead_id"])


def downgrade():
    op.drop_table("preview_hits")
    op.drop_column("leads", "preview_opened_at")
