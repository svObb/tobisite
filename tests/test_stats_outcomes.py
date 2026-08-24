"""Учёт исходов (6.15): «принято админом» и «продано» — разные числа.

Дефект: приёмка, продажа и отказ клиента складывались в один счётчик
«Принято», и работник не видел ни одной своей продажи отдельно.
"""
import re
from types import SimpleNamespace

from sqlalchemy import func, select

import config
from handlers_admin import stats
from handlers_worker import my_stats, outcome_line, status_counts
from models import Lead, Session, Worker

ADMIN = 1
ACTIVE = (Lead.cancelled_at.is_(None), Lead.deleted_at.is_(None))


class FakeState:
    def __init__(self):
        self.data = {}
        self.state = None

    async def set_state(self, state):
        self.state = state

    async def update_data(self, **kw):
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)


class FakeMsg:
    def __init__(self, tg_id=ADMIN):
        self.from_user = SimpleNamespace(id=tg_id)
        self.sent = []

    async def answer(self, text, **kw):
        self.sent.append(text)


def test_sold_and_refused_are_counted_apart_from_acceptance():
    line = outcome_line({"new": 5, "verified": 2, "sold": 3, "refused": 1,
                         "rejected": 4})
    # принято = всё, что прошло проверку админом: 2 + 3 + 1
    assert "Принято: 6" in line
    assert "продано: 3" in line and "отказ клиента: 1" in line
    assert "отклонено: 4" in line


def test_empty_base_gives_zeros():
    assert outcome_line({}) == ("Принято: 0 · продано: 0 · отказ клиента: 0 · "
                               "отклонено: 0")


async def test_status_counts_matches_a_direct_query(make_lead, worker_id):
    base = [Lead.worker_id == worker_id, *ACTIVE]
    async with Session() as s, s.begin():
        await make_lead(s, status="sold")
        await make_lead(s, status="refused")
        await make_lead(s, status="rejected")
        await make_lead(s)

    async with Session() as s:
        counts = await status_counts(s, base)
        expected = dict((await s.execute(
            select(Lead.status, func.count(Lead.id)).where(*base)
            .group_by(Lead.status)
        )).all())
    assert counts == expected
    assert counts["sold"] == 1 and counts["refused"] == 1


async def test_worker_sees_his_sale_apart_from_acceptance(make_lead, worker_id):
    async with Session() as s, s.begin():
        await make_lead(s, status="sold")
        await make_lead(s, status="refused")
        await make_lead(s, status="verified", gap_type="slow", gap_value="8")
        worker = await s.get(Worker, worker_id)

    msg = FakeMsg(tg_id=worker.tg_id)
    await my_stats(msg, FakeState(), worker)
    text, = msg.sent
    assert "Принято: 3 · продано: 1 · отказ клиента: 1" in text


async def test_admin_stats_shows_the_same_split(make_lead):
    async with Session() as s, s.begin():
        await make_lead(s, status="sold")

    async with Session() as s:
        sold = await s.scalar(
            select(func.count()).select_from(Lead)
            .where(*ACTIVE, Lead.status == "sold")
        )

    msg = FakeMsg()
    await stats(msg, FakeState())
    text, = msg.sent
    assert int(re.search(r"продано: (\d+)", text).group(1)) == sold
    assert "отказ клиента" in text and "отклонено" in text


def test_sold_and_refused_stay_inside_acceptance():
    """Продажа и отказ клиента приёмку не отменяют — иначе «принято» упадёт."""
    for key in ("sold", "refused"):
        assert key in config.ACCEPTED_STATUSES
