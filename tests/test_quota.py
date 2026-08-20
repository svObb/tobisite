"""Дневной лимит не обходится отменой (дефект 6.8).

Отменённые записи считаются в квоте дня; удалённые админом — нет.
"""
from datetime import datetime

import config
from handlers_worker import used_today
from models import Lead, Session


async def test_cancelled_leads_still_count(worker_id, make_lead):
    async with Session() as s, s.begin():
        a = await make_lead(s)
        b = await make_lead(s)
        b.cancelled_at = datetime.now(config.TZ)
        a_id = a.id

    async with Session() as s:
        assert await used_today(s, worker_id) == 2

    async with Session() as s, s.begin():
        (await s.get(Lead, a_id)).deleted_at = datetime.now(config.TZ)

    async with Session() as s:
        assert await used_today(s, worker_id) == 1
