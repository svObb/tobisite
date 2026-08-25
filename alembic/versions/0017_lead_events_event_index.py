"""Индекс журнала под метрики недели: (event, created_at) (13.1)

Revision ID: 0017
Revises: 0016
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_lead_events_event_created_at", "lead_events",
                    ["event", "created_at"])


def downgrade():
    op.drop_index("ix_lead_events_event_created_at", table_name="lead_events")
