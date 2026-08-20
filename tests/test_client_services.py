"""client_services (16.13): дефолты, CHECK-констрейнт статуса, подсчёт MRR."""
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import services
from models import ClientService, Session


def test_catalog_has_known_ids():
    # /subs валидирует service_id по каталогу — каталог должен быть непустым
    # и содержать якорные услуги волны 1
    ids = {s["id"] for s in services.SERVICES}
    assert "missed-call-textback" in ids
    assert "gbp-optimization" in ids


async def _add(make_lead, **kw) -> int:
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        cs = ClientService(lead_id=lead.id,
                           service_id=kw.pop("service_id", "gbp-optimization"),
                           price_usd=kw.pop("price_usd", Decimal("50")), **kw)
        s.add(cs)
        await s.flush()
        return cs.id


async def test_defaults_active_and_started(make_lead):
    cs_id = await _add(make_lead)
    async with Session() as s:
        cs = await s.get(ClientService, cs_id)
        assert cs.status == "active"
        assert cs.started_at is not None
        assert cs.canceled_at is None
        assert cs.price_usd == Decimal("50")


async def test_bad_status_rejected_by_db(make_lead):
    with pytest.raises(IntegrityError):
        await _add(make_lead, status="expired")


async def test_mrr_counts_only_active(make_lead):
    a = await _add(make_lead, price_usd=Decimal("75"))
    b = await _add(make_lead, service_id="missed-call-textback",
                   price_usd=Decimal("50"))
    await _add(make_lead, status="canceled", price_usd=Decimal("999"))
    async with Session() as s:
        mrr = await s.scalar(
            select(func.coalesce(func.sum(ClientService.price_usd), 0))
            .where(ClientService.status == "active",
                   ClientService.id.in_([a, b]))
        )
    assert Decimal(mrr) == Decimal("125")
