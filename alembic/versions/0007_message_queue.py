"""Очередь одобрения писем: message_drafts и message_versions (9.19–9.24)

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from models import DRAFT_STATUSES, VERSION_AUTHORS, in_list

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

NOW = sa.text("now()")


def upgrade():
    op.create_table(
        "message_drafts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("lead_id", sa.BigInteger, sa.ForeignKey("leads.id"),
                  nullable=False),
        sa.Column("touch_number", sa.Integer, nullable=False),
        sa.Column("channel", sa.Text, nullable=False, server_default="email"),
        sa.Column("lang", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("claimed_by", sa.BigInteger),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("available_at", sa.DateTime(timezone=True)),
        sa.Column("shown_version_id", sa.BigInteger),
        sa.Column("expired_leases", sa.Integer, nullable=False,
                  server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.CheckConstraint(in_list("status", DRAFT_STATUSES),
                           name="ck_message_drafts_status"),
        sa.UniqueConstraint("lead_id", "touch_number",
                            name="uq_message_drafts_lead_touch"),
    )
    op.create_index("ix_message_drafts_status", "message_drafts", ["status"])
    op.create_table(
        "message_versions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("draft_id", sa.BigInteger,
                  sa.ForeignKey("message_drafts.id"), nullable=False),
        sa.Column("author", sa.Text, nullable=False),
        sa.Column("subject", sa.Text),
        sa.Column("body", sa.Text),
        sa.Column("slots_json", postgresql.JSONB),
        sa.Column("edited_slots", postgresql.ARRAY(sa.Text)),
        sa.Column("diff_ratio", sa.Numeric(4, 3)),
        sa.Column("prompt_version", sa.Text),
        sa.Column("model", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.CheckConstraint(in_list("author", VERSION_AUTHORS),
                           name="ck_message_versions_author"),
    )
    op.create_index("ix_message_versions_draft_id", "message_versions",
                    ["draft_id"])


def downgrade():
    op.drop_table("message_versions")
    op.drop_table("message_drafts")
