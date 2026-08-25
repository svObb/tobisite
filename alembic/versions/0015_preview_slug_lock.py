"""Слаг превью закрепляется за черновиком: уникальный индекс и статус publishing (10.12)

Revision ID: 0015
Revises: 0014
"""
import sqlalchemy as sa
from alembic import op

from models import BUILD_STATUSES, in_list

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

NOT_NULL = sa.text("r2_prefix IS NOT NULL")


def upgrade():
    # Констрейнт собирается из текущего BUILD_STATUSES — как в 0004 у статусов
    # лида: на свежей базе 0008 уже создал его с publishing, здесь это no-op.
    op.drop_constraint("ck_drafts_status", "drafts", type_="check")
    op.create_check_constraint(
        "ck_drafts_status", "drafts", in_list("status", BUILD_STATUSES)
    )
    # два черновика с одним слагом — это одна страница, затёртая чужой
    # компанией; резерв слага до выкладки в R2 опирается именно на этот индекс
    op.create_index("uq_drafts_r2_prefix", "drafts", ["r2_prefix"], unique=True,
                    postgresql_where=NOT_NULL)


def downgrade():
    op.drop_index("uq_drafts_r2_prefix", "drafts")
    op.execute("UPDATE drafts SET status = 'generated' WHERE status = 'publishing'")
    op.drop_constraint("ck_drafts_status", "drafts", type_="check")
    op.create_check_constraint(
        "ck_drafts_status", "drafts",
        in_list("status", [s for s in BUILD_STATUSES if s != "publishing"]),
    )
