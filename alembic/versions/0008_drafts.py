"""Черновики сайтов и обогащение карточки лида (8.27–8.34, Д13 §5)

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from models import BUILD_STATUSES, in_list

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

NOW = sa.text("now()")
EMPTY_JSON = sa.text("'{}'::jsonb")

# Объект Column одноразовый — он привязывается к таблице на первом же
# add_column, поэтому колонки собираются заново в каждой функции.
LEAD_COLUMNS = [
    ("enrichment", postgresql.JSONB, dict(nullable=False,
                                          server_default=EMPTY_JSON)),
    ("needs_enrichment", sa.Boolean, dict(nullable=False,
                                          server_default="false")),
    ("enrichment_request", sa.Text, {}),
]


def upgrade():
    for name, type_, kw in LEAD_COLUMNS:
        op.add_column("leads", sa.Column(name, type_, **kw))
    op.create_table(
        "drafts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("lead_id", sa.BigInteger, sa.ForeignKey("leads.id"),
                  nullable=False),
        sa.Column("library_version", sa.Text),
        sa.Column("seed", sa.BigInteger),
        sa.Column("recipe_id", sa.Text),
        sa.Column("token_preset", sa.Text),
        sa.Column("section_variants", postgresql.ARRAY(sa.Text)),
        sa.Column("image_ids", postgresql.ARRAY(sa.Text)),
        sa.Column("recipe_json", postgresql.JSONB),
        sa.Column("r2_prefix", sa.Text),
        sa.Column("preview_host", sa.Text),
        sa.Column("checks_json", postgresql.JSONB),
        sa.Column("status", sa.Text, nullable=False,
                  server_default="generated"),
        sa.Column("generated_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.CheckConstraint(in_list("status", BUILD_STATUSES),
                           name="ck_drafts_status"),
    )
    # частичный уникальный: удалённые черновики лида не мешают собрать новый
    op.create_index("uq_drafts_lead_active", "drafts", ["lead_id"], unique=True,
                    postgresql_where=sa.text("deleted_at IS NULL"))
    op.create_index("ix_drafts_status", "drafts", ["status"])


def downgrade():
    op.drop_table("drafts")
    for name, _, _ in reversed(LEAD_COLUMNS):
        op.drop_column("leads", name)
