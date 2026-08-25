"""Ежемесячный счёт подписки, напоминания и предупреждение о следующем
(12.29, 12.16, 12.30).

Наружу отсюда не уходит ничего. Счёт — внутренняя запись: календарь держит
бот, а выставляет счёт и получает деньги человек. Автосписаний нет и не
планируется; все сообщения модуля адресованы админам и работнику лида в
Telegram, клиенту бот не пишет.

Календарь живёт на самой продаже: sales.sub_next_at — дата следующего счёта,
и она двигается ровно на месяц с каждым выставленным. Поэтому простой бота не
съедает месяц: цикл догоняет пропущенное, выставляя счёт за каждый период,
который успел наступить.
"""
import asyncio
import calendar
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

import config
import keyboards as kb
import notify
from models import (
    OPEN_INVOICE_STATUSES, Invoice, Lead, Sale, Session, Worker, log_event,
)
from notify import esc

log = logging.getLogger(__name__)

# Удалённая карточка гасит календарь: счёт по лиду, которого у нас больше нет,
# некому объяснить и не с кем сверить. Подписку при удалении снимает сам
# хендлер (handlers_admin.delete_lead), а это — второй рубеж на случай строки,
# закрытой в обход него.
LIVE = Lead.deleted_at.is_(None)

# Потолок догона за один заход. Бот, простоявший полгода, выставит счета за все
# пропущенные месяцы — это настоящий долг клиента, — но упереться в потолок
# честнее, чем крутить бесконечный цикл на битой дате.
MAX_CATCHUP = 12


@dataclass(frozen=True)
class Tick:
    """Что сделал один заход: id счетов и лидов, по которым были действия."""
    issued: list[int] = field(default_factory=list)
    reminded: list[int] = field(default_factory=list)
    upcoming: list[int] = field(default_factory=list)


