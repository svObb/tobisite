"""Факт-стоимость письма (9.17): сколько письмо стоило на самом деле.

Окно теста начинается меткой времени самой базы: часы контейнера и часы
машины могут разойтись, а записи cost_ledger штампует база.
"""
from decimal import Decimal

from sqlalchemy import func, select

import costs
import email_gen
from handlers_admin import costs_report
from models import CostLedger, Session
from test_email_gen import UK_DRAFT, UK_JSON
from test_reject_reason import FakeMsg, FakeState
from test_suppression_log import cmd


async def _db_now():
    async with Session() as s:
        return await s.scalar(select(func.now()))


async def _spend(lead_id, usd, *, op="letter", calls=1):
    async with Session() as s, s.begin():
        s.add(CostLedger(op=op, model="pytest-letter", cost_usd=Decimal(usd),
                         api_calls=calls, lead_id=lead_id,
                         batch_id="pytest-letter"))


# --- счёт ---------------------------------------------------------------------

async def test_cost_is_spend_divided_by_letters(make_lead):
    since = await _db_now()
    async with Session() as s, s.begin():
        first, second = await make_lead(s), await make_lead(s)
    await _spend(first.id, "0.0040")
    await _spend(second.id, "0.0060")

    cost = await costs.letter_cost(since)

    assert (cost.units, cost.calls) == (2, 2)
    assert cost.total == Decimal("0.0100")
    assert cost.per_unit == Decimal("0.0050") and cost.within_target


async def test_regeneration_is_the_same_letter(make_lead):
    since = await _db_now()
    async with Session() as s, s.begin():
        lead = await make_lead(s)
    # линтер завернул первый ответ: два вызова модели, письмо по-прежнему одно
    await _spend(lead.id, "0.0030")
    await _spend(lead.id, "0.0030")

    cost = await costs.letter_cost(since)

    assert (cost.units, cost.calls) == (1, 2)
    assert cost.per_unit == Decimal("0.0060")


async def test_other_operations_do_not_count(make_lead):
    since = await _db_now()
    async with Session() as s, s.begin():
        lead = await make_lead(s)
    await _spend(lead.id, "0.0010")
    await _spend(lead.id, "9.9900", op="draft")

    cost = await costs.letter_cost(since)

    assert cost.total == Decimal("0.0010") and cost.units == 1


async def test_empty_window_counts_nothing():
    cost = await costs.letter_cost(await _db_now())
    assert (cost.units, cost.total, cost.per_unit) == (0, 0, 0)
    assert cost.within_target


async def test_expensive_letter_misses_the_target(make_lead):
    since = await _db_now()
    async with Session() as s, s.begin():
        lead = await make_lead(s)
    await _spend(lead.id, "0.0101")

    assert not (await costs.letter_cost(since)).within_target


# --- живое письмо -------------------------------------------------------------

async def test_generated_letter_stays_within_a_cent(model, gap_lead):
    """Цель пункта на фикстуре: одно письмо — не дороже цента."""
    model(UK_JSON)
    lead = await gap_lead()
    since = await _db_now()

    result = await email_gen.build_email(lead, UK_DRAFT)

    cost = await costs.letter_cost(since)
    assert result.ok and cost.units == 1
    assert cost.within_target, f"письмо стоило ${cost.per_unit}"


# --- отчёт --------------------------------------------------------------------

async def test_costs_report_shows_the_fact_cost(make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s)
    await _spend(lead.id, "0.0050")

    msg = FakeMsg()
    await costs_report(msg, FakeState(), cmd("month"))

    assert "Письмо по факту:" in msg.sent[0]
    assert f"${costs.LETTER_TARGET_USD:.2f}" in msg.sent[0]
