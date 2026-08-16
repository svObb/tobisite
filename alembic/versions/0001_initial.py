"""initial schema

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

from models import CONTACT_TYPE_KEYS, LEAD_STATUS_KEYS, in_list

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

NOW = sa.text("now()")


def _times():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
    )


def upgrade():
    op.create_table(
        "workers",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("tg_id", sa.BigInteger, nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("daily_limit", sa.Integer),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        *_times(),
    )

    op.create_table(
        "leads",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("worker_id", sa.BigInteger, sa.ForeignKey("workers.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("website_url", sa.Text),
        sa.Column("domain_norm", sa.Text),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column("country", sa.Text, nullable=False),
        sa.Column("city", sa.Text, nullable=False),
        sa.Column("language", sa.Text, nullable=False),
        sa.Column("niche", sa.Text, nullable=False),
        sa.Column("google_rating", sa.Text),
        sa.Column("note", sa.Text),
        sa.Column("screenshot_file_id", sa.Text),
        sa.Column("found_via", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="new"),
        sa.Column("possible_duplicate", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("admin_note", sa.Text),
        sa.Column("draft_url", sa.Text),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_by", sa.BigInteger),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        *_times(),
        sa.CheckConstraint(in_list("status", LEAD_STATUS_KEYS), name="ck_leads_status"),
    )
    op.create_index(
        "uq_leads_domain_norm_active", "leads", ["domain_norm"], unique=True,
        postgresql_where=sa.text(
            "domain_norm IS NOT NULL AND cancelled_at IS NULL AND deleted_at IS NULL"
        ),
    )
    op.execute(
        "CREATE INDEX ix_leads_name_city_lower ON leads "
        "(lower(btrim(name)), lower(btrim(city)))"
    )
    for col in ("worker_id", "country", "niche", "status", "created_at"):
        op.create_index(f"ix_leads_{col}", "leads", [col])

    op.create_table(
        "contacts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("lead_id", sa.BigInteger, sa.ForeignKey("leads.id"), nullable=False),
        sa.Column("ctype", sa.Text, nullable=False),
        sa.Column("ctype_other", sa.Text),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("value_norm", sa.Text),
        sa.Column("lead_cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        *_times(),
        sa.CheckConstraint(in_list("ctype", CONTACT_TYPE_KEYS), name="ck_contacts_ctype"),
    )
    op.create_index(
        "uq_contacts_phone_norm_active", "contacts", ["value_norm"], unique=True,
        postgresql_where=sa.text(
            "ctype = 'phone' AND value_norm IS NOT NULL "
            "AND deleted_at IS NULL AND lead_cancelled_at IS NULL"
        ),
    )
    op.create_index("ix_contacts_lead_id", "contacts", ["lead_id"])

    op.create_table(
        "lead_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("lead_id", sa.BigInteger, sa.ForeignKey("leads.id"), nullable=False),
        sa.Column("event", sa.Text, nullable=False),
        sa.Column("field", sa.Text),
        sa.Column("old_value", sa.Text),
        sa.Column("new_value", sa.Text),
        sa.Column("actor_tg_id", sa.BigInteger, nullable=False),
        *_times(),
    )
    op.create_index("ix_lead_events_lead_id", "lead_events", ["lead_id"])


def downgrade():
    op.drop_table("lead_events")
    op.drop_table("contacts")
    op.drop_table("leads")
    op.drop_table("workers")