def next_month(moment: datetime, anchor_day: int = 0) -> datetime:
    """Следующий месяц; в коротком месяце — его последний день.

    День берётся от якоря — дня, с которого начали подписку, а не от дня
    предыдущего счёта. Иначе подписка от 31 января, разок подрезанная февралём,
    так и осталась бы 28-го навсегда: один короткий месяц сдвигал бы весь
    остаток года.
    """
    year, month = divmod(moment.month, 12)
    year, month = moment.year + year, month + 1
    day = min(anchor_day or moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


async def issue_due(bot) -> list[int]:
    """Счета за наступившие периоды (12.29). Возвращает id выставленных."""
    now = _now()
    async with Session() as s:
        sale_ids = list(await s.scalars(
            select(Sale.id).join(Lead, Lead.id == Sale.lead_id).where(
                Sale.sub_amount.isnot(None),
                Sale.sub_cancelled_at.is_(None),
                Sale.sub_next_at <= now,
                LIVE,
            ).order_by(Sale.id)
        ))
    made = []
    for sale_id in sale_ids:
        try:
            fresh = await _issue(sale_id, now)
        except Exception:
            # календарь остальных продаж не должен зависеть от одной сломанной
            log.exception("продажа %s: счёт не выставлен", sale_id)
            continue
        for invoice_id in fresh:
            made.append(invoice_id)
            await _announce(bot, invoice_id)
    return made


async def remind_unpaid(bot) -> list[int]:
    """Напоминания по неоплаченным счетам (12.16). Возвращает id напомненных.

    Первое напоминание переводит счёт в overdue, дальше повторы идут не чаще
    раза в INVOICE_REMIND_EVERY_DAYS — состояние на самой строке счёта, поэтому
    перезапуск бота не начинает напоминать заново.
    """
    now = _now()
    async with Session() as s:
        rows = (await s.execute(
            select(Invoice, Lead, Worker)
            .join(Lead, Lead.id == Invoice.lead_id)
            .outerjoin(Worker, Worker.id == Lead.worker_id)
            .where(Invoice.status.in_(OPEN_INVOICE_STATUSES),
                   Invoice.due_at <= now, LIVE)
            .order_by(Invoice.id)
        )).all()
    sent = []
    for invoice, lead, worker in rows:
        if not _due_to_remind(invoice, now):
            continue
        try:
            count = await _mark_reminded(invoice.id, now)
        except Exception:
            log.exception("счёт %s: напоминание не записано", invoice.id)
            continue
        if not count:
            continue
        sent.append(invoice.id)
        text = (f"🔔 Счёт #{invoice.id} по <b>#{lead.id} {esc(lead.name)}</b> "
                f"не оплачен: {invoice.amount:.2f} {esc(invoice.currency)}, "
                f"срок был {_date(invoice.due_at)}.\n"
                f"Напоминание {count}-е.")
        await notify.to_admins(bot, text,
                               reply_markup=kb.invoice_kb(invoice.id))
        if worker is not None and not worker.deleted_at and worker.is_active:
            await notify.send(bot, worker.tg_id, text)
    return sent


async def notify_upcoming(bot) -> list[int]:
    """Предупреждение о ближайшем счёте (12.30). Возвращает id лидов.

    Уведомление внутреннее: клиенту бот не пишет. Отмена цикла — командой,
    и она названа прямо в тексте: без неё «предупредили» значит «поставили
    в известность», а не «дали выйти».
    """
    now = _now()
    async with Session() as s:
        rows = (await s.execute(
            select(Sale, Lead).join(Lead, Lead.id == Sale.lead_id)
            .where(Sale.sub_amount.isnot(None),
                   Sale.sub_cancelled_at.is_(None),
                   Sale.sub_next_at > now,
                   Sale.sub_next_at <= now + _notice(),
                   LIVE)
            .order_by(Sale.id)
        )).all()
    told = []
    for sale, lead in rows:
        if sale.sub_notified_at and sale.sub_notified_at >= sale.sub_next_at - _notice():
            continue
        try:
            if not await _mark_notified(sale.id, now):
                continue
        except Exception:
            log.exception("продажа %s: предупреждение не записано", sale.id)
            continue
        told.append(lead.id)
        days = max((sale.sub_next_at - now).days, 0)
        await notify.to_admins(
            bot,
            f"⏰ Очередной счёт по <b>#{lead.id} {esc(lead.name)}</b> — "
            f"{_date(sale.sub_next_at)} (через {days} дн.): "
            f"{sale.sub_amount:.2f} {esc(sale.currency)}.\n"
            f"Остановить подписку: /invoice off {lead.id}",
            reply_markup=kb.open_card_kb(lead.id),
        )
    return told


async def tick(bot) -> Tick:
    """Один заход по календарю: выставить, напомнить, предупредить."""
    return Tick(issued=await issue_due(bot),
                reminded=await remind_unpaid(bot),
                upcoming=await notify_upcoming(bot))


async def run_forever(bot):
    """Фоновая задача бота. Планировщика в проекте нет — цикл здесь."""
    while True:
        await asyncio.sleep(config.BILLING_POLL_SEC)
        try:
            await tick(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("заход по счетам не прошёл: %s", e)


# --- внутреннее ---------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(config.TZ)


def _notice() -> timedelta:
    return timedelta(days=config.INVOICE_NOTICE_DAYS)


def _date(moment: datetime | None) -> str:
    return _local(moment).strftime("%d.%m.%Y") if moment else "—"


def _local(moment: datetime | None) -> datetime | None:
    """Дата в нашем часовом поясе: из базы время приходит в UTC, а «31-е» — это
    31-е по календарю клиента, а не по гринвичу."""
    return moment.astimezone(config.TZ) if moment else None


def _due_to_remind(invoice, now: datetime) -> bool:
    if invoice.reminded_at is None:
        return True
    return invoice.reminded_at <= now - timedelta(
        days=config.INVOICE_REMIND_EVERY_DAYS)


async def _issue(sale_id: int, now: datetime) -> list[int]:
    """Счета одной продажи за все наступившие периоды — одной транзакцией."""
    made = []
    async with Session() as s, s.begin():
        sale = await s.get(Sale, sale_id, with_for_update=True)
        if sale is None or sale.sub_amount is None or sale.sub_cancelled_at:
            return []
        lead = await s.get(Lead, sale.lead_id)
        if lead is None or lead.deleted_at:
            return []
        anchor = _local(sale.sub_started_at)
        anchor_day = anchor.day if anchor else 0
        for _ in range(MAX_CATCHUP):
            period = _local(sale.sub_next_at)
            if period is None or period > now:
                break
            # конфликт по (sale_id, period_start) значит, что счёт за этот
            # месяц уже есть: календарь всё равно двигаем, второго не плодим
            invoice_id = await s.scalar(
                pg_insert(Invoice).values(
                    lead_id=sale.lead_id, sale_id=sale.id, period_start=period,
                    amount=sale.sub_amount, currency=sale.currency,
                    due_at=period + timedelta(days=config.INVOICE_DUE_DAYS),
                ).on_conflict_do_nothing(constraint="uq_invoices_sale_period")
                .returning(Invoice.id)
            )
            sale.sub_next_at = next_month(period, anchor_day)
            if invoice_id is not None:
                made.append(invoice_id)
                log_event(s, sale.lead_id, "invoice_issued",
                          config.ADMIN_TG_ID, field=str(invoice_id),
                          new=f"{sale.sub_amount} {sale.currency}")
        else:
            log.warning("продажа %s: счета догоняют календарь дольше %s "
                        "месяцев — остаток уйдёт следующим заходом",
                        sale_id, MAX_CATCHUP)
    return made


async def _mark_reminded(invoice_id: int, now: datetime) -> int:
    """Отметка напоминания. 0 — счёт уже закрыт или напоминали только что."""
    async with Session() as s, s.begin():
        invoice = await s.get(Invoice, invoice_id, with_for_update=True)
        if invoice is None or invoice.status not in OPEN_INVOICE_STATUSES:
            return 0
        if not _due_to_remind(invoice, now):
            return 0
        invoice.status = "overdue"
        invoice.reminded_at = now
        invoice.reminders += 1
        log_event(s, invoice.lead_id, "invoice_reminded", config.ADMIN_TG_ID,
                  field=str(invoice.id), new=str(invoice.reminders))
        return invoice.reminders


async def _mark_notified(sale_id: int, now: datetime) -> bool:
    async with Session() as s, s.begin():
        sale = await s.get(Sale, sale_id, with_for_update=True)
        if sale is None or sale.sub_amount is None or sale.sub_cancelled_at:
            return False
        if sale.sub_notified_at and sale.sub_notified_at >= sale.sub_next_at - _notice():
            return False
        sale.sub_notified_at = now
        return True


async def _announce(bot, invoice_id: int):
    async with Session() as s:
        invoice = await s.get(Invoice, invoice_id)
        lead = await s.get(Lead, invoice.lead_id) if invoice else None
    if invoice is None or lead is None or lead.deleted_at:
        return
    await notify.to_admins(
        bot,
        f"🧾 Счёт #{invoice.id} по <b>#{lead.id} {esc(lead.name)}</b>: "
        f"{invoice.amount:.2f} {esc(invoice.currency)} за месяц с "
        f"{_date(invoice.period_start)}.\n"
        f"Оплатить до {_date(invoice.due_at)}. Выставляет счёт человек — "
        f"бот только ведёт календарь.",
        reply_markup=kb.invoice_kb(invoice.id),
    )
