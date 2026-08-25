"""Тексты слотов черновика для публикации без нового вызова модели (10.13)

Revision ID: 0013
Revises: 0012
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("drafts", sa.Column("slots_json", JSONB))


def downgrade():
    op.drop_column("drafts", "slots_json")
