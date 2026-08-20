"""CSV уважает фильтры и выгружает draft_url/admin_note (дефекты 6.11–6.12)."""
from datetime import datetime

from sqlalchemy import select

import config
from handlers_admin import CSV_HEADER, flt_conditions
from models import Lead, Session


def test_csv_header_has_new_columns():
    assert CSV_HEADER[-2:] == ["черновик", "заметка_админа"]


async def test_flt_conditions_filter_and_hide_cancelled(worker_id, make_lead):
    async with Session() as s, s.begin():
        await make_lead(s, country="Украина")
        sk = await make_lead(s, country="Словакия")
        sk_id = sk.id

    conds = flt_conditions({"worker_id": worker_id, "country": "Словакия"})
    async with Session() as s:
        ids = [l.id for l in await s.scalars(select(Lead).where(*conds))]
    assert ids == [sk_id]

    async with Session() as s, s.begin():
        (await s.get(Lead, sk_id)).cancelled_at = datetime.now(config.TZ)
    async with Session() as s:
        ids = [l.id for l in await s.scalars(select(Lead).where(*conds))]
    assert ids == []
