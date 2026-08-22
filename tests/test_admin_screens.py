"""Экраны панели: цифры сходятся с прямыми запросами, фильтры /leads работают.

Ожидаемые значения считаются прямым запросом прямо перед обращением к экрану:
тестовая база общая, и жёстко зашитые числа врали бы при любых соседних данных.
Сами цифры берутся со страницы из data-атрибутов, а не поиском по тексту.
"""
import re
import uuid
from decimal import Decimal

from sqlalchemy import func, select

import config
import costs
from models import (
    ClientService, Contact, CostLedger, Lead, Session, log_event, month_start,
)

ACTIVE = (Lead.cancelled_at.is_(None), Lead.deleted_at.is_(None))


def value_of(html: str, attr: str) -> str:
    match = re.search(rf'{attr}="([^"]+)"', html)
    assert match, f"на странице нет атрибута {attr}"
    return match.group(1)


def tag() -> str:
    """Уникальная метка прогона: по ней тест видит только свои строки."""
    return f"pytest-{uuid.uuid4().hex[:8]}"


async def test_dashboard_matches_direct_queries(admin, make_lead):
    async with Session() as s, s.begin():
        # verified без наблюдения не пускает ck_leads_verified_needs_gap
        lead = await make_lead(s, status="verified", gap_type="slow", gap_value="8")
        s.add(ClientService(lead_id=lead.id, service_id="gbp-optimization",
                            price_usd=Decimal("70.00")))
        s.add(CostLedger(op="draft", model="pytest-dash", cost_usd=Decimal("2.50"),
                         api_calls=2, batch_id="pytest-admin"))

    async with Session() as s:
        mrr = await s.scalar(
            select(func.coalesce(func.sum(ClientService.price_usd), 0))
            .where(ClientService.status == "active")
        )
        spent = await costs.month_spent(s)
        verified = await s.scalar(
            select(func.count()).select_from(Lead)
            .where(*ACTIVE, Lead.status == "verified")
        )

    html = (await admin.get("/")).text
    assert value_of(html, "data-mrr") == f"{mrr:.2f}"
    assert value_of(html, "data-spent") == f"{spent:.2f}"
    assert f'data-status="verified" data-count="{verified}"' in html
    # воронка показывает все статусы, включая скаутские raw/candidate
    for key, _ in config.STATUSES:
        assert f'data-status="{key}"' in html


async def test_dashboard_shows_last_events(admin, make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        log_event(s, lead.id, "pytest_event", 1, field="status",
                  old="new", new="verified")
        lead_id = lead.id

    html = (await admin.get("/")).text
    assert "pytest_event" in html
    assert f'href="/leads/{lead_id}"' in html


async def test_costs_screen_matches_direct_queries(admin):
    model = tag()
    async with Session() as s, s.begin():
        s.add(CostLedger(op="qa", model=model, cost_usd=Decimal("1.2345"),
                         api_calls=3, input_tokens=1000, output_tokens=20,
                         batch_id="pytest-admin"))
        s.add(CostLedger(op="qa", model=model, cost_usd=Decimal("0.7655"),
                         api_calls=1, batch_id="pytest-admin"))

    async with Session() as s:
        total = await s.scalar(
            select(func.coalesce(func.sum(CostLedger.cost_usd), 0))
            .where(CostLedger.created_at >= month_start())
        )
        ours = await s.scalar(
            select(func.sum(CostLedger.cost_usd))
            .where(CostLedger.model == model)
        )

    html = (await admin.get("/costs")).text
    assert value_of(html, "data-total") == f"{total:.4f}"
    assert f'data-op="qa" data-model="{model}" data-cost="{ours:.4f}"' in html


async def test_costs_window_switches_to_day(admin):
    html = (await admin.get("/costs?window=day")).text
    assert "Расходы за сегодня" in html


async def test_subs_screen_matches_direct_queries(admin, make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        active = ClientService(lead_id=lead.id, service_id="missed-call-textback",
                               price_usd=Decimal("50.00"))
        canceled = ClientService(lead_id=lead.id, service_id="gbp-optimization",
                                 price_usd=Decimal("999.00"), status="canceled")
        s.add_all([active, canceled])
        await s.flush()
        active_id, canceled_id = active.id, canceled.id

    async with Session() as s:
        mrr = await s.scalar(
            select(func.coalesce(func.sum(ClientService.price_usd), 0))
            .where(ClientService.status == "active")
        )

    html = (await admin.get("/subs")).text
    assert value_of(html, "data-subs-mrr") == f"{mrr:.2f}"
    assert f'data-sub="{active_id}"' in html
    # отменённая в списке есть, но в MRR (прямая сумма выше) не входит
    assert f'data-sub="{canceled_id}"' in html


async def test_leads_filters(admin, make_lead, worker_id):
    niche, country = tag(), tag()
    async with Session() as s, s.begin():
        mine = await make_lead(s, niche=niche, country=country, status="sent")
        other = await make_lead(s, status="new")
        mine_id, other_id = mine.id, other.id

    # каждый фильтр по отдельности оставляет наш лид и убирает соседний
    for query in (f"?niche={niche}", f"?country={country}", "?status=sent",
                  f"?worker={worker_id}&niche={niche}", "?days=0&status=sent"):
        html = (await admin.get(f"/leads{query}")).text
        assert f'data-lead="{mine_id}"' in html, query
        assert f'data-lead="{other_id}"' not in html, query


async def test_leads_pagination_by_25(admin, make_lead):
    niche = tag()
    async with Session() as s, s.begin():
        leads = [await make_lead(s, niche=niche) for _ in range(26)]
        ids = sorted(lead.id for lead in leads)

    first = (await admin.get(f"/leads?niche={niche}")).text
    assert value_of(first, "data-total") == "26"
    assert first.count('data-lead="') == 25
    # сортировка та же, что в боте: сверху самый свежий
    assert value_of(first, "data-lead") == str(ids[-1])

    second = (await admin.get(f"/leads?niche={niche}&offset=25")).text
    assert second.count('data-lead="') == 1
    assert f'data-lead="{ids[0]}"' in second


async def test_leads_htmx_returns_only_table(admin, make_lead):
    niche = tag()
    async with Session() as s, s.begin():
        await make_lead(s, niche=niche)

    response = await admin.get(f"/leads?niche={niche}",
                               headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert response.text.lstrip().startswith("<div id=\"leads\"")
    assert "<html" not in response.text


async def test_lead_card_shows_contacts_and_history(admin, make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s, note="pytest-заметка")
        s.add(Contact(lead_id=lead.id, ctype="email", value="pytest@example.com"))
        log_event(s, lead.id, "pytest_card", 1)
        lead_id, name = lead.id, lead.name

    html = (await admin.get(f"/leads/{lead_id}")).text
    assert name in html
    assert "pytest@example.com" in html
    assert "pytest-заметка" in html
    assert "pytest_card" in html


async def test_deleted_lead_hidden(admin, make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        lead.deleted_at = func.now()
        lead_id = lead.id

    assert (await admin.get(f"/leads/{lead_id}")).status_code == 404
    html = (await admin.get("/leads")).text
    assert f'data-lead="{lead_id}"' not in html
