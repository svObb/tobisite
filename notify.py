"""Внутренние уведомления бота: админам — о новых лидах, работнику — о его лиде.

Наружу отсюда не уходит ничего: адресаты — только чаты Telegram, которые уже
завели с ботом переписку. Ни одно уведомление не обязано дойти (бота могли
заблокировать, чат не начат), поэтому отказ Telegram только пишется в лог: к
этому моменту данные уже в базе, и ронять хендлер из-за несостоявшегося
сообщения нельзя.
"""
import html
import logging

from sqlalchemy import func, select

import config
import gap_validation as gv
import keyboards as kb
from models import Contact, Lead, Session, Worker

log = logging.getLogger(__name__)

# Значок по исходу: в списке чатов видно, что случилось, ещё до открытия.
STATUS_ICONS = {
    "verified": "✅", "rejected": "🚫", "refused": "🙅", "sold": "💰",
    "replied": "📩", "replied_interested": "🔥",
}


def esc(v) -> str:
    return html.escape(str(v)) if v is not None else "—"


async def send(bot, tg_id: int, text: str, reply_markup=None) -> bool:
    try:
        await bot.send_message(tg_id, text, reply_markup=reply_markup)
        return True
    except Exception as e:
        log.warning("уведомление не дошло до tg=%s: %s", tg_id, e)
        return False


async def to_admins(bot, text: str, *, skip_tg_id: int | None = None,
                    reply_markup=None) -> int:
    """Сообщение всем админам (6.16), кроме инициатора. Сколько дошло."""
    delivered = 0
    for tg_id in config.ADMIN_IDS:
        if tg_id == skip_tg_id:
            continue
        delivered += await send(bot, tg_id, text, reply_markup)
    return delivered


async def new_lead(bot, lead_id: int, *, skip_tg_id: int | None = None) -> int:
    """6.13: админ узнаёт о новой компании сразу, а не при следующем открытии списка."""
    if not config.NOTIFY_NEW_LEAD:
        return 0
    async with Session() as s:
        lead = await s.get(Lead, lead_id)
        if lead is None or lead.deleted_at:
            return 0
        author = await s.get(Worker, lead.worker_id)
        contacts = await s.scalar(
            select(func.count()).select_from(Contact)
            .where(Contact.lead_id == lead_id, Contact.deleted_at.is_(None))
        )
    text = "\n".join([
        f"🆕 <b>#{lead.id} {esc(lead.name)}</b>",
        f"{esc(lead.niche)} · {esc(lead.city)}, {esc(lead.country)}",
        f"Сайт: {esc(lead.website_url) if lead.website_url else 'нет'}",
        f"Наблюдение: {esc(gv.gap_line(lead.gap_type, lead.gap_value, lead.gap_note))}",
        f"Контактов: {contacts}",
        f"Работник: {esc(author.name if author else None)}",
    ])
    return await to_admins(bot, text, skip_tg_id=skip_tg_id,
                           reply_markup=kb.open_card_kb(lead.id))


async def lead_status(bot, lead, worker, old: str, new: str, *,
                      reason: str | None = None,
                      actor_tg_id: int | None = None) -> bool:
    """6.14: решение по лиду работник узнаёт от бота, а не в личной переписке.

    Молчим о внутренних шагах конвейера (config.WORKER_NOTIFY_STATUSES) и о
    собственных действиях работника: он их только что видел на экране.
    """
    # is_active и deleted_at — независимые флаги: отключённый работник строку
    # не теряет, и без первой проверки бот продолжал бы писать тому, кого от
    # работы уже отстранили
    if worker is None or worker.deleted_at or not worker.is_active:
        return False
    if worker.tg_id == actor_tg_id:
        return False
    if new not in config.WORKER_NOTIFY_STATUSES:
        return False
    text = (f"{STATUS_ICONS.get(new, '🔄')} <b>#{lead.id} {esc(lead.name)}</b>: "
            f"{esc(config.STATUS_LABELS.get(old, old))} → "
            f"{esc(config.STATUS_LABELS.get(new, new))}")
    if reason:
        text += f"\nПричина: {esc(config.LEAD_REJECT_LABELS.get(reason, reason))}"
    return await send(bot, worker.tg_id, text)
