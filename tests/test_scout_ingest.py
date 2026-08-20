"""ingest скаута: статусы, контакты, дедуп до INSERT, флаг имя+город,
дневной лимит. Карточки создаются уже со score/verdict — сеть не нужна.

Лиды создаются с worker_id тестового работника, поэтому сессионная чистка
conftest сносит их по диапазону tg_id.
"""
import itertools

from sqlalchemy import select

import config
from dedup import normalize_domain, normalize_phone
from models import Contact, Lead, LeadEvent, Session
from scout.ingest import ingest, scout_imported_today
from scout.types import RawBiz

_seq = itertools.count(1)


def _phone() -> str:
    # диапазон 4xxxxxx — не пересекается с другими файлами тестов
    return f"+38050{4000000 + next(_seq)}"


def _card(**kw) -> RawBiz:
    kw.setdefault("name", f"pytest-scout-{next(_seq)}")
    kw.setdefault("verdict", "review")
    kw.setdefault("score", 45)
    kw.setdefault("reasons", ["тестовая причина"])
    kw.setdefault("source_url", "https://www.openstreetmap.org/node/1")
    return RawBiz(**kw)


async def _ingest(cards, worker_id, batch_id="pytest-scout"):
    return await ingest(
        cards, country="Украина", niche="Стоматология",
        default_city="Тест-город", worker_id=worker_id,
        actor_tg_id=1, batch_id=batch_id,
    )


async def test_ingest_statuses_contact_and_digest_order(worker_id):
    phone = _phone()
    cand = _card(verdict="candidate", score=80, phone=phone, has_ads=True)
    raw = _card(verdict="review", score=45)
    stats = await _ingest([raw, cand], worker_id)

    assert stats.imported_candidate == 1 and stats.imported_raw == 1
    # дайджест показывает топ по убыванию балла
    assert [sc for sc, _, _ in stats.imported] == [80, 45]

    async with Session() as s:
        lead = await s.scalar(select(Lead).where(Lead.name == cand.name))
        assert lead.status == "candidate"
        assert lead.has_ads is True
        assert lead.found_via == "scout:overpass"
        assert lead.language == "не определён"
        assert "80/100" in lead.note and "pytest-scout" in lead.note
        contact = await s.scalar(
            select(Contact).where(Contact.lead_id == lead.id)
        )
        assert contact.ctype == "phone"
        assert contact.value_norm == normalize_phone(phone, "UA")
        event = await s.scalar(
            select(LeadEvent).where(LeadEvent.lead_id == lead.id)
        )
        assert event.event == "scout_import" and event.new_value == "80"

        other = await s.scalar(select(Lead).where(Lead.name == raw.name))
        assert other.status == "raw" and other.has_ads is False


async def test_ingest_skips_existing_domain_and_phone(make_lead, worker_id):
    dom = f"pytest-taken-{next(_seq)}.example"
    phone = _phone()
    async with Session() as s, s.begin():
        lead = await make_lead(
            s, website_url=f"https://{dom}", domain_norm=normalize_domain(dom)
        )
        s.add(Contact(lead_id=lead.id, ctype="phone",
                      value=phone, value_norm=normalize_phone(phone, "UA")))

    stats = await _ingest(
        [_card(website=dom), _card(phone=phone)], worker_id
    )
    assert stats.dup_domain == 1 and stats.dup_phone == 1
    assert stats.imported == []


async def test_ingest_same_name_city_flags_not_blocks(make_lead, worker_id):
    name = f"pytest-scout-same-{next(_seq)}"
    async with Session() as s, s.begin():
        await make_lead(s, name=name, city="Тест-город")

    stats = await _ingest([_card(name=f"  {name.upper()} ")], worker_id)
    assert stats.flagged_name_city == 1 and stats.imported_raw == 1

    async with Session() as s:
        lead = await s.scalar(select(Lead).where(
            Lead.id == stats.imported[0][1]
        ))
        assert lead.possible_duplicate is True


async def test_ingest_daily_limit(worker_id, monkeypatch):
    async with Session() as s:
        current = await scout_imported_today(s)
    monkeypatch.setattr(config, "SCOUT_DAILY_RAW_LIMIT", current + 1)

    stats = await _ingest([_card(), _card()], worker_id)
    assert stats.imported_raw == 1 and stats.limit_skipped == 1
