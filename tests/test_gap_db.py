"""Схема наблюдения: CHECK-и, TTL, анти-копипаста и стоп-лист suppression."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

import config
from models import (
    Contact, GAP_TTL_DAYS, Lead, Session, Suppression, company_key, email_key,
    engine, gap_age_days, gap_repeated, gap_stale, suppression_hit,
)

GAP_COLUMNS = ["gap_type", "gap_value", "gap_note", "gap_screenshot",
               "gap_captured_at", "gap_seconds", "gap_auto_verified"]


async def test_migration_added_gap_columns():
    async with engine.connect() as conn:
        columns = await conn.run_sync(
            lambda sync: {c["name"] for c in inspect(sync).get_columns("leads")}
        )
    assert set(GAP_COLUMNS) <= columns


async def test_bad_gap_type_rejected_by_db(make_lead):
    with pytest.raises(IntegrityError):
        async with Session() as s, s.begin():
            await make_lead(s, gap_type="site_is_bad")


async def test_verified_without_gap_rejected_by_db(make_lead):
    with pytest.raises(IntegrityError):
        async with Session() as s, s.begin():
            await make_lead(s, status="verified")


async def test_verified_with_gap_passes(make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s, status="verified", gap_type="slow",
                               gap_value="8", gap_captured_at=datetime.now(config.TZ))
    assert lead.id


async def test_verified_update_of_gapless_lead_rejected(make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s)
    with pytest.raises(IntegrityError):
        async with Session() as s, s.begin():
            fresh = await s.get(Lead, lead.id)
            fresh.status = "verified"


# --- TTL --------------------------------------------------------------------

async def test_gap_stale_after_ttl(make_lead):
    now = datetime.now(config.TZ)
    async with Session() as s, s.begin():
        fresh = await make_lead(s, gap_type="slow", gap_value="8",
                                gap_captured_at=now - timedelta(days=3))
        old = await make_lead(s, gap_type="slow", gap_value="9",
                              gap_captured_at=now - timedelta(days=GAP_TTL_DAYS + 6))
        never = await make_lead(s)
    assert not gap_stale(fresh)
    assert gap_stale(old)
    assert gap_age_days(old) == GAP_TTL_DAYS + 6
    assert not gap_stale(never) and gap_age_days(never) is None


# --- правило 4 на живых данных ----------------------------------------------

async def test_gap_repeated_catches_the_same_text(worker_id, make_lead):
    async with Session() as s, s.begin():
        first = await make_lead(s, gap_type="no_prices",
                                gap_value="шукав ціну на імплантацію")
    async with Session() as s:
        assert await gap_repeated(s, worker_id, "no_prices",
                                  "Шукав ціну на  імплантацію", None)
        assert not await gap_repeated(s, worker_id, "no_prices",
                                      "немає цін на чищення зубів", None)
        # переснятие того же лида не считается копипастой самого себя
        assert not await gap_repeated(s, worker_id, "no_prices",
                                      "шукав ціну на імплантацію", None,
                                      exclude_lead_id=first.id)


async def test_gap_repeated_ignores_button_values(worker_id, make_lead):
    async with Session() as s, s.begin():
        await make_lead(s, gap_type="no_booking", gap_value="тільки телефон")
    async with Session() as s:
        assert not await gap_repeated(s, worker_id, "no_booking",
                                      "тільки телефон", None)


# --- стоп-лист --------------------------------------------------------------

async def _suppress(kind, value):
    async with Session() as s, s.begin():
        s.add(Suppression(kind=kind, value_norm=value, reason="pytest",
                          source="pytest"))


async def test_bad_suppression_kind_rejected():
    with pytest.raises(IntegrityError):
        await _suppress("phone", "+380501112233")


async def test_suppression_hit_by_domain(make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s, website_url="https://sup-domain.example",
                               domain_norm="sup-domain.example")
    async with Session() as s:
        assert not await suppression_hit(s, lead)
    await _suppress("domain", "sup-domain.example")
    async with Session() as s:
        assert await suppression_hit(s, lead)


async def test_suppression_hit_by_company(make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s, name="  Клініка Пітер  ", city="Львів")
    async with Session() as s:
        assert not await suppression_hit(s, lead)
    await _suppress("company", company_key("клініка пітер", "львів"))
    async with Session() as s:
        assert await suppression_hit(s, lead)


async def test_suppression_hit_by_email_hash(make_lead):
    async with Session() as s, s.begin():
        lead = await make_lead(s)
        s.add(Contact(lead_id=lead.id, ctype="email", value="Stop@Example.COM"))
    async with Session() as s:
        assert not await suppression_hit(s, lead)
    await _suppress("email_hash", email_key(" stop@example.com "))
    async with Session() as s:
        assert await suppression_hit(s, lead)


async def test_suppression_unique_per_kind_and_value():
    await _suppress("domain", "sup-twice.example")
    with pytest.raises(IntegrityError):
        await _suppress("domain", "sup-twice.example")
