"""Факт-стоимости черновика и лида (13.5) и не-ИИ API в журнале (20.3).

Стоимость письма живёт в test_letter_cost.py — она появилась раньше и у неё
своя цель. Здесь то, что обобщило её на остальные единицы работы.

Окно каждого теста начинается меткой времени самой базы: часы контейнера и
часы машины могут разойтись, а записи cost_ledger штампует база.
"""
from decimal import Decimal

from sqlalchemy import func, select

import config
import costs
from handlers_admin import costs_report
from models import CostLedger, Session
from test_reject_reason import FakeMsg, FakeState
from test_suppression_log import cmd


async def _db_now():
    async with Session() as s:
        return await s.scalar(select(func.now()))


async def _spend(lead_id, usd, *, op, calls=1):
    async with Session() as s, s.begin():
        s.add(CostLedger(op=op, model="pytest-unit", cost_usd=Decimal(usd),
                         api_calls=calls, lead_id=lead_id,
                         batch_id="pytest-unit"))


# --- черновик -----------------------------------------------------------------

async def test_draft_cost_is_spend_divided_by_drafts(make_lead):
    since = await _db_now()
    async with Session() as s, s.begin():
        first, second = await make_lead(s), await make_lead(s)
    await _spend(first.id, "0.0040", op="draft")
    await _spend(second.id, "0.0080", op="draft")

    cost = await costs.draft_cost(since)

    assert (cost.units, cost.calls) == (2, 2)
    assert cost.per_unit == Decimal("0.0060")
    # цели по черновику нет — обещать по нему нечего
    assert cost.target is None and cost.within_target


async def test_slot_regeneration_is_the_same_draft(make_lead):
    since = await _db_now()
    async with Session() as s, s.begin():
        lead = await make_lead(s)
    await _spend(lead.id, "0.0030", op="draft")
    await _spend(lead.id, "0.0030", op="draft")

    cost = await costs.draft_cost(since)

    assert (cost.units, cost.calls) == (1, 2)
    assert cost.per_unit == Decimal("0.0060")


async def test_letters_do_not_count_as_drafts(make_lead):
    since = await _db_now()
    async with Session() as s, s.begin():
        lead = await make_lead(s)
    await _spend(lead.id, "9.9900", op="letter")

    assert (await costs.draft_cost(since)).units == 0


# --- лид ----------------------------------------------------------------------

async def test_lead_cost_divides_all_spending_by_new_leads(make_lead):
    since = await _db_now()
    async with Session() as s, s.begin():
        first, second = await make_lead(s), await make_lead(s)
    # разные операции: лид платит за всё, что было потрачено в окне
    await _spend(first.id, "0.0100", op="draft")
    await _spend(second.id, "0.0100", op="letter")
    await _spend(None, "0.0200", op="qa")

    cost = await costs.lead_cost(since)

    assert cost.units == 2 and cost.total == Decimal("0.0400")
    assert cost.per_unit == Decimal("0.0200")


async def test_lead_cost_without_leads_shows_no_number():
    since = await _db_now()
    await _spend(None, "0.5000", op="qa")

    cost = await costs.lead_cost(since)

    # делить не на что: ноль честнее выдуманной стоимости
    assert cost.units == 0 and cost.per_unit == 0
    assert cost.total == Decimal("0.5000")


async def test_three_fact_costs_come_in_one_call():
    assert [u.unit for u in await costs.unit_costs(await _db_now())] == [
        "letter", "draft", "lead"
    ]


# --- не-ИИ API (20.3) ---------------------------------------------------------

async def test_api_call_costs_calls_times_the_sku_price(monkeypatch):
    monkeypatch.setitem(config.API_PRICES, "places", 0.017)
    since = await _db_now()

    await costs.log_api(op="places", calls=3, batch_id="pytest-unit",
                        note="pytest places")

    async with Session() as s:
        row = (await s.scalars(
            select(CostLedger).where(CostLedger.op == "places",
                                     CostLedger.created_at >= since)
        )).one()
    assert row.api_calls == 3 and row.cost_usd == Decimal("0.051000")
    assert row.input_tokens == 0 and row.model is None


async def test_free_api_call_is_still_written_down():
    since = await _db_now()

    await costs.log_api(op="scout", calls=7, batch_id="pytest-unit",
                        note="pytest overpass")

    async with Session() as s:
        row = (await s.scalars(
            select(CostLedger).where(CostLedger.op == "scout",
                                     CostLedger.created_at >= since)
        )).one()
    assert row.cost_usd == 0 and row.api_calls == 7


async def test_price_not_set_means_zero(monkeypatch):
    monkeypatch.setitem(config.API_PRICES, "twilio", 0)
    assert costs.api_price("twilio") == 0
    assert costs.api_price("нет такой операции") == 0


# --- отчёт --------------------------------------------------------------------

async def test_costs_report_shows_all_three_fact_costs(make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s)
    await _spend(lead.id, "0.0050", op="letter")
    await _spend(lead.id, "0.0070", op="draft")

    msg = FakeMsg()
    await costs_report(msg, FakeState(), cmd("month"))

    assert "Факт-стоимости" in msg.sent[0]
    for label in ("Письмо по факту:", "Черновик по факту:", "Лид по факту:"):
        assert label in msg.sent[0]
