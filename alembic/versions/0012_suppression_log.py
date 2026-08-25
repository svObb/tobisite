"""Журнал отписок и жалоб (9.34)

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op

from models import SUPPRESSION_EVENTS, SUPPRESSION_KINDS, in_list

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

NOW = sa.text("now()")


def upgrade():
    op.create_table(
        "suppression_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("event", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("value_norm", sa.Text, nullable=False),
        sa.Column("lead_id", sa.BigInteger, sa.ForeignKey("leads.id")),
        sa.Column("source", sa.Text),
        sa.Column("note", sa.Text),
        sa.Column("actor_tg_id", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.CheckConstraint(in_list("event", SUPPRESSION_EVENTS),
                           name="ck_suppression_events_event"),
        sa.CheckConstraint(in_list("kind", SUPPRESSION_KINDS),
                           name="ck_suppression_events_kind"),
    )
    # уникальности нет намеренно: повторная жалоба — отдельное событие
    op.create_index("ix_suppression_events_created_at", "suppression_events",
                    ["created_at"])
    op.create_index("ix_suppression_events_lead_id", "suppression_events",
                    ["lead_id"])


def downgrade():
    op.drop_table("suppression_events")
