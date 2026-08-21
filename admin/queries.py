"""Все SELECT-ы экранов панели. Только чтение — роль admin_ro большего и не умеет.

Цифры обязаны сходиться с ботом, поэтому фильтры и суммы повторяют его код:
расходы месяца считает costs.month_spent(), MRR — как /subs, воронка — по всем
config.STATUSES, а условия и сортировка списка лидов — как flt_conditions/page
в handlers_admin.py. Импортировать оттуда нельзя: это модуль aiogram-хендлеров.
"""
from decimal import Decimal

from sqlalchemy import func, select

import config
import costs
from models import (
    ClientService, Contact, CostLedger, Lead, LeadEvent, Worker, day_start,
    month_start,
)

PAGE_SIZE = 25
EVENTS_ON_CARD = 30
# «живой лид»: то же, что ACTIVE в handlers_admin.py
ACTIVE = (Lead.cancelled_at.is_(None), Lead.deleted_at.is_(None))
# те же окна, что у /costs в боте
COST_WINDOWS = {
    "day": ("за сегодня", lambda: day_start()),
    "week": ("за 7 дней", lambda: day_start(6)),
    "month": ("за месяц", month_start),
}


# --- дашборд -----------------------------------------------------------------

async def funnel(session):
    """[(статус, подпись, счётчик)] по всем config.STATUSES, включая пустые."""
    rows = dict((await session.execute(
        select(Lead.status, func.count()).where(*ACTIVE).group_by(Lead.status)
    )).all())
    return [(key, label, rows.get(key, 0)) for key, label in config.STATUSES]


async def mrr(session) -> Decimal:
    """Сумма price_usd активных подписок — ровно то, что показывает /subs."""
    return Decimal(await session.scalar(
        select(func.coalesce(func.sum(ClientService.price_usd), 0))
        .where(ClientService.status == "active")
    ))


async def month_spent(session) -> Decimal:
    return await costs.month_spent(session)


async def recent_events(session, limit: int = 10):
    return (await session.execute(
        select(LeadEvent, Lead.name)
        .join(Lead, Lead.id == LeadEvent.lead_id)
        .order_by(LeadEvent.id.desc()).limit(limit)
    )).all()


# --- лиды --------------------------------------------------------------------

def lead_conditions(flt: dict):
    conds = list(ACTIVE)
    if flt.get("worker_id"):
        conds.append(Lead.worker_id == flt["worker_id"])
    if flt.get("country"):
        conds.append(Lead.country == flt["country"])
    if flt.get("niche"):
        conds.append(Lead.niche == flt["niche"])
    if flt.get("status"):
        conds.append(Lead.status == flt["status"])
    if flt.get("days") is not None:
        conds.append(Lead.created_at >= day_start(flt["days"]))
    return conds


async def leads_page(session, flt: dict, offset: int):
    """([(лид, имя работника)], всего) — сортировка та же, что в списке бота."""
    conds = lead_conditions(flt)
    total = await session.scalar(
        select(func.count()).select_from(Lead).where(*conds)
    )
    rows = (await session.execute(
        select(Lead, Worker.name).join(Worker, Worker.id == Lead.worker_id)
        .where(*conds).order_by(Lead.id.desc())
        .offset(offset).limit(PAGE_SIZE)
    )).all()
    return rows, total


async def filter_options(session) -> dict:
    """Значения выпадающих списков — из базы, как меню фильтров в боте."""
    countries = list(await session.scalars(
        select(Lead.country).where(*ACTIVE).distinct().order_by(Lead.country)
    ))
    niches = list(await session.scalars(
        select(Lead.niche).where(*ACTIVE).distinct().order_by(Lead.niche)
    ))
    workers = (await session.execute(
        select(Worker.id, Worker.name)
        .where(Worker.deleted_at.is_(None)).order_by(Worker.name)
    )).all()
    return {"countries": countries, "niches": niches, "workers": workers}


async def lead_card(session, lead_id: int) -> dict | None:
    """None — лида нет или он удалён; удалённые не показывает и бот."""
    lead = await session.get(Lead, lead_id)
    if lead is None or lead.deleted_at:
        return None
    contacts = list(await session.scalars(
        select(Contact)
        .where(Contact.lead_id == lead_id, Contact.deleted_at.is_(None))
        .order_by(Contact.id)
    ))
    events = list(await session.scalars(
        select(LeadEvent).where(LeadEvent.lead_id == lead_id)
        .order_by(LeadEvent.id.desc()).limit(EVENTS_ON_CARD)
    ))
    author = await session.get(Worker, lead.worker_id)
    subs = list(await session.scalars(
        select(ClientService).where(ClientService.lead_id == lead_id)
        .order_by(ClientService.id)
    ))
    return {"lead": lead, "author": author, "contacts": contacts,
            "events": events, "subs": subs}


# --- расходы и подписки ------------------------------------------------------

async def cost_breakdown(session, since):
    """Разбивка по op и model — тем же группированием, что /costs в боте."""
    return (await session.execute(
        select(
            CostLedger.op, CostLedger.model,
            func.sum(CostLedger.api_calls),
            func.sum(CostLedger.cost_usd),
            func.sum(CostLedger.input_tokens),
            func.sum(CostLedger.output_tokens),
            func.sum(CostLedger.cache_read_tokens),
        )
        .where(CostLedger.created_at >= since)
        .group_by(CostLedger.op, CostLedger.model)
        .order_by(func.sum(CostLedger.cost_usd).desc())
    )).all()


async def cost_recent(session, limit: int = 20):
    return list(await session.scalars(
        select(CostLedger).order_by(CostLedger.id.desc()).limit(limit)
    ))


async def subscriptions(session, status: str, newest_first: bool = False):
    order = ClientService.id.desc() if newest_first else ClientService.id
    return (await session.execute(
        select(ClientService, Lead.name)
        .join(Lead, Lead.id == ClientService.lead_id)
        .where(ClientService.status == status).order_by(order)
    )).all()
