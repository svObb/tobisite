"""client_services: реестр подписок клиентов на доп-услуги (16.13)

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

from models import CLIENT_SERVICE_STATUSES, in_list

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

NOW = sa.text("now()")


def upgrade():
    op.create_table(
        "client_services",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("lead_id", sa.BigInteger, sa.ForeignKey("leads.id"),
                  nullable=False),
        sa.Column("service_id", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("price_usd", sa.Numeric(10, 2), nullable=False,
                  server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.Column("canceled_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW,
                  nullable=False),
        sa.CheckConstraint(in_list("status", CLIENT_SERVICE_STATUSES),
                           name="ck_client_services_status"),
    )
    op.create_index("ix_client_services_lead_id", "client_services", ["lead_id"])
    op.create_index("ix_client_services_status", "client_services", ["status"])


def downgrade():
    op.drop_table("client_services")
